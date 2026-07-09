"""純 asyncio TCP 連線管理：connect / recv / exponential backoff reconnect。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

OnDataCallback = Callable[[bytes], Awaitable[None]]
OnConnectionCallback = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class ReconnectPolicy:
    initial_sec: float
    max_sec: float


class TcpClient:
    def __init__(
        self,
        host: str,
        port: int,
        on_data: OnDataCallback,
        *,
        policy: ReconnectPolicy,
        on_connect: OnConnectionCallback | None = None,
        on_disconnect: OnConnectionCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_data = on_data
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._policy = policy
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = asyncio.Event()
        self._recv_chunk_size = 4096
        self._connected = False

    async def run(self) -> None:
        """無限重試主迴圈，直到 stop() 被呼叫。"""
        attempt = 0
        while not self._stop_event.is_set():
            reader: asyncio.StreamReader | None = None
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await self._connect()
                attempt = 0
                self._connected = True
                self._logger.info("decoder connected %s:%d", self._host, self._port)
                if self._on_connect is not None:
                    await self._on_connect()
                await self._recv_loop(reader, writer)
            except asyncio.CancelledError:
                raise
            except ConnectionError as exc:
                self._logger.warning("decoder disconnected: %s", exc)
            except OSError as exc:
                self._logger.warning("decoder disconnected: %s", exc)
            except Exception:
                self._logger.exception("unexpected error in tcp client")
            finally:
                if self._connected:
                    self._connected = False
                    if self._on_disconnect is not None:
                        await self._on_disconnect()
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

            if self._stop_event.is_set():
                break

            delay = self._next_backoff(attempt)
            attempt += 1
            self._logger.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """通知 run() 優雅退出。"""
        self._stop_event.set()

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(self._host, self._port)

    async def _recv_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        del writer
        while not self._stop_event.is_set():
            data = await reader.read(self._recv_chunk_size)
            if not data:
                raise ConnectionError("remote closed connection")
            await self._on_data(data)

    def _next_backoff(self, attempt: int) -> float:
        delay = self._policy.initial_sec * (2**attempt)
        return min(delay, self._policy.max_sec)

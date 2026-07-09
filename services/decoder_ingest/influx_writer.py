"""批次寫入 InfluxDB；100 筆或 5 秒 flush；失敗 retry + 本地 fallback。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from .packet_parser import ParsedEvent, bytes_to_printable_ascii


@dataclass(slots=True)
class InfluxWriterConfig:
    url: str
    token: str
    org: str
    bucket: str
    decoder_id: str
    batch_size: int
    flush_interval_sec: float
    fallback_path: Path
    measurement: str = "decoder_raw_events"
    max_retries: int = 3


class InfluxWriter:
    def __init__(
        self,
        config: InfluxWriterConfig,
        *,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._dry_run = dry_run
        self._logger = logger or logging.getLogger(__name__)
        self._client: InfluxDBClientAsync | None = None
        self._write_api = None
        self._buffer: list[ParsedEvent] = []
        self._lock = asyncio.Lock()
        self._last_flush_at: datetime | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """建立 InfluxDBClientAsync（dry_run 時跳過）。"""
        if self._dry_run:
            self._last_flush_at = datetime.now(timezone.utc)
            return

        self._client = InfluxDBClientAsync(
            url=self._config.url,
            token=self._config.token,
            org=self._config.org,
        )
        self._write_api = self._client.write_api()
        self._last_flush_at = datetime.now(timezone.utc)

    async def stop(self) -> None:
        """最終 flush + 關閉 client。"""
        self._stop_event.set()
        await self.flush(force=True)

        if self._client is not None:
            await self._client.close()
            self._client = None
            self._write_api = None

    async def enqueue(self, event: ParsedEvent) -> None:
        should_flush = False
        async with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= self._config.batch_size:
                should_flush = True

        if should_flush:
            await self.flush()

    async def flush_loop(self) -> None:
        """週期性檢查時間閾值並 flush。"""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.flush_interval_sec,
                )
                break
            except asyncio.TimeoutError:
                if self._should_flush_by_time():
                    await self.flush()

    async def flush(self, *, force: bool = False) -> None:
        """滿 batch_size 或 force 時寫入。"""
        async with self._lock:
            if not self._buffer:
                return
            if not force and len(self._buffer) < self._config.batch_size:
                if not self._should_flush_by_time():
                    return

            batch = self._buffer
            self._buffer = []
            self._last_flush_at = datetime.now(timezone.utc)

        points = [self._event_to_point(event) for event in batch]

        if self._dry_run:
            for event in batch:
                self._logger.info(
                    "[dry-run] event_type=%s raw_hex=%s fields=%s",
                    event.event_type,
                    event.raw.hex(),
                    event.fields,
                )
            return

        try:
            await self._write_batch(points)
        except Exception:
            self._logger.exception("influx write failed after retries, writing fallback")
            await self._write_fallback(batch)

    def _event_to_point(self, event: ParsedEvent) -> Point:
        # decoder_id 優先用事件自帶的來源（多 decoder 架構下每個事件各自
        # 標記是哪台 decoder 產生的），沒有才 fallback 到 writer 層級的
        # 固定預設值（單 decoder、無 DECODERS 設定時的舊行為）。用 .tag()
        # 而非 .field()，才能在 Influx 查詢時當篩選/分組依據。
        decoder_id = event.fields.get("decoder_id", self._config.decoder_id)
        point = (
            Point(self._config.measurement)
            .tag("decoder_id", decoder_id)
            .tag("event_type", event.event_type)
            .field("raw_hex", event.raw.hex())
            .field("raw_ascii", bytes_to_printable_ascii(event.raw))
            .time(event.timestamp)
        )
        for key, value in event.fields.items():
            if key == "decoder_id":
                continue
            point = point.field(key, value)
        return point

    async def _write_batch(self, points: list[Point]) -> None:
        """retry max_retries 次。"""
        if self._write_api is None:
            raise RuntimeError("InfluxDB write API not initialized")

        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 1):
            try:
                await self._write_api.write(
                    bucket=self._config.bucket,
                    org=self._config.org,
                    record=points,
                )
                self._logger.debug("flushed %d points to influx", len(points))
                return
            except Exception as exc:
                last_error = exc
                self._logger.warning(
                    "influx write attempt %d/%d failed: %s",
                    attempt,
                    self._config.max_retries,
                    exc,
                )
                if attempt < self._config.max_retries:
                    await asyncio.sleep(0.5 * attempt)

        raise RuntimeError("influx write exhausted retries") from last_error

    async def _write_fallback(self, events: list[ParsedEvent]) -> None:
        """append NDJSON 到 fallback_path。"""
        path = self._config.fallback_path
        path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        for event in events:
            record = {
                "timestamp": event.timestamp.astimezone(timezone.utc).isoformat(),
                "event_type": event.event_type,
                "decoder_id": event.fields.get("decoder_id", self._config.decoder_id),
                "raw_hex": event.raw.hex(),
                "raw_ascii": bytes_to_printable_ascii(event.raw),
                "fields": event.fields,
            }
            lines.append(json.dumps(record, ensure_ascii=False))

        def _append() -> None:
            with path.open("a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")

        await asyncio.to_thread(_append)
        self._logger.warning("wrote %d events to fallback %s", len(events), path)

    def _should_flush_by_time(self) -> bool:
        if not self._buffer:
            return False
        if self._last_flush_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_flush_at).total_seconds()
        return elapsed >= self._config.flush_interval_sec

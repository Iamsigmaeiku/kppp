"""TCP 收包管線：raw capture、單一 decoder 連線、多 decoder 並發、feed 結果處理。

從 main.py 抽出，職責：
  - raw_capture_worker()：把未知封包寫進 raw_capture.log
  - handle_feed_result()：處理 PacketParser 輸出（unknowns/events/passings）
  - _run_single_decoder()：管理單一 decoder TCP 連線（PacketParser + TcpClient）
  - tcp_ingest_loop()：所有已設定的 decoder 併發運作
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .dashboard import (
    broadcast_capture,
    broadcast_decoder_status,
    broadcast_lap_update,
    broadcast_session_info,
)
from .influx_writer import InfluxWriter
from .lap_tracker import LapTracker
from .packet_parser import (
    FeedResult,
    PacketParser,
    ParsedEvent,
    PassingRule,
    UnknownPacket,
    bytes_to_printable_ascii,
    format_passing_calibration_line,
    format_raw_log_line,
)
from .session_manager import SessionManager
from .tcp_client import ReconnectPolicy, TcpClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# raw capture
# ---------------------------------------------------------------------------

async def raw_capture_worker(
    queue: asyncio.Queue[UnknownPacket | None],
    path: Path,
    *,
    stop_event: asyncio.Event,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    while True:
        if stop_event.is_set() and queue.empty():
            break

        try:
            packet = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

        if packet is None:
            queue.task_done()
            break

        line = format_raw_log_line(packet)

        def _append() -> None:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        await asyncio.to_thread(_append)
        logger.info("unknown packet captured: %s", line)
        queue.task_done()


# ---------------------------------------------------------------------------
# calibration helper
# ---------------------------------------------------------------------------

def _append_calibration_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# feed result handler（PacketParser 輸出 → InfluxWriter + LapTracker + dashboard）
# ---------------------------------------------------------------------------

async def handle_feed_result(
    result: FeedResult,
    *,
    decoder_id: str,
    unknown_queue: asyncio.Queue[UnknownPacket | None],
    writer: InfluxWriter,
    dry_run: bool,
    lap_tracker: LapTracker | None = None,
    with_dashboard: bool = False,
    calibration_path: Path | None = None,
    session_manager: SessionManager | None = None,
) -> None:
    del dry_run
    session_id = session_manager.current_session_id if session_manager else None

    for unknown in result.unknowns:
        await unknown_queue.put(unknown)
        if with_dashboard:
            await broadcast_capture(
                timestamp=unknown.timestamp.isoformat(),
                hex_data=unknown.raw.hex(),
                ascii_data=bytes_to_printable_ascii(unknown.raw),
            )

    for event in result.events:
        fields = {**event.fields, "decoder_id": decoder_id}
        if session_id is not None:
            fields["session_id"] = session_id
        await writer.enqueue(
            ParsedEvent(
                timestamp=event.timestamp,
                event_type=event.event_type,
                raw=event.raw,
                fields=fields,
            )
        )

    for passing in result.passings:
        if calibration_path is not None:
            await asyncio.to_thread(
                _append_calibration_line,
                calibration_path,
                format_passing_calibration_line(passing),
            )

        if lap_tracker is None:
            continue
        if session_manager is not None:
            # 用真實牆鐘時間（而非 passing.received_at）記錄活動時間：
            # replay 模式餵的是歷史時間戳，閒置安全網關心的是「伺服器現在
            # 是否還在收到即時資料」，跟被重播的舊時間無關。
            session_manager.note_activity()
            if not session_manager.numbered:
                # 第一筆真實過線才佔「第 N 節」號，避免空 reset/重啟燒號。
                from services.webapp import session_numbering

                try:
                    number = await session_numbering.ensure_session_numbered(
                        session_manager.current_session_id,
                        session_manager.session_started_at,
                    )
                    if number is not None:
                        session_manager.numbered = True
                        if with_dashboard:
                            session_date = None
                            try:
                                from zoneinfo import ZoneInfo

                                from services.webapp.app import app as web_app

                                tz_name = getattr(
                                    getattr(web_app.state, "web_config", None),
                                    "display_timezone",
                                    None,
                                )
                                if tz_name:
                                    at = session_manager.session_started_at
                                    if at.tzinfo is None:
                                        at = at.replace(tzinfo=timezone.utc)
                                    session_date = (
                                        at.astimezone(ZoneInfo(tz_name))
                                        .date()
                                        .isoformat()
                                    )
                            except Exception:
                                session_date = None
                            await broadcast_session_info(
                                session_manager.current_session_id,
                                session_number=number,
                                session_date=session_date,
                            )
                except Exception:
                    logger.exception(
                        "ensure_session_numbered failed for %s",
                        session_manager.current_session_id,
                    )
        state = lap_tracker.record_passing(passing)
        if not state["registered"]:
            logger.info(
                "unregistered transponder passing: %s laps=%s best=%.3fs",
                state["transponder_id"],
                state["lap_count"],
                state["best_lap_time"] or 0.0,
            )
        if with_dashboard:
            await broadcast_lap_update(state)

        # 過去 passing 事件從未寫入 Influx（PassingRule.parse() 恆回傳
        # None，只有 to_passing_event() 被使用）；這裡補上，讓每次通過連同
        # 當下算出的圈速結果一起進 Influx，做為長期分析用的持久化紀錄。
        passing_fields: dict[str, str | int | float | bool] = {
            "transponder_id": state["transponder_id"],
            "car_number": state["car_number"],
            "registered": state["registered"],
            "lap_count": state["lap_count"],
            "last_lap_time": state["last_lap_time"] or 0.0,
            "best_lap_time": state["best_lap_time"] or 0.0,
            "decoder_id": decoder_id,
        }
        if session_id is not None:
            passing_fields["session_id"] = session_id
        await writer.enqueue(
            ParsedEvent(
                timestamp=passing.received_at,
                event_type="passing",
                raw=bytes.fromhex(passing.raw_payload),
                fields=passing_fields,
            )
        )


# ---------------------------------------------------------------------------
# TCP 連線管理
# ---------------------------------------------------------------------------

async def _run_single_decoder(
    endpoint,  # DecoderEndpoint — avoid circular import with config
    *,
    transponder_id_len: int,
    unknown_queue: asyncio.Queue[UnknownPacket | None],
    writer: InfluxWriter,
    dry_run: bool,
    stop_event: asyncio.Event,
    lap_tracker: LapTracker | None = None,
    with_dashboard: bool = False,
    calibration_path: Path | None = None,
    session_manager: SessionManager | None = None,
) -> None:
    """管理單一 decoder 的 TCP 連線；每台各自有自己的 PacketParser
    （TCP stream framing 的 buffer 不可跨連線共用），但共用同一個
    lap_tracker/writer/unknown_queue，讓多台 decoder 的資料合併進同一份
    賽事狀態。
    """
    parser = PacketParser(
        rules=[PassingRule(transponder_id_len=transponder_id_len)]
    )

    async def on_data(chunk: bytes) -> None:
        received_at = datetime.now(timezone.utc)
        result = parser.feed(chunk, received_at=received_at)
        await handle_feed_result(
            result,
            decoder_id=endpoint.decoder_id,
            unknown_queue=unknown_queue,
            writer=writer,
            dry_run=dry_run,
            lap_tracker=lap_tracker,
            with_dashboard=with_dashboard,
            calibration_path=calibration_path,
            session_manager=session_manager,
        )

    async def on_connect() -> None:
        if lap_tracker is None:
            return
        lap_tracker.set_decoder_connected(endpoint.decoder_id, True)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())
            for state in lap_tracker.all_states():
                await broadcast_lap_update(state)

    async def on_disconnect() -> None:
        if lap_tracker is None:
            return
        lap_tracker.set_decoder_connected(endpoint.decoder_id, False)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())
            for state in lap_tracker.all_states():
                await broadcast_lap_update(state)

    tcp_client = TcpClient(
        endpoint.host,
        endpoint.port,
        on_data,
        policy=ReconnectPolicy(
            initial_sec=endpoint.reconnect_initial_sec,
            max_sec=endpoint.reconnect_max_sec,
        ),
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )

    ingest_task = asyncio.create_task(
        tcp_client.run(), name=f"tcp-client-{endpoint.decoder_id}"
    )
    try:
        await stop_event.wait()
    finally:
        tcp_client.stop()
        ingest_task.cancel()
        try:
            await ingest_task
        except asyncio.CancelledError:
            pass


async def tcp_ingest_loop(
    config,  # AppConfig — avoid circular import
    *,
    unknown_queue: asyncio.Queue[UnknownPacket | None],
    writer: InfluxWriter,
    dry_run: bool,
    stop_event: asyncio.Event,
    lap_tracker: LapTracker | None = None,
    with_dashboard: bool = False,
    session_manager: SessionManager | None = None,
) -> None:
    """所有已設定的 decoder 併發運作；每台各自獨立連線/重連，
    共用同一個 lap_tracker，讓同一支 transponder 不管從哪台 decoder
    收到都會合併進同一份賽事狀態（去重靠既有的 noise threshold）。
    """
    decoder_tasks = [
        asyncio.create_task(
            _run_single_decoder(
                endpoint,
                transponder_id_len=config.lap.transponder_prefix_len,
                unknown_queue=unknown_queue,
                writer=writer,
                dry_run=dry_run,
                stop_event=stop_event,
                lap_tracker=lap_tracker,
                with_dashboard=with_dashboard,
                calibration_path=config.passing_calibration_path,
                session_manager=session_manager,
            ),
            name=f"decoder-{endpoint.decoder_id}",
        )
        for endpoint in config.decoders
    ]
    try:
        await stop_event.wait()
    finally:
        for task in decoder_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*decoder_tasks, return_exceptions=True)

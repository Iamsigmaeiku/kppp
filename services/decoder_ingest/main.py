"""組裝 TcpClient / PacketParser / InfluxWriter；處理 signal 與 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, ConfigError, DecoderEndpoint, load_config
from .dashboard import (
    app as dashboard_app,
    broadcast_capture,
    broadcast_decoder_status,
    broadcast_lap_update,
    broadcast_session_info,
    broadcast_session_reset,
    get_reset_hook,
    get_session_started_hook,
    set_lap_tracker,
    set_reset_hook,
    set_session_manager,
)
from .influx_writer import InfluxWriter, InfluxWriterConfig
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
from .session_snapshot import load_snapshot, write_snapshot
from .tcp_client import ReconnectPolicy, TcpClient

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MYLAPS decoder TCP ingest service")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只印到 console，不寫 InfluxDB",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        metavar="LOGFILE",
        help="重播 raw_capture.log 給 parser",
    )
    parser.add_argument(
        "--with-dashboard",
        action="store_true",
        help="啟動 FastAPI WebSocket + 靜態面板 (port 8000)",
    )
    return parser


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


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


async def snapshot_loop(
    lap_tracker: LapTracker,
    session_manager: SessionManager,
    path: Path,
    interval_sec: float,
    *,
    stop_event: asyncio.Event,
) -> None:
    """週期性把 lap_tracker + session_id 寫到本地 JSON，供崩潰後快速復原。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass
        await asyncio.to_thread(write_snapshot, lap_tracker, session_manager, path)


def _append_calibration_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


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
        # decoder_id 隨事件帶入（見多 decoder 架構），讓每個 decoder 各自的
        # 來源在 Influx 裡可分辨；session_id 隨事件帶入（見 SessionManager），
        # 讓同一場次的每一圈可用 session_id 篩選出來重建完整歷史。
        # transponder_id 用 canonical（lap_tracker 已正規化 77/78），避免同一
        # 台車在 Influx 被拆成兩個 series。
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


async def _on_new_session_started(session_manager: SessionManager) -> None:
    """新場次真的開始了（服務啟動的第一節、或 archive_and_reset 換發新
    session_id 後）都要呼叫一次：讓 webapp 佔「第 N 節」號，並廣播給前端
    （即時面板立刻顯示節次，不要退回裸 sess-…）。
    """
    number: int | None = None
    session_date: str | None = None
    hook = get_session_started_hook()
    if hook is not None:
        try:
            result = await hook(
                session_manager.current_session_id,
                session_manager.session_started_at,
            )
            if isinstance(result, int):
                number = result
        except Exception:
            logger.exception(
                "session_started hook failed (webapp session numbering); continuing"
            )
    if number is None:
        try:
            from services.webapp import session_numbering

            number = await session_numbering.ensure_session_numbered(
                session_manager.current_session_id,
                session_manager.session_started_at,
            )
        except Exception:
            logger.exception(
                "ensure_session_numbered on new session failed for %s",
                session_manager.current_session_id,
            )
    if number is not None:
        session_manager.numbered = True
        try:
            from services.webapp.app import app as web_app
            from services.webapp.session_numbering import local_date_iso

            tz_name = getattr(
                getattr(web_app.state, "web_config", None),
                "display_timezone",
                None,
            ) or "Asia/Taipei"
            session_date = local_date_iso(
                session_manager.session_started_at, tz_name
            )
        except Exception:
            session_date = None
    await broadcast_session_info(
        session_manager.current_session_id,
        session_number=number,
        session_date=session_date,
    )


async def _roll_session_if_new_local_day(
    session_manager: SessionManager,
    lap_tracker: LapTracker,
    writer: InfluxWriter | None,
    *,
    tz_name: str,
    with_dashboard: bool = False,
) -> bool:
    """跨本地日：強制歸檔（有成績才寫 Influx）並換新 session_id。

    歷史 bug：昨晚第 9 節空著沒過線、snapshot 卻還掛著 → 隔天早上即時面板
    仍顯示「第 9 節（昨天）」。auto_idle 又要求 has_archivable_results，空節
    永遠不會被收掉。這裡不看有沒有成績，只要日期過了就換。
    """
    if not session_manager.is_from_previous_local_day(tz_name):
        return False

    archived_id = session_manager.current_session_id
    archived_started = session_manager.session_started_at
    logger.info(
        "day-rollover: archiving stale session_id=%s started=%s",
        archived_id,
        archived_started.isoformat(),
    )
    if writer is not None:
        await session_manager.archive_and_reset(
            lap_tracker, writer, trigger="day_rollover"
        )
    else:
        lap_tracker.reset_session()
        fresh = SessionManager.start_new()
        session_manager.current_session_id = fresh.current_session_id
        session_manager.session_started_at = fresh.session_started_at
        session_manager.last_activity_at = fresh.last_activity_at
        session_manager.numbered = False

    try:
        from services.webapp import session_numbering

        await session_numbering.ensure_session_numbered(archived_id, archived_started)
    except Exception:
        logger.exception(
            "ensure_session_numbered on day-rollover failed for %s", archived_id
        )

    on_reset = get_reset_hook()
    if on_reset is not None:
        on_reset()
    if with_dashboard:
        await _on_new_session_started(session_manager)
        await broadcast_session_reset(
            reset_at=datetime.now(timezone.utc).isoformat()
        )
    return True


async def _discard_stale_snapshot_session(
    lap_tracker: LapTracker,
    session_manager: SessionManager,
    snapshot_path: Path,
    *,
    tz_name: str,
) -> SessionManager:
    """啟動時若 snapshot 是昨天的**空**場次：丟掉、發今天新 session_id。

    有可歸檔成績的隔夜場次留給 writer ready 後的 day-rollover 走完整 archive。
    """
    if not session_manager.is_from_previous_local_day(tz_name):
        return session_manager
    if lap_tracker.has_archivable_results():
        logger.warning(
            "snapshot session_id=%s is from previous local day but has results; "
            "deferring to day-rollover archive",
            session_manager.current_session_id,
        )
        return session_manager
    logger.warning(
        "discarding empty snapshot session_id=%s from previous local day; starting fresh",
        session_manager.current_session_id,
    )
    lap_tracker.reset_session()
    session_manager = SessionManager.start_new()
    write_snapshot(lap_tracker, session_manager, snapshot_path)
    return session_manager


async def lap_timer_broadcast_loop(
    lap_tracker: LapTracker,
    *,
    stop_event: asyncio.Event,
    session_manager: SessionManager | None = None,
    writer: InfluxWriter | None = None,
    auto_archive_idle_sec: float | None = None,
    auto_archive_all_frozen_sec: float | None = None,
    display_timezone: str = "Asia/Taipei",
) -> None:
    """每秒推送本圈計時；逾時或 decoder 斷線時凍結。

    自動歸檔觸發（有可歸檔成績時）：
    1. 完全閒置 >= AUTO_ARCHIVE_IDLE_SEC（安全網）
    2. 全車本圈已暫停 且 閒置 >= AUTO_ARCHIVE_ALL_FROZEN_SEC
       （場次實質結束：車都停了不必再等半小時才進第七節）
    3. 跨本地日（不論有沒有成績）→ day_rollover
    """
    while not stop_event.is_set():
        if session_manager is not None:
            await _roll_session_if_new_local_day(
                session_manager,
                lap_tracker,
                writer,
                tz_name=display_timezone,
                with_dashboard=True,
            )

        states = lap_tracker.all_states()
        for state in states:
            await broadcast_lap_update(state)
        await broadcast_decoder_status(lap_tracker.decoder_status_message())

        if (
            session_manager is not None
            and writer is not None
            and lap_tracker.has_archivable_results()
        ):
            idle = session_manager.idle_seconds()
            should_archive = False
            trigger_reason = ""
            if (
                auto_archive_idle_sec is not None
                and idle >= auto_archive_idle_sec
            ):
                should_archive = True
                trigger_reason = "auto_idle"
            elif (
                auto_archive_all_frozen_sec is not None
                and idle >= auto_archive_all_frozen_sec
                and lap_tracker.all_timers_inactive()
            ):
                should_archive = True
                trigger_reason = "auto_idle"  # 沿用既有 trigger 字面；語意仍是安全網

            if should_archive:
                logger.info(
                    "auto-archive: reason=%s idle=%.0fs session_id=%s cars=%d",
                    trigger_reason,
                    idle,
                    session_manager.current_session_id,
                    len(states),
                )
                archived_id = session_manager.current_session_id
                archived_started = session_manager.session_started_at
                await session_manager.archive_and_reset(
                    lap_tracker, writer, trigger="auto_idle"
                )
                # 歸檔當下再補一次編號：避免第一筆過線時 numbering 失敗，
                # 場次紀錄只顯示裸 sess-… 而不是「第 N 節」。
                try:
                    from services.webapp import session_numbering

                    await session_numbering.ensure_session_numbered(
                        archived_id, archived_started
                    )
                except Exception:
                    logger.exception(
                        "ensure_session_numbered on archive failed for %s",
                        archived_id,
                    )
                on_reset = get_reset_hook()
                if on_reset is not None:
                    on_reset()
                await _on_new_session_started(session_manager)
                await broadcast_session_reset(
                    reset_at=datetime.now(timezone.utc).isoformat()
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            break
        except asyncio.TimeoutError:
            continue


def _parse_replay_line(line: str) -> tuple[datetime, bytes] | None:
    """解析 `{iso} | {hex} | {ascii}` 格式。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [part.strip() for part in stripped.split("|")]
    if len(parts) < 2:
        return None

    try:
        ts = datetime.fromisoformat(parts[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        raw = bytes.fromhex(parts[1])
    except ValueError:
        return None

    return ts, raw


REPLAY_DECODER_ID = "replay"


async def replay_file(
    logfile: Path,
    parser: PacketParser,
    *,
    unknown_queue: asyncio.Queue[UnknownPacket | None],
    writer: InfluxWriter,
    dry_run: bool,
    lap_tracker: LapTracker | None = None,
    with_dashboard: bool = False,
    calibration_path: Path | None = None,
    session_manager: SessionManager | None = None,
) -> None:
    """解析 raw_capture.log 每行 hex，餵給 parser.feed_frame()。"""
    if not logfile.exists():
        raise FileNotFoundError(f"replay log not found: {logfile}")

    if lap_tracker is not None:
        # replay 模式沒有真正的 TCP decoder connect/disconnect 事件，
        # 但邏輯上這段期間就是「decoder 連線中」在餵資料，否則計時器會維持
        # 建構時的預設「未連線」狀態，導致 current_lap_elapsed 永遠回傳
        # None（本圈計時整場顯示「—」）。用一個固定的合成 decoder_id 代表
        # 這段 replay 期間的連線。
        lap_tracker.set_decoder_connected(REPLAY_DECODER_ID, True)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())

    lines = await asyncio.to_thread(logfile.read_text, encoding="utf-8")
    for lineno, line in enumerate(lines.splitlines(), start=1):
        parsed = _parse_replay_line(line)
        if parsed is None:
            if line.strip():
                logger.warning("replay skip line %d: %r", lineno, line)
            continue

        received_at, frame = parsed
        result = parser.feed_frame(frame, received_at=received_at)
        await handle_feed_result(
            result,
            decoder_id=REPLAY_DECODER_ID,
            unknown_queue=unknown_queue,
            writer=writer,
            dry_run=dry_run,
            lap_tracker=lap_tracker,
            with_dashboard=with_dashboard,
            calibration_path=calibration_path,
            session_manager=session_manager,
        )

    logger.info("replay finished: %s", logfile)

    if lap_tracker is not None:
        # replay 結束後模擬斷線，讓計時器行為跟真實斷線一致（凍結在最後
        # elapsed 值），而不是悄悄維持「連線中」但再也不會有新資料。
        lap_tracker.set_decoder_connected(REPLAY_DECODER_ID, False)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())


async def _run_single_decoder(
    endpoint: DecoderEndpoint,
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
    config: AppConfig,
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


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    def _handler() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            signal.signal(sig, lambda _s, _f: _handler())


async def run_service(
    *,
    dry_run: bool,
    replay: Path | None,
    with_dashboard: bool = False,
) -> None:
    config = load_config(dry_run=dry_run)
    setup_logging(config.log_level)
    for endpoint in config.decoders:
        logger.info(
            "decoder target resolved to %s:%d id=%s (from env, .env override=False)",
            endpoint.host,
            endpoint.port,
            endpoint.decoder_id,
        )

    # --replay 只重播單一 log 檔案，不需要模擬多 decoder，固定用一個 parser。
    # 真正的多 decoder TCP 連線（tcp_ingest_loop）則是每台各自建立自己的
    # PacketParser，見 _run_single_decoder。
    parser = PacketParser(
        rules=[
            PassingRule(transponder_id_len=config.lap.transponder_prefix_len),
        ]
    )
    lap_tracker = LapTracker(
        noise_threshold_sec=config.lap.noise_threshold_sec,
        timer_timeout_sec=config.lap.timer_timeout_sec,
        max_lap_time_sec=config.lap.max_lap_time_sec,
        car_number_map=config.lap.car_number_map,
        decoder_ids=[endpoint.decoder_id for endpoint in config.decoders],
        decoder_tick_hz=config.lap.decoder_tick_hz,
    )
    restored = load_snapshot(lap_tracker, config.snapshot_path)
    if restored is not None:
        session_manager = restored.session_manager
    else:
        # orphan / 無 snapshot：發新 session_id，並立刻覆寫磁碟上可能殘留
        # 的無 session_id 舊檔，避免下次啟動又警告一次。
        session_manager = SessionManager.start_new()
        write_snapshot(lap_tracker, session_manager, config.snapshot_path)
    display_tz = os.getenv("DISPLAY_TIMEZONE", "Asia/Taipei").strip() or "Asia/Taipei"
    session_manager = await _discard_stale_snapshot_session(
        lap_tracker,
        session_manager,
        config.snapshot_path,
        tz_name=display_tz,
    )
    writer = InfluxWriter(
        InfluxWriterConfig(
            url=config.influx.url,
            token=config.influx.token,
            org=config.influx.org,
            bucket=config.influx.bucket,
            decoder_id=config.influx.decoder_id,
            batch_size=config.influx.batch_size,
            flush_interval_sec=config.influx.flush_interval_sec,
            fallback_path=config.influx.fallback_path,
        ),
        dry_run=dry_run,
    )
    # snapshot 持久化與 dashboard 無關：auto_idle / 手動 reset 都靠這個 hook
    # 在清空記憶體後立刻寫出「新 session_id + 空 states」。
    set_reset_hook(
        lambda: write_snapshot(lap_tracker, session_manager, config.snapshot_path)
    )
    if with_dashboard:
        set_lap_tracker(lap_tracker)
        set_session_manager(session_manager, writer)

    stop_event = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop_event)

    unknown_queue: asyncio.Queue[UnknownPacket | None] = asyncio.Queue()
    await writer.start()

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(
            raw_capture_worker(
                unknown_queue,
                config.raw_capture_path,
                stop_event=stop_event,
            ),
            name="raw-capture",
        ),
        asyncio.create_task(writer.flush_loop(), name="influx-flush"),
        asyncio.create_task(
            snapshot_loop(
                lap_tracker,
                session_manager,
                config.snapshot_path,
                config.snapshot_interval_sec,
                stop_event=stop_event,
            ),
            name="session-snapshot",
        ),
    ]

    if with_dashboard:
        import uvicorn

        from services.webapp.app import configure_app

        # 掛上 Phase 2+ 新增的 auth/car_bindings/avatars 等路由與
        # middleware；dashboard_app 和 webapp 的 app 是同一個 FastAPI
        # instance（見 services/webapp/app.py），這裡只是把它組裝完整。
        configure_app()
        # 隔夜 snapshot 若還掛著昨天場次（含有成績），writer 已就绪後立刻換日。
        await _roll_session_if_new_local_day(
            session_manager,
            lap_tracker,
            writer,
            tz_name=display_tz,
            with_dashboard=True,
        )
        # 服務啟動時的第一節場次也要走一次「新場次開始」通知，
        # 否則要等到第一次 auto_idle 重置才會第一次被編號/廣播 session_id。
        await _on_new_session_started(session_manager)

        uv_config = uvicorn.Config(
            dashboard_app,
            host=config.dashboard.host,
            port=config.dashboard.port,
            log_level=config.log_level.lower(),
        )
        server = uvicorn.Server(uv_config)
        tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
        logger.info(
            "dashboard listening on http://%s:%d",
            config.dashboard.host,
            config.dashboard.port,
        )
        tasks.append(
            asyncio.create_task(
                lap_timer_broadcast_loop(
                    lap_tracker,
                    stop_event=stop_event,
                    session_manager=session_manager,
                    writer=writer,
                    auto_archive_idle_sec=config.auto_archive_idle_sec,
                    auto_archive_all_frozen_sec=config.auto_archive_all_frozen_sec,
                    display_timezone=display_tz,
                ),
                name="lap-timer-broadcast",
            )
        )

    if replay is not None:
        tasks.append(
            asyncio.create_task(
                replay_file(
                    replay,
                    parser,
                    unknown_queue=unknown_queue,
                    writer=writer,
                    dry_run=dry_run,
                    lap_tracker=lap_tracker,
                    with_dashboard=with_dashboard,
                    calibration_path=config.passing_calibration_path,
                    session_manager=session_manager,
                ),
                name="replay",
            )
        )
    else:
        tasks.append(
            asyncio.create_task(
                tcp_ingest_loop(
                    config,
                    unknown_queue=unknown_queue,
                    writer=writer,
                    dry_run=dry_run,
                    stop_event=stop_event,
                    lap_tracker=lap_tracker,
                    with_dashboard=with_dashboard,
                    session_manager=session_manager,
                ),
                name="tcp-ingest",
            )
        )

    try:
        if replay is not None:
            await tasks[-1]
            if with_dashboard:
                # replay 本身跑完不代表「這次操作」結束：dashboard 還要留著
                # 讓人在瀏覽器上看結果，所以繼續等待 Ctrl+C / SIGTERM，
                # 不要 replay 一結束就把整個服務（含 uvicorn）關掉。
                logger.info(
                    "replay finished, dashboard still serving — press Ctrl+C to stop"
                )
                await stop_event.wait()
            else:
                stop_event.set()
        else:
            await stop_event.wait()
    finally:
        await unknown_queue.join()
        await unknown_queue.put(None)
        await writer.stop()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(
            run_service(
                dry_run=args.dry_run,
                replay=args.replay,
                with_dashboard=args.with_dashboard,
            )
        )
    except ConfigError as exc:
        logging.error("config error: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

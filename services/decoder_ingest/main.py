"""組裝 TcpClient / PacketParser / InfluxWriter；處理 signal 與 CLI。

子模組職責分配：
  - ingest_loop.py   : raw_capture_worker, handle_feed_result,
                       _run_single_decoder, tcp_ingest_loop
  - session_lifecycle.py : snapshot_loop, _on_new_session_started,
                           _roll_session_if_new_local_day,
                           _discard_stale_snapshot_session
  - broadcast.py     : lap_timer_broadcast_loop
  - replay.py        : _parse_replay_line, replay_file
  本檔（main.py）    : build_arg_parser, setup_logging,
                       install_signal_handlers, run_service, main
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from .broadcast import lap_timer_broadcast_loop
from .config import AppConfig, ConfigError, load_config
from .dashboard import (
    app as dashboard_app,
    get_reset_hook,
    set_lap_tracker,
    set_reset_hook,
    set_session_manager,
)
from .ingest_loop import raw_capture_worker, tcp_ingest_loop
from .influx_writer import InfluxWriter, InfluxWriterConfig
from .lap_tracker import LapTracker
from .packet_parser import PacketParser, PassingRule, UnknownPacket
from .replay import replay_file
from .session_lifecycle import (
    _discard_stale_snapshot_session,
    _on_new_session_started,
    _roll_session_if_new_local_day,
    snapshot_loop,
)
from .session_manager import SessionManager
from .session_snapshot import load_snapshot, write_snapshot

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
    # PacketParser，見 ingest_loop._run_single_decoder。
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

"""每秒廣播圈速更新；處理自動歸檔與跨日換日觸發。

從 main.py 抽出，職責：
  - lap_timer_broadcast_loop()：每秒推送本圈計時；逾時或 decoder 斷線時凍結；
    偵測 auto_idle / all_frozen / day-rollover 條件後觸發歸檔換日。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .dashboard import (
    broadcast_decoder_status,
    broadcast_lap_update,
    broadcast_session_reset,
    get_reset_hook,
)
from .lap_tracker import LapTracker
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


async def lap_timer_broadcast_loop(
    lap_tracker: LapTracker,
    *,
    stop_event: asyncio.Event,
    session_manager: SessionManager | None = None,
    writer=None,  # InfluxWriter | None — avoid circular import
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
    # Import here to avoid circular dependency with main/session_lifecycle
    from .session_lifecycle import _on_new_session_started, _roll_session_if_new_local_day

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

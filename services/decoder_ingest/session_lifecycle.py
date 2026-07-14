"""Session 生命週期管理：跨日換日、snapshot 清理、新 session 通知。

從 main.py 抽出，職責：
  - _on_new_session_started()：新場次啟動時通知 dashboard + 編號
  - _roll_session_if_new_local_day()：跨本地日強制歸檔換日
  - _discard_stale_snapshot_session()：啟動時丟棄昨天的空場次
  - snapshot_loop()：週期性把 lap_tracker + session 狀態寫到本地 JSON
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .dashboard import (
    broadcast_session_info,
    broadcast_session_reset,
    get_reset_hook,
    get_session_started_hook,
)
from .lap_tracker import LapTracker
from .session_manager import SessionManager
from .session_snapshot import write_snapshot

logger = logging.getLogger(__name__)


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
    writer,  # InfluxWriter | None — avoid circular import
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

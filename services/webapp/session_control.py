"""本節場次狀態 / 手動歸檔。

即時面板與「我的資料」需要：
1. 看到「今天第 N 節」（不是裸 sess-…）
2. 本圈計時都暫停、且有可歸檔成績時，一鍵歸檔
3. 歸檔後拿到 session_number，好去產生 AI 教練報告

歸檔直接呼叫同 process 的 SessionManager.archive_and_reset（跟
/api/session/reset 同一條路），不需要前端帶 SESSION_RESET_TOKEN——
登入使用者按確認即可（場務收場用）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.dashboard import (
    broadcast_session_info,
    broadcast_session_reset,
    get_influx_writer,
    get_lap_tracker,
    get_reset_hook,
    get_session_manager,
    get_session_started_hook,
)

from . import session_numbering
from .deps import get_db, require_user
from .models import RaceSession, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session")


async def _session_label(db: AsyncSession, session_id: str) -> dict:
    row = await db.get(RaceSession, session_id)
    return {
        "session_id": session_id,
        "session_number": row.session_number if row else None,
        "session_date": row.session_date.isoformat() if row and row.session_date else None,
        "label": (
            f"第 {row.session_number} 節"
            if row and row.session_number
            else session_id
        ),
    }


@router.get("/current")
async def current_session(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    del user  # 只要登入
    sm = get_session_manager()
    lt = get_lap_tracker()
    if sm is None:
        raise HTTPException(status_code=503, detail="session manager not ready")

    # 防禦：若還掛著昨天的場次（day-rollover 還沒跑完），不要秀舊節號。
    stale = False
    try:
        from .app import app as web_app

        tz_name = getattr(
            getattr(web_app.state, "web_config", None), "display_timezone", None
        )
        if tz_name and sm.is_from_previous_local_day(tz_name):
            stale = True
    except Exception:
        logger.exception("stale-session date check failed")

    if not stale:
        # 開節後尚未過線也可能缺號（重啟／舊 snapshot）；補編一次供即時面板。
        label = await _session_label(db, sm.current_session_id)
        if label["session_number"] is None:
            try:
                number = await session_numbering.ensure_session_numbered(
                    sm.current_session_id, sm.session_started_at
                )
                if number is not None:
                    sm.numbered = True
                await db.expire_all()
                label = await _session_label(db, sm.current_session_id)
            except Exception:
                logger.exception(
                    "ensure_session_numbered in /api/session/current failed for %s",
                    sm.current_session_id,
                )
    else:
        label = {
            "session_id": sm.current_session_id,
            "session_number": None,
            "session_date": None,
            "label": sm.current_session_id,
        }

    has_results = bool(lt and lt.has_archivable_results())
    all_paused = bool(lt and lt.all_timers_inactive() and has_results)
    return {
        **label,
        "started_at": sm.session_started_at.isoformat(),
        "idle_seconds": round(sm.idle_seconds(), 1),
        "has_archivable_results": has_results,
        "all_timers_paused": all_paused,
        "can_archive": has_results,
        "numbered": sm.numbered or label.get("session_number") is not None,
    }


@router.post("/archive")
async def archive_session(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手動歸檔本節：寫 session_archive、編號、換新 session_id、廣播前端。"""
    sm = get_session_manager()
    lt = get_lap_tracker()
    writer = get_influx_writer()
    if sm is None or lt is None or writer is None:
        raise HTTPException(status_code=503, detail="session manager not ready")

    if not lt.has_archivable_results():
        raise HTTPException(
            status_code=400,
            detail="本節還沒有可歸檔的圈速（至少要有一台車完成一圈）",
        )

    archived_id = sm.current_session_id
    archived_started = sm.session_started_at
    new_session_id = await sm.archive_and_reset(lt, writer, trigger="manual")

    session_number = None
    try:
        session_number = await session_numbering.ensure_session_numbered(
            archived_id, archived_started
        )
    except Exception:
        logger.exception(
            "ensure_session_numbered on manual archive failed for %s", archived_id
        )

    try:
        row = await db.get(RaceSession, archived_id)
        if row is not None:
            row.ended_at = datetime.now(timezone.utc)
            row.reset_trigger = "manual"
            row.created_by_user_id = user.id
            await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("failed to stamp ended_at for %s", archived_id)

    on_reset = get_reset_hook()
    if on_reset is not None:
        on_reset()

    reset_at = datetime.now(timezone.utc).isoformat()
    await broadcast_session_reset(reset_at=reset_at)

    new_number = None
    new_date = None
    hook = get_session_started_hook()
    if hook is not None:
        try:
            result = await hook(new_session_id, sm.session_started_at)
            if isinstance(result, int):
                new_number = result
                sm.numbered = True
        except Exception:
            logger.exception("session_started hook failed after manual archive")
    if new_number is None:
        try:
            new_number = await session_numbering.ensure_session_numbered(
                new_session_id, sm.session_started_at
            )
            if new_number is not None:
                sm.numbered = True
        except Exception:
            logger.exception(
                "ensure_session_numbered for new session after archive failed"
            )
    if new_number is not None:
        try:
            from .app import app as web_app

            tz_name = getattr(
                getattr(web_app.state, "web_config", None),
                "display_timezone",
                "Asia/Taipei",
            )
            new_date = session_numbering.local_date_iso(
                sm.session_started_at, tz_name
            )
        except Exception:
            new_date = None
    await broadcast_session_info(
        session_id=new_session_id,
        session_number=new_number,
        session_date=new_date,
    )

    await db.expire_all()
    archived_label = await _session_label(db, archived_id)
    if archived_label["session_number"] is None and session_number is not None:
        archived_label["session_number"] = session_number
        archived_label["label"] = f"第 {session_number} 節"

    logger.info(
        "manual archive by user_id=%s archived=%s number=%s new=%s",
        user.id,
        archived_id,
        archived_label.get("session_number"),
        new_session_id,
    )

    return {
        "ok": True,
        "archived": archived_label,
        "new_session_id": new_session_id,
        "reset_at": reset_at,
        "profile_url": "/profile",
        "session_url": f"/sessions/{archived_id}",
    }

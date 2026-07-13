"""場次每日編號（#1~#100，每天從 1 開始、滿 100 循環）。

重要：不再在「session 一開啟」就佔號。重啟 / 空 reset 會產生一堆沒有圈速
的空殼 session_id，若當下就編號，下一場實賽會從第 10、11 節起跳，綁定
與場次列表看起來就像「第五節跟第十節混在一起」。

流程：
1. on_session_started → 只確保 SQLite 有這列，session_number 先留空
2. ensure_session_numbered → 第一筆真實過線時才分配下一個號碼
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RaceSession

logger = logging.getLogger(__name__)


async def on_session_started(session_id: str, started_at: datetime) -> None:
    """新 session_id 出現時只建列、不佔號。"""
    from .app import app

    if not getattr(app.state, "webapp_configured", False):
        return

    session_factory = app.state.session_factory
    tz = ZoneInfo(app.state.web_config.display_timezone)

    at = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    local_date = at.astimezone(tz).date()

    async with session_factory() as db:
        try:
            existing = await db.get(RaceSession, session_id)
            if existing is None:
                db.add(
                    RaceSession(
                        id=session_id,
                        started_at=started_at,
                        session_date=local_date,
                        session_number=None,
                    )
                )
                await db.commit()
            elif existing.session_date is None:
                existing.session_date = local_date
                await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("session row upsert failed for %s", session_id)


async def ensure_session_numbered(session_id: str, started_at: datetime) -> int | None:
    """第一筆真實過線時呼叫：若尚未編號則分配當天 max+1。回傳編號或 None。"""
    from .app import app

    if not getattr(app.state, "webapp_configured", False):
        return None

    session_factory = app.state.session_factory
    tz = ZoneInfo(app.state.web_config.display_timezone)

    at = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    local_date = at.astimezone(tz).date()

    async with session_factory() as db:
        try:
            existing = await db.get(RaceSession, session_id)
            if existing is not None and existing.session_number is not None:
                return existing.session_number

            max_num = await db.scalar(
                select(func.max(RaceSession.session_number)).where(
                    RaceSession.session_date == local_date
                )
            )
            number = ((max_num or 0) % 100) + 1

            if existing is None:
                db.add(
                    RaceSession(
                        id=session_id,
                        started_at=started_at,
                        session_date=local_date,
                        session_number=number,
                    )
                )
            else:
                existing.session_date = local_date
                existing.session_number = number
                if existing.started_at is None:
                    existing.started_at = started_at

            await db.commit()
            logger.info(
                "session numbered: session_id=%s date=%s number=%s",
                session_id,
                local_date,
                number,
            )
            return number
        except IntegrityError:
            await db.rollback()
            logger.warning(
                "session numbering skipped for %s (date=%s): unique constraint hit",
                session_id,
                local_date,
                exc_info=True,
            )
            return None
        except Exception:
            await db.rollback()
            logger.exception("session numbering failed for %s", session_id)
            return None


async def resolve_session_id(
    db: AsyncSession, *, session_date: date, session_number: int
) -> str | None:
    """依「今天第幾節」查回真正的 session_id，供 car_bindings.py 綁定時
    使用者輸入 #N 解析用。
    """
    result = await db.execute(
        select(RaceSession.id).where(
            RaceSession.session_date == session_date,
            RaceSession.session_number == session_number,
        )
    )
    return result.scalar_one_or_none()

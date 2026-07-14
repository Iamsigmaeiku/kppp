"""場次每日編號（#1 起，每天從 1 開始）。

流程：
1. on_session_started → 開新節就佔「今天第 N 節」（即時面板立刻顯示）
2. ensure_session_numbered → 實際分配當天 max+1（冪等）
3. compute_display_labels → 場次列表顯示依 Influx 列表按日重編 1..N
4. sync_numbers_from_labels → 把顯示標籤寫回 SQLite（綁定用）
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

# 現場每天實賽節次上限（綁定 / 即時開節佔號用）。列表顯示不套此上限。
SESSION_NUMBER_MAX = 10


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at


def local_date_iso(started_at: datetime, tz_name: str) -> str:
    return _as_utc(started_at).astimezone(ZoneInfo(tz_name)).date().isoformat()


def compute_display_labels(
    sessions: list,
    *,
    tz_name: str,
) -> dict[str, dict]:
    """依場次列表算「第 N 節」——列表顯示的唯一可靠來源。

    按本地日期分組，組內依 started_at 由舊到新編 1..N。
    回傳 {session_id: {"session_number": int, "session_date": date}}。
    """
    tz = ZoneInfo(tz_name)
    by_day: dict[date, list] = {}
    for sess in sessions:
        started = getattr(sess, "started_at", None) or datetime.now(timezone.utc)
        local_date = _as_utc(started).astimezone(tz).date()
        by_day.setdefault(local_date, []).append(sess)

    labels: dict[str, dict] = {}
    for day, group in by_day.items():
        ordered = sorted(
            group,
            key=lambda s: (
                _as_utc(
                    getattr(s, "started_at", None)
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
                getattr(s, "session_id", ""),
            ),
        )
        for i, sess in enumerate(ordered, start=1):
            labels[sess.session_id] = {
                "session_number": i,
                "session_date": day,
            }
    return labels


async def on_session_started(session_id: str, started_at: datetime) -> int | None:
    """新 session_id 出現時立刻佔今天第 N 節，回傳編號（失敗則 None）。"""
    return await ensure_session_numbered(session_id, started_at)


async def ensure_session_numbered(session_id: str, started_at: datetime) -> int | None:
    """若尚未編號則分配當天 max+1。回傳編號或 None。"""
    from .app import app

    if not getattr(app.state, "webapp_configured", False):
        return None

    session_factory = app.state.session_factory
    tz = ZoneInfo(app.state.web_config.display_timezone)

    at = _as_utc(started_at)
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
            next_num = (max_num or 0) + 1
            if next_num > SESSION_NUMBER_MAX:
                logger.warning(
                    "session numbering capped at %d for date=%s session_id=%s",
                    SESSION_NUMBER_MAX,
                    local_date,
                    session_id,
                )
                return None
            number = next_num

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


async def sync_numbers_from_labels(
    db: AsyncSession,
    sessions: list,
    labels: dict[str, dict],
) -> None:
    """把 compute_display_labels 結果寫回 SQLite（綁定 / 明細頁用）。"""
    if not labels:
        return

    dates = {v["session_date"] for v in labels.values()}
    keep_ids = set(labels.keys())

    live_id: str | None = None
    try:
        from services.decoder_ingest.dashboard import get_session_manager

        sm = get_session_manager()
        if sm is not None:
            live_id = sm.current_session_id
    except Exception:
        logger.exception("sync: could not resolve live session_id")

    # 兩段式清號，避開 uq_session_date_number
    for day in sorted(dates):
        result = await db.execute(
            select(RaceSession).where(RaceSession.session_date == day)
        )
        for row in result.scalars().all():
            row.session_number = None
        await db.flush()

    result = await db.execute(
        select(RaceSession).where(RaceSession.id.in_(list(keep_ids)))
    )
    by_id = {rs.id: rs for rs in result.scalars().all()}

    for sess in sessions:
        lab = labels.get(sess.session_id)
        if lab is None:
            continue
        started = getattr(sess, "started_at", None) or datetime.now(timezone.utc)
        row = by_id.get(sess.session_id)
        if row is None:
            row = RaceSession(
                id=sess.session_id,
                started_at=started,
                ended_at=getattr(sess, "ended_at", None),
                session_date=lab["session_date"],
                session_number=lab["session_number"],
            )
            db.add(row)
            by_id[sess.session_id] = row
        else:
            row.session_date = lab["session_date"]
            row.session_number = lab["session_number"]
            if row.started_at is None:
                row.started_at = started
            ended = getattr(sess, "ended_at", None)
            if ended is not None and row.ended_at is None:
                row.ended_at = ended

    # 即時進行中的場次不在歸檔列表：補回一個接著的節號，避免面板被洗掉
    if live_id and live_id not in keep_ids:
        live = await db.get(RaceSession, live_id)
        if live is not None and live.session_date in dates:
            day_max = max(
                (
                    lab["session_number"]
                    for lab in labels.values()
                    if lab["session_date"] == live.session_date
                ),
                default=0,
            )
            live.session_number = day_max + 1

    await db.commit()
    logger.info(
        "synced display labels to SQLite: %d session(s) across %d day(s)",
        len(labels),
        len(dates),
    )


async def backfill_numbers_for_sessions(
    db: AsyncSession,
    sessions: list,
    *,
    tz_name: str,
) -> dict[str, dict]:
    """場次列表用：算節次標籤（給 template），並 sync 進 SQLite。

    回傳 dict[session_id → {session_number, session_date}]，template 用
    ``numbering[sid].session_number``。
    """
    if not sessions:
        return {}

    labels = compute_display_labels(sessions, tz_name=tz_name)
    try:
        await sync_numbers_from_labels(db, sessions, labels)
    except Exception:
        logger.exception(
            "sync_numbers_from_labels failed; display labels still returned"
        )
        try:
            await db.rollback()
        except Exception:
            pass
    return labels


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

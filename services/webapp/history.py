"""排行榜、場次瀏覽、單一場次的每一圈展開對照——主要讀 InfluxDB（見
influx_reader.py）。整站需登入（見 auth_gate.py）；InfluxDB 連線失敗時
這些頁面優雅降級成「暫時無法讀取歷史資料」，而不是整頁 500。

場次列表/明細額外查一次 SQLite，把每個 session_id 對應的「今天第幾節」
編號（見 session_numbering.py）標出來給人看；這是純顯示用的補充資訊，
SQLite 查詢失敗一樣不該讓整頁掛掉，只是退回顯示原始 session_id。
"""

from __future__ import annotations

import logging
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.influx_reader import InfluxReader

from . import session_numbering
from .avatars import avatar_url_for
from .deps import get_current_user, get_db
from .models import CarBinding, RaceSession, User, public_display_name
from .template_ctx import template_globals

logger = logging.getLogger(__name__)

router = APIRouter()


class DriverInfo(TypedDict):
    avatar_url: str | None
    driver_name: str | None


def _get_reader(request: Request) -> InfluxReader:
    return request.app.state.influx_reader


async def _binding_lookups(
    db: AsyncSession,
) -> tuple[dict[tuple[str, str], DriverInfo], dict[str, DriverInfo]]:
    """回傳 (by_session_tid, by_tid)。

    by_session_tid: (session_id, transponder_id) → 本節綁定的頭像/名稱
    by_tid: transponder_id → 全站歷史用（同一 TID 多綁定取最新）；
            **本節排行榜不可用 by_tid**，否則會造成跨節「看起來還綁著」。
    """
    by_session_tid: dict[tuple[str, str], DriverInfo] = {}
    by_tid: dict[str, DriverInfo] = {}
    try:
        result = await db.execute(
            select(CarBinding, User)
            .join(User, User.id == CarBinding.user_id)
            .order_by(CarBinding.bound_at.desc())
        )
        for binding, user in result.all():
            tid = (binding.transponder_id or "").upper()
            if not tid:
                continue
            info: DriverInfo = {
                "avatar_url": avatar_url_for(user),
                "driver_name": public_display_name(user),
            }
            by_session_tid[(binding.session_id, tid)] = info
            by_tid.setdefault(tid, info)
    except Exception:
        logger.exception("leaderboard: failed to load avatar bindings from SQLite")
    return by_session_tid, by_tid


def _entry_from_row(
    *,
    car_number: str | None,
    transponder_id: str,
    best_lap_time: float | None,
    time_label: str,
    session_id: str | None,
    driver: DriverInfo | None,
) -> dict:
    return {
        "name": car_number or transponder_id,
        "car_number": car_number or transponder_id,
        "driver_name": (driver or {}).get("driver_name"),
        "avatar_url": (driver or {}).get("avatar_url"),
        "time_label": time_label,
        "transponder_id": transponder_id,
        "best_lap_time": best_lap_time,
        "session_id": session_id,
    }


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    reader = _get_reader(request)
    laptime = request.app.state.templates.env.filters["laptime"]

    entries: list[dict] = []
    session_entries: list[dict] = []
    current_session_id: str | None = None
    influx_unavailable = False
    by_session_tid, by_tid = await _binding_lookups(db)

    try:
        alltime = await reader.get_alltime_best(limit=50)
        entries = [
            _entry_from_row(
                car_number=e.car_number,
                transponder_id=e.transponder_id,
                best_lap_time=e.best_lap_time,
                time_label=laptime(e.best_lap_time),
                session_id=e.session_id,
                # 全站：優先該場次綁定，否則同 TID 最近綁定（歷史紀錄合理）
                driver=by_session_tid.get(
                    (e.session_id, e.transponder_id.upper()),
                    by_tid.get(e.transponder_id.upper()),
                ),
            )
            for e in alltime
        ]

        sessions = await reader.list_sessions(range_start="-1d")
        # 跳過「有歸檔列但沒有任何完成圈」的空殼節（例如 auto_idle 誤歸檔），
        # 否則排行榜會顯示「這節還沒有任何一圈完成的紀錄」。
        for sess in sessions:
            summary = await reader.get_session_summary(sess.session_id)
            candidate = [
                _entry_from_row(
                    car_number=r.car_number,
                    transponder_id=r.transponder_id,
                    best_lap_time=r.best_lap_time,
                    time_label=laptime(r.best_lap_time or None),
                    session_id=sess.session_id,
                    # 本節：只認本節綁定，禁止跨節 by_tid fallback
                    driver=by_session_tid.get(
                        (sess.session_id, r.transponder_id.upper())
                    ),
                )
                for r in summary
                if r.best_lap_time and r.best_lap_time > 0
            ]
            if candidate:
                current_session_id = sess.session_id
                session_entries = candidate
                break
    except Exception:
        logger.exception("leaderboard: failed to read from InfluxDB")
        influx_unavailable = True

    return request.app.state.templates.TemplateResponse(
        request,
        "leaderboard.html",
        template_globals(
            user,
            alltime_entries=entries,
            session_entries=session_entries,
            current_session_id=current_session_id,
            influx_unavailable=influx_unavailable,
        ),
    )


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    reader = _get_reader(request)
    influx_unavailable = False
    sessions = []
    try:
        raw_sessions = await reader.list_sessions(range_start="-90d")
        # 空殼歸檔（best 全 0 / 沒完成圈）不進列表，否則會跟實賽節次混在一起。
        for sess in raw_sessions:
            summary = await reader.get_session_summary(sess.session_id)
            if any(r.best_lap_time and r.best_lap_time > 0 for r in summary):
                sessions.append(sess)
    except Exception:
        logger.exception("sessions: failed to read from InfluxDB")
        influx_unavailable = True

    numbering: dict[str, dict] = {}
    try:
        if sessions:
            tz_name = request.app.state.web_config.display_timezone
            # 直接依 Influx 列表算「第 N 節」，不靠 SQLite（空殼燒掉號也不會退回 sess-…）
            numbering = await session_numbering.backfill_numbers_for_sessions(
                db, sessions, tz_name=tz_name
            )
    except Exception:
        logger.exception("sessions: failed to load/backfill session numbering")

    return request.app.state.templates.TemplateResponse(
        request,
        "sessions.html",
        template_globals(
            user,
            sessions=sessions,
            numbering=numbering,
            influx_unavailable=influx_unavailable,
        ),
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail_page(
    request: Request,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    reader = _get_reader(request)
    try:
        summary = await reader.get_session_summary(session_id)
    except Exception as exc:
        logger.exception("session_detail: failed to read from InfluxDB")
        raise HTTPException(status_code=503, detail="InfluxDB 目前無法連線") from exc

    if not summary:
        raise HTTPException(status_code=404, detail="session not found or empty")

    race_session: RaceSession | None = None
    try:
        race_session = await db.get(RaceSession, session_id)
        if race_session is None or race_session.session_number is None:
            from datetime import datetime, timezone

            started = race_session.started_at if race_session else None
            if started is None:
                try:
                    ts = session_id.removeprefix("sess-")
                    started = datetime.strptime(ts, "%Y%m%d-%H%M%S").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    started = datetime.now(timezone.utc)
            await session_numbering.ensure_session_numbered(session_id, started)
            await db.expire_all()
            race_session = await db.get(RaceSession, session_id)
    except Exception:
        logger.exception("session_detail: failed to read session numbering from SQLite")

    return request.app.state.templates.TemplateResponse(
        request,
        "session_detail.html",
        template_globals(
            user,
            session_id=session_id,
            summary=summary,
            race_session=race_session,
        ),
    )


@router.get("/api/sessions/{session_id}/laps/{transponder_id}")
async def session_lap_history_api(
    request: Request, session_id: str, transponder_id: str
):
    reader = _get_reader(request)
    laptime_filter = request.app.state.templates.env.filters["laptime"]
    try:
        laps = await reader.get_lap_history(session_id, transponder_id.upper())
    except Exception as exc:
        logger.exception("lap_history: failed to read from InfluxDB")
        raise HTTPException(status_code=503, detail="InfluxDB 目前無法連線") from exc

    return JSONResponse(
        {
            "laps": [
                {
                    "lap_number": lap.lap_number,
                    "lap_time": lap.lap_time,
                    "lap_time_label": laptime_filter(lap.lap_time),
                    "recorded_at": lap.recorded_at.isoformat(),
                }
                for lap in laps
            ]
        }
    )

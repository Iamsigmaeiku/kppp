"""排行榜、場次瀏覽、單一場次的每一圈展開對照——主要讀 InfluxDB（見
influx_reader.py）。整站需登入（見 auth_gate.py）；InfluxDB 連線失敗時
這些頁面優雅降級成「暫時無法讀取歷史資料」，而不是整頁 500。

場次列表/明細額外查一次 SQLite，把每個 session_id 對應的「今天第幾節」
編號（見 session_numbering.py）標出來給人看；這是純顯示用的補充資訊，
SQLite 查詢失敗一樣不該讓整頁掛掉，只是退回顯示原始 session_id。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.influx_reader import InfluxReader

from .deps import get_db
from .models import RaceSession

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_reader(request: Request) -> InfluxReader:
    return request.app.state.influx_reader


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    reader = _get_reader(request)
    laptime = request.app.state.templates.env.filters["laptime"]

    entries: list[dict] = []
    session_entries: list[dict] = []
    current_session_id: str | None = None
    influx_unavailable = False

    try:
        alltime = await reader.get_alltime_best(limit=50)
        entries = [
            {
                "name": e.car_number or e.transponder_id,
                "avatar_url": None,
                "time_label": laptime(e.best_lap_time),
                "transponder_id": e.transponder_id,
                "best_lap_time": e.best_lap_time,
                "session_id": e.session_id,
            }
            for e in alltime
        ]

        sessions = await reader.list_sessions(range_start="-1d")
        current_session_id = sessions[0].session_id if sessions else None
        if current_session_id:
            summary = await reader.get_session_summary(current_session_id)
            session_entries = [
                {
                    "name": r.car_number or r.transponder_id,
                    "avatar_url": None,
                    "time_label": laptime(r.best_lap_time or None),
                    "transponder_id": r.transponder_id,
                    "best_lap_time": r.best_lap_time,
                }
                for r in summary
                if r.best_lap_time
            ]
    except Exception:
        logger.exception("leaderboard: failed to read from InfluxDB")
        influx_unavailable = True

    return request.app.state.templates.TemplateResponse(
        request,
        "leaderboard.html",
        {
            "alltime_entries": entries,
            "session_entries": session_entries,
            "current_session_id": current_session_id,
            "influx_unavailable": influx_unavailable,
        },
    )


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, db: AsyncSession = Depends(get_db)):
    reader = _get_reader(request)
    influx_unavailable = False
    sessions = []
    try:
        sessions = await reader.list_sessions(range_start="-90d")
    except Exception:
        logger.exception("sessions: failed to read from InfluxDB")
        influx_unavailable = True

    numbering: dict[str, RaceSession] = {}
    try:
        if sessions:
            result = await db.execute(
                select(RaceSession).where(
                    RaceSession.id.in_([s.session_id for s in sessions])
                )
            )
            numbering = {rs.id: rs for rs in result.scalars().all()}
    except Exception:
        logger.exception("sessions: failed to read session numbering from SQLite")

    return request.app.state.templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "sessions": sessions,
            "numbering": numbering,
            "influx_unavailable": influx_unavailable,
        },
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail_page(
    request: Request, session_id: str, db: AsyncSession = Depends(get_db)
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
    except Exception:
        logger.exception("session_detail: failed to read session numbering from SQLite")

    return request.app.state.templates.TemplateResponse(
        request,
        "session_detail.html",
        {"session_id": session_id, "summary": summary, "race_session": race_session},
    )


@router.get("/api/sessions/{session_id}/laps/{transponder_id}")
async def session_lap_history_api(request: Request, session_id: str, transponder_id: str):
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

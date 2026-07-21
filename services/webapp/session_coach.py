"""AI 教練報告（場次級，不需綁定車號）：場次一結束，系統自動幫這節裡
每一台至少完成一圈的車產生一份報告；任何人瀏覽場次頁都看得到，任何
登入使用者也可以手動幫任一台車觸發（不需要先綁定）。

跟 ai_coach.py（個人綁定制，/profile 用）是兩條並行的路徑，共用
ai_coach_core.py 的 prompt/LLM 呼叫邏輯，資料表各自獨立
（session_ai_coach_reports vs ai_coach_reports）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.influx_reader import InfluxReader
from services.decoder_ingest.lap_tracker import normalize_transponder_id

from .ai_coach_core import (
    PROMPT_VERSION,
    build_user_prompt,
    call_exptech,
    load_laps,
    load_telemetry,
    parse_report_json,
    tids_equivalent,
)
from .config import AiCoachConfig
from .deps import get_current_user, get_db, require_user
from .models import SessionAiCoachReport, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions")

_IN_FLIGHT: set[int] = set()
_IN_FLIGHT_LOCK = asyncio.Lock()


def _report_payload(row: SessionAiCoachReport) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "session_id": row.session_id,
        "transponder_id": row.transponder_id,
        "car_number": row.car_number,
        "triggered_by": row.triggered_by,
        "error_message": row.error_message,
    }
    if row.status == "done" and row.response_json:
        try:
            body["report"] = json.loads(row.response_json)
        except (TypeError, ValueError):
            body["report"] = None
    else:
        body["report"] = None
    return body


async def _run_report_job(
    *,
    app: Any,
    report_id: int,
    session_id: str,
    transponder_id: str,
    car_number: str,
) -> None:
    async with _IN_FLIGHT_LOCK:
        if report_id in _IN_FLIGHT:
            return
        _IN_FLIGHT.add(report_id)

    session_factory = app.state.session_factory
    ai_config: AiCoachConfig | None = app.state.web_config.ai_coach
    reader: InfluxReader = app.state.influx_reader

    try:
        async with session_factory() as db:
            row = await db.get(SessionAiCoachReport, report_id)
            if row is None:
                return
            row.status = "running"
            await db.commit()

        if ai_config is None:
            raise RuntimeError("AI coach 尚未設定")

        laps = await load_laps(reader, session_id, transponder_id)
        if not laps:
            raise ValueError("這節還沒有任何完整的圈速資料")

        telemetry = await load_telemetry(reader, session_id, transponder_id)
        best_lap_time = min(lap.lap_time for lap in laps)

        user_prompt = build_user_prompt(
            car_number=car_number,
            driver_name=car_number,  # 場次級報告沒有個別駕駛身分，用車號代稱
            best_lap_time=best_lap_time,
            laps=laps,
            telemetry=telemetry,
        )
        content = await call_exptech(ai_config, user_prompt)
        report = parse_report_json(content)
        model_name = ai_config.auto_chat_model or ai_config.default_model

        async with session_factory() as db:
            row = await db.get(SessionAiCoachReport, report_id)
            if row is None:
                return
            row.status = "done"
            row.model = model_name
            row.prompt_version = PROMPT_VERSION
            row.response_json = report.model_dump_json()
            row.error_message = None
            await db.commit()
    except Exception as exc:
        logger.exception(
            "session_coach: background job failed report_id=%s session_id=%s tid=%s",
            report_id,
            session_id,
            transponder_id,
        )
        try:
            async with session_factory() as db:
                row = await db.get(SessionAiCoachReport, report_id)
                if row is not None:
                    row.status = "failed"
                    row.error_message = str(exc)[:500]
                    await db.commit()
        except Exception:
            logger.exception(
                "session_coach: failed to persist error for report_id=%s", report_id
            )
    finally:
        async with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(report_id)


async def _get_latest(
    db: AsyncSession, session_id: str, transponder_id: str
) -> SessionAiCoachReport | None:
    result = await db.execute(
        select(SessionAiCoachReport)
        .where(
            SessionAiCoachReport.session_id == session_id,
            SessionAiCoachReport.transponder_id == transponder_id,
        )
        .order_by(SessionAiCoachReport.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/{session_id}/coach-reports/{transponder_id}")
async def generate_session_report(
    session_id: str,
    transponder_id: str,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """任何登入使用者都能觸發，不檢查是否綁定這台車——場次頁是公開瀏覽的，
    這裡故意不設車號歸屬門檻。"""
    web_config = request.app.state.web_config
    if web_config.ai_coach is None:
        raise HTTPException(
            status_code=503, detail="AI coach 尚未設定（缺少 AI_API_KEY/AI_BASE_URL）"
        )

    tid = transponder_id.strip().upper()

    existing = await db.execute(
        select(SessionAiCoachReport)
        .where(
            SessionAiCoachReport.session_id == session_id,
            SessionAiCoachReport.transponder_id == tid,
            SessionAiCoachReport.status.in_(("pending", "running")),
        )
        .order_by(SessionAiCoachReport.created_at.desc())
        .limit(1)
    )
    in_progress = existing.scalar_one_or_none()
    if in_progress is not None:
        return JSONResponse(status_code=202, content=_report_payload(in_progress))

    reader: InfluxReader = request.app.state.influx_reader
    try:
        laps = await load_laps(reader, session_id, tid)
    except Exception as exc:
        logger.exception(
            "session_coach: failed to read from InfluxDB (session_id=%s tid=%s)",
            session_id,
            tid,
        )
        raise HTTPException(status_code=503, detail="InfluxDB 目前無法連線") from exc

    if not laps:
        raise HTTPException(status_code=404, detail="這節還沒有任何完整的圈速資料")

    car_number = tid
    try:
        summary_rows = await reader.get_session_summary(session_id)
        summary_row = next(
            (r for r in summary_rows if tids_equivalent(r.transponder_id, tid)), None
        )
        if summary_row and summary_row.car_number:
            car_number = summary_row.car_number
    except Exception:
        logger.exception("session_coach: failed to resolve car_number for tid=%s", tid)

    row = SessionAiCoachReport(
        session_id=session_id,
        transponder_id=tid,
        car_number=car_number,
        triggered_by=f"manual:{user.id}",
        model="",
        prompt_version=PROMPT_VERSION,
        response_json="",
        status="pending",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    asyncio.create_task(
        _run_report_job(
            app=request.app,
            report_id=row.id,
            session_id=session_id,
            transponder_id=tid,
            car_number=car_number,
        )
    )

    return JSONResponse(status_code=202, content=_report_payload(row))


@router.get("/{session_id}/coach-reports/{transponder_id}")
async def session_report_status(
    session_id: str,
    transponder_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """不檢查是誰在看——場次頁本來就沒有綁定門檻，任何人都能看已產生的報告。"""
    tid = transponder_id.strip().upper()
    row = await _get_latest(db, session_id, tid)
    if row is None:
        return {"status": "none", "report": None, "error_message": None}
    return _report_payload(row)


async def on_session_archived(app: Any, session_id: str, cars: list[dict]) -> None:
    """session_archived hook 的實作：幫這節裡每一台至少完成一圈的車各自
    建一筆 pending row + 背景產生，不等它們跑完（呼叫端只等這個函式把
    背景工作排進佇列，不會被 LLM 呼叫本身卡住）。
    """
    session_factory = app.state.session_factory
    for car in cars:
        tid = normalize_transponder_id((car.get("transponder_id") or "").upper())
        if not tid:
            continue
        car_number = car.get("car_number") or tid
        try:
            async with session_factory() as db:
                row = SessionAiCoachReport(
                    session_id=session_id,
                    transponder_id=tid,
                    car_number=car_number,
                    triggered_by="auto_session_end",
                    model="",
                    prompt_version=PROMPT_VERSION,
                    response_json="",
                    status="pending",
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                report_id = row.id
        except Exception:
            logger.exception(
                "session_coach: failed to create auto report row session_id=%s tid=%s",
                session_id,
                tid,
            )
            continue

        asyncio.create_task(
            _run_report_job(
                app=app,
                report_id=report_id,
                session_id=session_id,
                transponder_id=tid,
                car_number=car_number,
            )
        )

"""AI 教練報告（個人綁定制）：讀 InfluxDB 裡使用者綁定的那節/那支車的每圈
圈速 + 遙測摘要，呼叫 ExpTech 的 OpenAI 相容 chat/completions API 產生賽後
文字建議。

產生改為 server-side background job：POST 立刻回 pending，離頁後仍會跑完
並寫入 SQLite；回 /profile 可 poll status 或靠 SSR 看到結果。

共用邏輯（prompt 組裝、呼叫 LLM、解析回應）在 ai_coach_core.py，
場次級（不需綁定、場次結束自動產生）的版本見 session_coach.py。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.influx_reader import InfluxReader

from .ai_coach_core import (
    PROMPT_VERSION,
    build_user_prompt,
    call_exptech,
    dump_stored_report,
    has_any_telemetry,
    load_laps,
    load_stored_report,
    load_telemetry,
    parse_report_json,
    tids_equivalent,
)
from .config import AiCoachConfig
from .deps import get_db, require_user
from .models import AiCoachReport, CarBinding, User, public_display_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-coach")

_IN_FLIGHT: set[int] = set()
_IN_FLIGHT_LOCK = asyncio.Lock()


class GenerateRequest(BaseModel):
    session_id: str
    transponder_id: str


def _report_payload(row: AiCoachReport) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "session_id": row.session_id,
        "transponder_id": row.transponder_id,
        "error_message": row.error_message,
        "report": None,
        "has_telemetry": False,
    }
    if row.status == "done" and row.response_json:
        report, has_telemetry = load_stored_report(row.response_json)
        body["report"] = report
        body["has_telemetry"] = has_telemetry
    return body


async def _run_report_job(
    *,
    app: Any,
    report_id: int,
    user_id: int,
    session_id: str,
    transponder_id: str,
    car_number: str,
    driver_name: str,
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
            row = await db.get(AiCoachReport, report_id)
            if row is None:
                return
            row.status = "running"
            await db.commit()

        if ai_config is None:
            raise RuntimeError("AI coach 尚未設定")

        laps = await load_laps(reader, session_id, transponder_id)
        summary_rows = await reader.get_session_summary(session_id)
        if not laps:
            raise ValueError("這節還沒有任何完整的圈速資料")

        telemetry = await load_telemetry(reader, session_id, transponder_id)

        summary_row = next(
            (
                r
                for r in summary_rows
                if r.transponder_id.upper() == transponder_id
                or tids_equivalent(r.transponder_id, transponder_id)
            ),
            None,
        )
        best_lap_time = (
            summary_row.best_lap_time
            if summary_row and summary_row.best_lap_time
            else min(lap.lap_time for lap in laps)
        )
        user_prompt = build_user_prompt(
            car_number=car_number,
            driver_name=driver_name,
            best_lap_time=best_lap_time,
            laps=laps,
            telemetry=telemetry,
        )
        content = await call_exptech(ai_config, user_prompt)
        report = parse_report_json(content)
        model_name = ai_config.auto_chat_model or ai_config.default_model

        async with session_factory() as db:
            row = await db.get(AiCoachReport, report_id)
            if row is None:
                return
            row.status = "done"
            row.model = model_name
            row.prompt_version = PROMPT_VERSION
            row.response_json = dump_stored_report(
                report, has_telemetry=has_any_telemetry(telemetry)
            )
            row.error_message = None
            await db.commit()
    except Exception as exc:
        logger.exception(
            "ai_coach: background job failed report_id=%s session_id=%s tid=%s",
            report_id,
            session_id,
            transponder_id,
        )
        try:
            async with session_factory() as db:
                row = await db.get(AiCoachReport, report_id)
                if row is not None:
                    row.status = "failed"
                    row.error_message = str(exc)[:500]
                    await db.commit()
        except Exception:
            logger.exception("ai_coach: failed to persist error for report_id=%s", report_id)
    finally:
        async with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(report_id)


@router.post("/reports")
async def generate_report(
    body: GenerateRequest,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    web_config = request.app.state.web_config
    if web_config.ai_coach is None:
        raise HTTPException(
            status_code=503, detail="AI coach 尚未設定（缺少 AI_API_KEY/AI_BASE_URL）"
        )

    transponder_id = body.transponder_id.strip().upper()

    binding_result = await db.execute(
        select(CarBinding).where(
            CarBinding.user_id == user.id,
            CarBinding.session_id == body.session_id,
            CarBinding.transponder_id == transponder_id,
        )
    )
    binding = binding_result.scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=403, detail="你尚未綁定這節比賽的這支車號")

    # 已有進行中的 job → 直接回傳，不重複開
    existing = await db.execute(
        select(AiCoachReport)
        .where(
            AiCoachReport.user_id == user.id,
            AiCoachReport.session_id == body.session_id,
            AiCoachReport.transponder_id == transponder_id,
            AiCoachReport.status.in_(("pending", "running")),
        )
        .order_by(AiCoachReport.created_at.desc())
        .limit(1)
    )
    in_progress = existing.scalar_one_or_none()
    if in_progress is not None:
        return JSONResponse(status_code=202, content=_report_payload(in_progress))

    reader: InfluxReader = request.app.state.influx_reader
    try:
        laps = await load_laps(reader, body.session_id, transponder_id)
    except Exception as exc:
        logger.exception(
            "ai_coach: failed to read from InfluxDB (session_id=%s transponder_id=%s)",
            body.session_id,
            transponder_id,
        )
        raise HTTPException(status_code=503, detail="InfluxDB 目前無法連線") from exc

    if not laps:
        raise HTTPException(status_code=404, detail="這節還沒有任何完整的圈速資料")

    row = AiCoachReport(
        user_id=user.id,
        session_id=body.session_id,
        transponder_id=transponder_id,
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
            user_id=user.id,
            session_id=body.session_id,
            transponder_id=transponder_id,
            car_number=binding.car_number or transponder_id,
            driver_name=public_display_name(user),
        )
    )

    return JSONResponse(status_code=202, content=_report_payload(row))


@router.get("/reports/status")
async def report_status(
    session_id: str,
    transponder_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tid = transponder_id.strip().upper()
    result = await db.execute(
        select(AiCoachReport)
        .where(
            AiCoachReport.user_id == user.id,
            AiCoachReport.session_id == session_id,
            AiCoachReport.transponder_id == tid,
        )
        .order_by(AiCoachReport.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return {
            "status": "none",
            "report": None,
            "error_message": None,
            "has_telemetry": False,
        }
    return _report_payload(row)

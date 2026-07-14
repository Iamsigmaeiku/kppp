"""AI 教練報告：讀 InfluxDB 裡使用者綁定的那節/那支車的每圈圈速，呼叫
ExpTech 的 OpenAI 相容 chat/completions API 產生賽後文字建議。

產生改為 server-side background job：POST 立刻回 pending，離頁後仍會跑完
並寫入 SQLite；回 /profile 可 poll status 或靠 SSR 看到結果。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.dashboard import get_lap_tracker, get_session_manager
from services.decoder_ingest.influx_reader import InfluxReader, LapRecord
from services.decoder_ingest.lap_tracker import normalize_transponder_id

from .config import AiCoachConfig
from .deps import get_db, require_user
from .models import AiCoachReport, CarBinding, User, public_display_name


def _tids_equivalent(a: str, b: str) -> bool:
    return normalize_transponder_id(a) == normalize_transponder_id(b)


def _laps_from_live_tracker(session_id: str, transponder_id: str) -> list[LapRecord]:
    """本節尚未刷進 Influx / 尚未歸檔時，直接從記憶體 lap_tracker 取圈速。"""
    from datetime import datetime, timezone

    sm = get_session_manager()
    lt = get_lap_tracker()
    if sm is None or lt is None:
        return []
    if sm.current_session_id != session_id:
        return []
    now = datetime.now(timezone.utc)
    for state in lt.all_states():
        tid = state.get("transponder_id") or ""
        if not _tids_equivalent(tid, transponder_id):
            continue
        history = state.get("lap_history") or []
        return [
            LapRecord(lap_number=i + 1, lap_time=float(t), recorded_at=now)
            for i, t in enumerate(history)
            if t and float(t) > 0
        ]
    return []


async def _load_laps(
    reader: InfluxReader, session_id: str, transponder_id: str
) -> list[LapRecord]:
    try:
        laps = await reader.get_lap_history(session_id, transponder_id)
        if laps:
            return laps
    except Exception:
        logger.exception(
            "ai_coach: Influx lap history failed; trying live tracker session_id=%s",
            session_id,
        )
    return _laps_from_live_tracker(session_id, transponder_id)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-coach")

PROMPT_VERSION = "kpp-ai-coach-v1"
_IN_FLIGHT: set[int] = set()
_IN_FLIGHT_LOCK = asyncio.Lock()

SYSTEM_PROMPT = """你是一位卡丁車教練。你的任務是根據每圈圈速資料，產生顧客看得懂的賽後文字建議。

語氣：
- 繁體中文
- 像教練，不像聊天機器人
- 直接指出問題
- 不羞辱顧客
- 給下一輪可執行目標

限制：
- 不要保證成績一定進步
- 不要使用過度專業術語
- 不要提到不存在的感測資料
- 不要編造真實 GPS 走線或彎道資訊
- 不要說正在即時監控，因為這是賽後分析
- 你僅有的資料是每圈圈速（沒有 sector、沒有煞車/GPS/遙測資料），每一點建議
  都必須能從圈速數字本身推導，不可虛構走線、煞車點或感測器數據

只能輸出下面這個 JSON 結構本身，不要加任何其他文字、說明或 markdown code fence：
{
  "summary": "整體表現摘要",
  "strengths": ["做得不錯的地方"],
  "weaknesses": ["可以改進的地方"],
  "next_run_goals": ["下一輪可執行的具體目標"],
  "lap_observations": [
    {"lap_number": 1, "lap_time": 54.2, "delta_to_best": 1.1, "note": "這圈比最佳圈慢了多少、可能代表什麼"}
  ],
  "confidence_score": 80
}
"""


class LapObservation(BaseModel):
    lap_number: int
    lap_time: float
    delta_to_best: float | None = None
    note: str


class AICoachReportSchema(BaseModel):
    summary: str
    strengths: list[str] = []
    weaknesses: list[str] = []
    next_run_goals: list[str] = []
    lap_observations: list[LapObservation] = []
    confidence_score: int


class GenerateRequest(BaseModel):
    session_id: str
    transponder_id: str


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _build_user_prompt(
    *,
    car_number: str,
    driver_name: str,
    best_lap_time: float,
    laps: list,
) -> str:
    average_lap_time = sum(lap.lap_time for lap in laps) / len(laps)
    payload = {
        "car_number": car_number,
        "driver_display_name": driver_name,
        "lap_count": len(laps),
        "best_lap_time": round(best_lap_time, 3),
        "average_lap_time": round(average_lap_time, 3),
        "laps": [
            {
                "lap_number": lap.lap_number,
                "lap_time": round(lap.lap_time, 3),
                "delta_to_session_best": round(lap.lap_time - best_lap_time, 3),
            }
            for lap in laps
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


async def _call_exptech(ai_config: AiCoachConfig, user_prompt: str) -> str:
    """呼叫 ExpTech；主模型空回覆時自動 fallback 到 fast/default。"""
    models: list[str] = []
    for candidate in (
        ai_config.auto_chat_model,
        ai_config.fast_model,
        ai_config.default_model,
        "auto",
    ):
        if candidate and candidate not in models:
            models.append(candidate)

    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=90.0) as client:
        for model in models:
            try:
                response = await client.post(
                    f"{ai_config.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {ai_config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 2000,
                        "temperature": 0.3,
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = _extract_message_content(data)
                if content:
                    if model != models[0]:
                        logger.info(
                            "ai_coach: fell back to model=%s after empty/failed primary",
                            model,
                        )
                    return content
                logger.warning(
                    "ai_coach: empty content from model=%s finish_reason=%s",
                    model,
                    ((data.get("choices") or [{}])[0].get("finish_reason")),
                )
                last_error = ValueError(f"model {model} returned empty content")
            except Exception as exc:
                logger.warning("ai_coach: model=%s failed: %s", model, exc)
                last_error = exc

    raise ValueError(
        f"所有 AI 模型都無法產生內容（tried={models}）：{last_error}"
    )


def _extract_message_content(data: dict) -> str:
    """相容一般 chat 與 reasoning 模型的回覆欄位。"""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    for key in ("content", "reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = choice.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return ""


def _parse_report_json(content: str) -> AICoachReportSchema:
    cleaned = _strip_code_fence(content)
    if not cleaned.lstrip().startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return AICoachReportSchema.model_validate(json.loads(cleaned))


def _report_payload(row: AiCoachReport) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row.id,
        "status": row.status,
        "session_id": row.session_id,
        "transponder_id": row.transponder_id,
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

        laps = await _load_laps(reader, session_id, transponder_id)
        summary_rows = await reader.get_session_summary(session_id)
        if not laps:
            raise ValueError("這節還沒有任何完整的圈速資料")

        summary_row = next(
            (
                r
                for r in summary_rows
                if r.transponder_id.upper() == transponder_id
                or _tids_equivalent(r.transponder_id, transponder_id)
            ),
            None,
        )
        best_lap_time = (
            summary_row.best_lap_time
            if summary_row and summary_row.best_lap_time
            else min(lap.lap_time for lap in laps)
        )
        user_prompt = _build_user_prompt(
            car_number=car_number,
            driver_name=driver_name,
            best_lap_time=best_lap_time,
            laps=laps,
        )
        content = await _call_exptech(ai_config, user_prompt)
        report = _parse_report_json(content)
        model_name = ai_config.auto_chat_model or ai_config.default_model

        async with session_factory() as db:
            row = await db.get(AiCoachReport, report_id)
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
        laps = await _load_laps(reader, body.session_id, transponder_id)
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
        return {"status": "none", "report": None, "error_message": None}
    return _report_payload(row)

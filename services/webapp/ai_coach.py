"""AI 教練報告：讀 InfluxDB 裡使用者綁定的那節/那支車的每圈圈速，呼叫
ExpTech 的 OpenAI 相容 chat/completions API 產生賽後文字建議。

Prompt 語氣/限制沿用 Kart_app/smartkart_app/docs/AI_COACH_PROMPT.md 的教練
人設，但輸出 JSON 的形狀改成 KPP 實際擁有的資料——只有逐圈圈速，沒有
sector/煞車/GPS/遙測，所以拿掉了參考專案裡的 corner_advices 欄位，換成
lap_observations（每圈一則、只能從圈速數字本身推導的觀察），並在 prompt
裡明講這個限制，呼應原始 prompt「不要提到不存在的感測資料」的規範。

定位是賽後分析（不是即時監控），所以每次按「產生報告」都是重新呼叫一次
API 並新增一筆紀錄，不做去重快取——教練報告本來就可能隨呼叫產生不同措辭，
使用者自己決定要不要重新產生。
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.influx_reader import InfluxReader

from .config import AiCoachConfig
from .deps import get_db, require_user
from .models import AiCoachReport, CarBinding, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-coach")

PROMPT_VERSION = "kpp-ai-coach-v1"

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
    model = ai_config.auto_chat_model or ai_config.default_model
    async with httpx.AsyncClient(timeout=30.0) as client:
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
                "max_tokens": 1200,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]


@router.post("/reports")
async def generate_report(
    body: GenerateRequest,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
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

    reader: InfluxReader = request.app.state.influx_reader
    try:
        laps = await reader.get_lap_history(body.session_id, transponder_id)
        summary_rows = await reader.get_session_summary(body.session_id)
    except Exception as exc:
        logger.exception(
            "ai_coach: failed to read from InfluxDB (session_id=%s transponder_id=%s)",
            body.session_id,
            transponder_id,
        )
        raise HTTPException(status_code=503, detail="InfluxDB 目前無法連線") from exc

    if not laps:
        raise HTTPException(status_code=404, detail="這節還沒有任何完整的圈速資料")

    summary_row = next(
        (r for r in summary_rows if r.transponder_id == transponder_id), None
    )
    best_lap_time = (
        summary_row.best_lap_time
        if summary_row and summary_row.best_lap_time
        else min(lap.lap_time for lap in laps)
    )

    user_prompt = _build_user_prompt(
        car_number=binding.car_number or transponder_id,
        driver_name=user.display_name or user.email,
        best_lap_time=best_lap_time,
        laps=laps,
    )

    try:
        content = await _call_exptech(web_config.ai_coach, user_prompt)
        report = AICoachReportSchema.model_validate(
            json.loads(_strip_code_fence(content))
        )
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        logger.exception(
            "ai_coach: report generation failed (session_id=%s transponder_id=%s)",
            body.session_id,
            transponder_id,
        )
        raise HTTPException(status_code=502, detail=f"AI 教練報告產生失敗：{exc}") from exc

    model_name = web_config.ai_coach.auto_chat_model or web_config.ai_coach.default_model
    db.add(
        AiCoachReport(
            user_id=user.id,
            session_id=body.session_id,
            transponder_id=transponder_id,
            model=model_name,
            prompt_version=PROMPT_VERSION,
            response_json=report.model_dump_json(),
        )
    )
    await db.commit()

    return report.model_dump()

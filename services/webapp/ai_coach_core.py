"""AI 教練共用邏輯：prompt 組裝、呼叫 ExpTech、回應解析。

被兩條路徑共用：
  - ai_coach.py：個人綁定制（使用者需先綁定車號才能觸發，結果只有自己看得到）
  - session_coach.py：場次級（不需綁定，場次一結束自動幫每台完成圈的車產生，
    任何人瀏覽場次頁都看得到）

兩邊都吃同一份 SYSTEM_PROMPT / schema / LLM 呼叫邏輯，避免各自維護一份、
行為漂移。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

from services.decoder_ingest.dashboard import get_lap_tracker, get_session_manager
from services.decoder_ingest.influx_reader import InfluxReader, LapRecord
from services.decoder_ingest.lap_tracker import normalize_transponder_id

from .config import AiCoachConfig

if TYPE_CHECKING:
    from services.decoder_ingest.influx_reader import LapTelemetrySummary

logger = logging.getLogger(__name__)


def tids_equivalent(a: str, b: str) -> bool:
    return normalize_transponder_id(a) == normalize_transponder_id(b)


def laps_from_live_tracker(session_id: str, transponder_id: str) -> list[LapRecord]:
    """本節尚未刷進 Influx / 尚未歸檔時，直接從記憶體 lap_tracker 取圈速。"""
    sm = get_session_manager()
    lt = get_lap_tracker()
    if sm is None or lt is None:
        return []
    if sm.current_session_id != session_id:
        return []
    now = datetime.now(timezone.utc)
    for state in lt.all_states():
        tid = state.get("transponder_id") or ""
        if not tids_equivalent(tid, transponder_id):
            continue
        history = state.get("lap_history") or []
        return [
            LapRecord(lap_number=i + 1, lap_time=float(t), recorded_at=now)
            for i, t in enumerate(history)
            if t and float(t) > 0
        ]
    return []


async def load_laps(
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
    return laps_from_live_tracker(session_id, transponder_id)


async def load_telemetry(
    reader: InfluxReader, session_id: str, transponder_id: str
) -> list["LapTelemetrySummary"]:
    """遙測是錦上添花，查不到就當作沒有，不擋報告產生（圈速本身才是必要資料）。"""
    try:
        return await reader.get_lap_telemetry_summary(session_id, transponder_id)
    except Exception:
        logger.exception(
            "ai_coach: telemetry summary failed session_id=%s tid=%s",
            session_id,
            transponder_id,
        )
        return []

PROMPT_VERSION = "kpp-ai-coach-v2-telemetry"

# 場次一結束可能有 8~12 台車同時觸發，限制同時對 ExpTech 的併發呼叫數，
# 避免瞬間炸出一堆併發請求。個人路徑跟場次路徑共用同一個 semaphore。
_LLM_SEMAPHORE = asyncio.Semaphore(3)

SYSTEM_PROMPT = """你是一位卡丁車教練。你的任務是根據每圈圈速（若有提供，還有每圈的速度/G力遙測摘要），產生顧客看得懂的賽後文字建議。

語氣：
- 繁體中文
- 像教練，不像聊天機器人
- 直接指出問題
- 不羞辱顧客
- 給下一輪可執行目標

限制：
- 不要保證成績一定進步
- 不要使用過度專業術語
- 不要說正在即時監控，因為這是賽後分析
- 每一點建議都必須能從提供的數字本身推導，不可虛構走線、彎道位置或任何
  沒有出現在資料裡的細節
- 每圈的遙測欄位（avg_speed_mps/max_speed_mps/max_lat_g/max_brake_g/
  brake_event_count）若為 null，代表這圈沒有遙測重疊，只能講這圈的圈速，
  不可以假裝有煞車/速度/G力資料
- 有遙測資料的圈，可以具體引用數字（例如「這圈煞車次數比較多、平均時速卻
  沒有比較快，可能煞車點太保守」），但不可以無中生有彎道編號或走線描述

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


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def build_user_prompt(
    *,
    car_number: str,
    driver_name: str,
    best_lap_time: float,
    laps: list["LapRecord"],
    telemetry: list["LapTelemetrySummary"] | None = None,
) -> str:
    average_lap_time = sum(lap.lap_time for lap in laps) / len(laps)
    telemetry_by_lap = {t.lap_number: t for t in (telemetry or [])}

    lap_rows = []
    for lap in laps:
        row: dict[str, Any] = {
            "lap_number": lap.lap_number,
            "lap_time": round(lap.lap_time, 3),
            "delta_to_session_best": round(lap.lap_time - best_lap_time, 3),
        }
        t = telemetry_by_lap.get(lap.lap_number)
        row["avg_speed_mps"] = _round_or_none(t.avg_speed_mps if t else None)
        row["max_speed_mps"] = _round_or_none(t.max_speed_mps if t else None)
        row["max_lat_g"] = _round_or_none(t.max_lat_g if t else None, 2)
        row["max_brake_g"] = _round_or_none(t.max_brake_g if t else None, 2)
        row["brake_event_count"] = t.brake_event_count if t else None
        lap_rows.append(row)

    has_any_telemetry = any(
        row["avg_speed_mps"] is not None for row in lap_rows
    )
    payload = {
        "car_number": car_number,
        "driver_display_name": driver_name,
        "lap_count": len(laps),
        "best_lap_time": round(best_lap_time, 3),
        "average_lap_time": round(average_lap_time, 3),
        "has_telemetry_for_any_lap": has_any_telemetry,
        "laps": lap_rows,
    }
    return json.dumps(payload, ensure_ascii=False)


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


async def call_exptech(ai_config: AiCoachConfig, user_prompt: str) -> str:
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
    async with _LLM_SEMAPHORE:
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

    raise ValueError(f"所有 AI 模型都無法產生內容（tried={models}）：{last_error}")


def parse_report_json(content: str) -> AICoachReportSchema:
    cleaned = _strip_code_fence(content)
    if not cleaned.lstrip().startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return AICoachReportSchema.model_validate(json.loads(cleaned))

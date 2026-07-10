"""ai_coach.py 的純函式/schema：fence 去除、prompt 組裝、輸出 schema 驗證。
不呼叫真正的 ExpTech API。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.webapp.ai_coach import (
    AICoachReportSchema,
    _build_user_prompt,
    _strip_code_fence,
)


class _FakeLap:
    def __init__(self, lap_number: int, lap_time: float) -> None:
        self.lap_number = lap_number
        self.lap_time = lap_time


def test_strip_code_fence_removes_json_fence():
    text = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_passthrough_when_no_fence():
    text = '{"a": 1}'
    assert _strip_code_fence(text) == '{"a": 1}'


def test_build_user_prompt_shape():
    laps = [_FakeLap(1, 55.0), _FakeLap(2, 53.5)]
    prompt = _build_user_prompt(
        car_number="42", driver_name="Alice", best_lap_time=53.5, laps=laps
    )
    payload = json.loads(prompt)

    assert payload["car_number"] == "42"
    assert payload["lap_count"] == 2
    assert payload["laps"][0]["delta_to_session_best"] == pytest.approx(1.5)
    assert payload["laps"][1]["delta_to_session_best"] == pytest.approx(0.0)


def test_ai_coach_report_schema_rejects_missing_confidence_score():
    with pytest.raises(ValidationError):
        AICoachReportSchema.model_validate({"summary": "ok"})


def test_ai_coach_report_schema_accepts_minimal_valid_payload():
    report = AICoachReportSchema.model_validate(
        {"summary": "整體表現不錯", "confidence_score": 70}
    )
    assert report.strengths == []
    assert report.lap_observations == []

"""ai_coach_core.py 的純函式/schema：fence 去除、prompt 組裝、輸出 schema
驗證。不呼叫真正的 ExpTech API。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.webapp.ai_coach_core import (
    AICoachReportSchema,
    _extract_message_content,
    _strip_code_fence,
    build_user_prompt,
    parse_report_json,
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
    prompt = build_user_prompt(
        car_number="42", driver_name="Alice", best_lap_time=53.5, laps=laps
    )
    payload = json.loads(prompt)

    assert payload["car_number"] == "42"
    assert payload["lap_count"] == 2
    assert payload["laps"][0]["delta_to_session_best"] == pytest.approx(1.5)
    assert payload["laps"][1]["delta_to_session_best"] == pytest.approx(0.0)
    assert payload["has_telemetry_for_any_lap"] is False
    assert payload["laps"][0]["avg_speed_mps"] is None


def test_build_user_prompt_includes_telemetry_when_present():
    from services.decoder_ingest.influx_reader import LapTelemetrySummary

    laps = [_FakeLap(1, 55.0), _FakeLap(2, 53.5)]
    telemetry = [
        LapTelemetrySummary(
            lap_number=1,
            avg_speed_mps=12.3,
            max_speed_mps=16.5,
            max_lat_g=1.8,
            max_brake_g=1.1,
            brake_event_count=5,
        ),
        LapTelemetrySummary(
            lap_number=2,
            avg_speed_mps=None,
            max_speed_mps=None,
            max_lat_g=None,
            max_brake_g=None,
            brake_event_count=None,
        ),
    ]
    prompt = build_user_prompt(
        car_number="11",
        driver_name="Bob",
        best_lap_time=53.5,
        laps=laps,
        telemetry=telemetry,
    )
    payload = json.loads(prompt)

    assert payload["has_telemetry_for_any_lap"] is True
    assert payload["laps"][0]["avg_speed_mps"] == pytest.approx(12.3)
    assert payload["laps"][0]["brake_event_count"] == 5
    assert payload["laps"][1]["avg_speed_mps"] is None
    assert payload["laps"][1]["brake_event_count"] is None


def test_ai_coach_report_schema_rejects_missing_confidence_score():
    with pytest.raises(ValidationError):
        AICoachReportSchema.model_validate({"summary": "ok"})


def test_ai_coach_report_schema_accepts_minimal_valid_payload():
    report = AICoachReportSchema.model_validate(
        {"summary": "整體表現不錯", "confidence_score": 70}
    )
    assert report.strengths == []
    assert report.lap_observations == []


def test_extract_message_content_prefers_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": '{"summary":"x","confidence_score":1}',
                    "reasoning_content": "thinking...",
                }
            }
        ]
    }
    assert _extract_message_content(data).startswith("{")


def test_extract_message_content_falls_back_to_reasoning():
    data = {
        "choices": [
            {"message": {"content": "", "reasoning_content": '{"summary":"r","confidence_score":1}'}}
        ]
    }
    assert "summary" in _extract_message_content(data)


def test_parse_report_json_extracts_embedded_object():
    raw = '這是說明\n{"summary":"ok","confidence_score":55}\n結尾'
    report = parse_report_json(raw)
    assert report.summary == "ok"
    assert report.confidence_score == 55

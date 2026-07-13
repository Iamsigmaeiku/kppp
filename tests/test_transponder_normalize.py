"""Normalize UID last nibble; first-lap session numbering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.decoder_ingest.lap_tracker import LapTracker, normalize_transponder_id
from services.decoder_ingest.packet_parser import PassingEvent


def test_normalize_transponder_id_collapses_trailing_nibble():
    assert normalize_transponder_id("140211241C78") == "140211241C77"
    assert normalize_transponder_id("140211241c76") == "140211241C77"
    assert normalize_transponder_id("140211241C77") == "140211241C77"
    # 非 6/7/8 尾碼不碰（測試用假 UID）
    assert normalize_transponder_id("AABBCCDDEEFF") == "AABBCCDDEEFF"


def test_77_and_78_share_state_and_car_number():
    tracker = LapTracker(
        noise_threshold_sec=1.0,
        car_number_map={"140211241C77": "18"},
    )
    t0 = datetime(2026, 7, 12, tzinfo=timezone.utc)
    s1 = tracker.record_passing(
        PassingEvent(
            transponder_id="140211241C78",
            raw_payload="140211241C780000",
            received_at=t0,
        )
    )
    assert s1["registered"] is True
    assert s1["car_number"] == "18"
    assert s1["transponder_id"] == "140211241C77"

    s2 = tracker.record_passing(
        PassingEvent(
            transponder_id="140211241C77",
            raw_payload="140211241C770001",
            received_at=t0 + timedelta(seconds=55.0),
        )
    )
    assert s2["lap_count"] == 1
    assert s2["best_lap_time"] == 55.0
    assert len(tracker.all_states()) == 1


def test_load_snapshot_merges_77_78_variants():
    tracker = LapTracker(car_number_map={"140215494F77": "19"})
    tracker.load_snapshot(
        {
            "states": {
                "140215494F78": {
                    "lap_count": 14,
                    "best_lap_time": 51.69,
                    "last_lap_time": 52.0,
                    "lap_history": [51.69],
                    "last_passing_at": None,
                    "last_raw_payload": None,
                    "last_passing_tick": None,
                },
                "140215494F77": {
                    "lap_count": 2,
                    "best_lap_time": 60.0,
                    "last_lap_time": 60.0,
                    "lap_history": [60.0],
                    "last_passing_at": None,
                    "last_raw_payload": None,
                    "last_passing_tick": None,
                },
            }
        }
    )
    states = tracker.all_states()
    assert len(states) == 1
    assert states[0]["transponder_id"] == "140215494F77"
    assert states[0]["lap_count"] == 14
    assert states[0]["car_number"] == "19"

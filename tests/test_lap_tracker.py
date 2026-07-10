"""lap_tracker.py 圈速計算測試：tick/256000 為主路徑；空 Hz 才 fallback wall-clock。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.lap_tracker import LapTracker
from services.decoder_ingest.packet_parser import DECODER_TICK_BYTE_LEN, PassingEvent

TID = "AABBCCDDEEFF"


def _event(*, received_at, decoder_tick=None, tick_byte_len=None):
    return PassingEvent(
        transponder_id=TID,
        raw_payload=TID + "000000000000",
        received_at=received_at,
        decoder_tick=decoder_tick,
        tick_byte_len=tick_byte_len,
    )


def test_wall_clock_when_tick_hz_disabled():
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=None)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(
        _event(received_at=t0, decoder_tick=1000, tick_byte_len=DECODER_TICK_BYTE_LEN)
    )
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=12.345),
            decoder_tick=13000,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    assert state["last_lap_time"] == pytest.approx(12.345, abs=1e-6)


def test_tick_based_lap_time_when_enabled():
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=1000.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(
        _event(received_at=t0, decoder_tick=1000, tick_byte_len=DECODER_TICK_BYTE_LEN)
    )
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=12.0),
            decoder_tick=1000 + 12345,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    assert state["last_lap_time"] == pytest.approx(12.345, abs=1e-6)


def test_pdf_example_tick_delta_over_256000():
    """截圖範例：(0x543C8B3B - 0x53297837) / 256000 = 70.419015625"""
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=256000.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(
        _event(
            received_at=t0,
            decoder_tick=0x53297837,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    state = tracker.record_passing(
        _event(
            # wall-clock 刻意偏一點，確認採用 tick
            received_at=t0 + timedelta(seconds=70.0),
            decoder_tick=0x543C8B3B,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    assert state["last_lap_time"] == pytest.approx(70.419015625, abs=1e-9)


def test_tick_wraparound_32bit():
    tracker = LapTracker(noise_threshold_sec=0.1, decoder_tick_hz=1000.0)
    modulus = 1 << 32
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_tick = modulus - 500
    tracker.record_passing(
        _event(
            received_at=t0,
            decoder_tick=last_tick,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    new_tick = 100
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=0.6),
            decoder_tick=new_tick,
            tick_byte_len=DECODER_TICK_BYTE_LEN,
        )
    )
    expected_delta_ticks = (new_tick - last_tick) % modulus
    assert state["last_lap_time"] == pytest.approx(expected_delta_ticks / 1000.0, abs=1e-6)


def test_tick_and_received_at_disagreement_logs_warning(caplog):
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=1000.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(
        _event(received_at=t0, decoder_tick=0, tick_byte_len=DECODER_TICK_BYTE_LEN)
    )
    with caplog.at_level("WARNING"):
        state = tracker.record_passing(
            _event(
                received_at=t0 + timedelta(seconds=5.0),
                decoder_tick=20000,
                tick_byte_len=DECODER_TICK_BYTE_LEN,
            )
        )
    assert state["last_lap_time"] == pytest.approx(20.0, abs=1e-6)
    assert any("lap_time mismatch" in rec.message for rec in caplog.records)


def test_finalize_in_progress_laps_counts_final_lap():
    tracker = LapTracker(noise_threshold_sec=1.0, max_lap_time_sec=600.0)
    tracker.set_decoder_connected("dec-1", True)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(_event(received_at=t0))
    tracker.record_passing(_event(received_at=t0 + timedelta(seconds=50.0)))

    tracker.finalize_in_progress_laps(at=t0 + timedelta(seconds=50.0 + 42.0))

    state = tracker.all_states()[0]
    assert state["lap_count"] == 2
    assert state["last_lap_time"] == pytest.approx(42.0, abs=1e-6)
    assert state["lap_history"][-1] == pytest.approx(42.0, abs=1e-6)
    assert state["timer_active"] is False


def test_finalize_in_progress_laps_skips_too_short_or_too_long():
    tracker = LapTracker(noise_threshold_sec=10.0, max_lap_time_sec=600.0)
    tracker.set_decoder_connected("dec-1", True)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(_event(received_at=t0))

    tracker.finalize_in_progress_laps(at=t0 + timedelta(seconds=3.0))
    state = tracker.all_states()[0]
    assert state["lap_count"] == 0

    tracker2 = LapTracker(noise_threshold_sec=1.0, max_lap_time_sec=600.0)
    tracker2.set_decoder_connected("dec-1", True)
    tracker2.record_passing(_event(received_at=t0))
    tracker2.finalize_in_progress_laps(at=t0 + timedelta(seconds=700.0))
    state2 = tracker2.all_states()[0]
    assert state2["lap_count"] == 0


def test_finalize_in_progress_laps_noop_without_passing():
    tracker = LapTracker(noise_threshold_sec=1.0)
    tracker.finalize_in_progress_laps()
    assert tracker.all_states() == []

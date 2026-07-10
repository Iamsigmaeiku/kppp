"""lap_tracker.py 圈速計算測試：預設（decoder tick 關閉）行為必須與加入
tick 支援前完全一致；開啟後改用 tick 計算，含 wraparound 與計算分歧警告。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.lap_tracker import LapTracker
from services.decoder_ingest.packet_parser import PassingEvent

TID = "AABBCCDDEEFF"


def _event(*, received_at, decoder_tick=None, tick_byte_len=None):
    return PassingEvent(
        transponder_id=TID,
        raw_payload=TID + "000000000000",
        received_at=received_at,
        decoder_tick=decoder_tick,
        tick_byte_len=tick_byte_len,
    )


def test_default_behavior_unchanged_when_tick_disabled():
    tracker = LapTracker(noise_threshold_sec=1.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(_event(received_at=t0, decoder_tick=1000, tick_byte_len=3))
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=12.345),
            decoder_tick=13000,
            tick_byte_len=3,
        )
    )
    # decoder_tick_hz 未設定：即使事件帶有 decoder_tick，仍完全依 received_at
    # 計算，與加入 tick 支援前的行為位元不差。
    assert state["last_lap_time"] == pytest.approx(12.345, abs=1e-6)


def test_tick_based_lap_time_when_enabled():
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=1000.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(_event(received_at=t0, decoder_tick=1000, tick_byte_len=3))
    # received_at 的間隔刻意跟 tick 算出來的不同，驗證是 tick 那個值被採用。
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=12.0),
            decoder_tick=1000 + 12345,
            tick_byte_len=3,
        )
    )
    assert state["last_lap_time"] == pytest.approx(12.345, abs=1e-6)


def test_tick_wraparound_handled():
    tracker = LapTracker(noise_threshold_sec=0.1, decoder_tick_hz=1000.0)
    modulus = 1 << 24
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_tick = modulus - 500
    tracker.record_passing(_event(received_at=t0, decoder_tick=last_tick, tick_byte_len=3))
    new_tick = 100
    state = tracker.record_passing(
        _event(
            received_at=t0 + timedelta(seconds=0.6),
            decoder_tick=new_tick,
            tick_byte_len=3,
        )
    )
    expected_delta_ticks = (new_tick - last_tick) % modulus
    assert state["last_lap_time"] == pytest.approx(expected_delta_ticks / 1000.0, abs=1e-6)


def test_tick_and_received_at_disagreement_logs_warning(caplog):
    tracker = LapTracker(noise_threshold_sec=1.0, decoder_tick_hz=1000.0)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(_event(received_at=t0, decoder_tick=0, tick_byte_len=3))
    with caplog.at_level("WARNING"):
        state = tracker.record_passing(
            _event(
                received_at=t0 + timedelta(seconds=5.0),
                decoder_tick=20000,
                tick_byte_len=3,
            )
        )
    # tick-based = 20000/1000 = 20.0s；received_at-based = 5.0s；相差 > 0.5s
    # 應記警告，且 tick-based 的值仍被採用。
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

    # 才過 3 秒就結束場次：太短，不該被算成一圈。
    tracker.finalize_in_progress_laps(at=t0 + timedelta(seconds=3.0))
    state = tracker.all_states()[0]
    assert state["lap_count"] == 0

    tracker2 = LapTracker(noise_threshold_sec=1.0, max_lap_time_sec=600.0)
    tracker2.set_decoder_connected("dec-1", True)
    tracker2.record_passing(_event(received_at=t0))
    # 過了快 12 分鐘才結束場次：早就離場，不該被算成一圈。
    tracker2.finalize_in_progress_laps(at=t0 + timedelta(seconds=700.0))
    state2 = tracker2.all_states()[0]
    assert state2["lap_count"] == 0


def test_finalize_in_progress_laps_noop_without_passing():
    tracker = LapTracker(noise_threshold_sec=1.0)
    tracker.finalize_in_progress_laps()
    assert tracker.all_states() == []

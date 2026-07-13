"""all_timers_inactive / has_archivable_results for auto-archive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.decoder_ingest.lap_tracker import LapTracker
from services.decoder_ingest.packet_parser import PassingEvent

TID = "140211084277"


def _event(at: datetime, tid: str = TID) -> PassingEvent:
    return PassingEvent(
        transponder_id=tid,
        raw_payload=tid + "0000",
        received_at=at,
    )


def test_all_timers_inactive_after_timeout():
    tracker = LapTracker(
        noise_threshold_sec=1.0,
        timer_timeout_sec=30.0,
        car_number_map={TID: "17"},
    )
    tracker.set_decoder_connected("dec-1", True)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=5)
    tracker.record_passing(_event(t0))
    tracker.record_passing(_event(t0 + timedelta(seconds=2)))

    # 剛過線：計時仍 active
    assert tracker.all_timers_inactive() is False

    # 模擬時間前進超過 timeout：snapshot 會凍結
    state = tracker._states[TID]
    elapsed, active = tracker._timer_snapshot(
        state, at=t0 + timedelta(seconds=2 + 60)
    )
    assert active is False
    assert elapsed is not None and elapsed >= 60
    assert tracker.all_timers_inactive() is True
    assert tracker.has_archivable_results() is True


def test_empty_tracker_not_inactive_or_archivable():
    tracker = LapTracker()
    assert tracker.all_timers_inactive() is False
    assert tracker.has_archivable_results() is False

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.postprocess.realtime_smoother import process_lap_outputs
from services.postprocess.rts_smoother import SmoothOutput
from services.timing.lap_timer import LapTimer


T0 = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _out(sec: float, y: float, *, gap: bool = False) -> SmoothOutput:
    return SmoothOutput(
        t=T0 + timedelta(seconds=sec),
        lat=0,
        lon=0,
        x_m=5,
        y_m=y,
        speed_mps=10,
        sigma_m=0.1,
        gap=gap,
        vx_mps=0,
        vy_mps=10,
        pps_age_ms=100,
    )


def test_live_outputs_produce_independent_lap() -> None:
    timer = LapTimer((0, 0), (10, 0), forward=(0, 1), min_lap_sec=5)
    first = [_out(0, -5), _out(1, 5)]
    events, previous = process_lap_outputs(timer, first, None)
    assert len(events) == 1
    assert events[0].lap_time is None
    second = [_out(9, -5), _out(10, 5)]
    events, previous = process_lap_outputs(timer, second, previous)
    assert len(events) == 1
    assert events[0].valid
    assert events[0].lap_time == 9.0


def test_live_lap_timer_does_not_bridge_marked_gap() -> None:
    timer = LapTimer((0, 0), (10, 0), forward=(0, 1), min_lap_sec=5)
    events, _ = process_lap_outputs(
        timer, [_out(0, -5), _out(1, 5, gap=True)], None
    )
    assert events == []

from __future__ import annotations

import pytest

from services.timing.lap_timer import LapTimer, TimedState


def _s(t: float, y: float, vy: float, *, sigma: float = 0.05, pit: bool = False):
    return TimedState(
        time_ns=int(t * 1e9),
        x_m=5.0,
        y_m=y,
        vx_mps=0.0,
        vy_mps=vy,
        position_sigma_m=sigma,
        clock_quality="LOCKED",
        pps_age_ms=20.0,
        in_pit=pit,
    )


def test_directed_gate_hermite_crossing_and_lap():
    timer = LapTimer((0, 0), (10, 0), forward=(0, 1), min_lap_sec=5)
    assert timer.update(_s(0, -5, 8), _s(1, 3, 8)) is not None
    result = timer.update(_s(9, -5, 8), _s(10, 3, 8))
    assert result is not None and result.valid
    assert result.lap_time == pytest.approx(9.0, abs=1e-6)
    assert result.uncertainty_ms == pytest.approx(6.25, abs=0.1)


def test_pit_and_reverse_crossing_do_not_trigger():
    timer = LapTimer((0, 0), (10, 0), forward=(0, 1))
    assert timer.update(_s(0, -5, 8, pit=True), _s(1, 3, 8, pit=True)) is None
    assert timer.update(_s(2, 5, -8), _s(3, -3, -8)) is None


def test_low_position_quality_is_invalid_not_fake_precision():
    timer = LapTimer((0, 0), (10, 0), forward=(0, 1), max_uncertainty_ms=50)
    result = timer.update(_s(0, -5, 8, sigma=2.0), _s(1, 3, 8, sigma=2.0))
    assert result is not None
    assert result.valid is False
    assert result.lap_time is None
    assert result.uncertainty_ms > 50


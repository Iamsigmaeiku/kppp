"""Independent directed-gate lap timer using GNSS/INS state trajectories."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimedState:
    time_ns: int
    x_m: float
    y_m: float
    vx_mps: float
    vy_mps: float
    position_sigma_m: float
    clock_quality: str
    pps_age_ms: float | None
    in_pit: bool = False


@dataclass(frozen=True, slots=True)
class LapTiming:
    lap_time: float | None
    crossing_time_ns: int
    source: str
    uncertainty_ms: float
    clock_quality: str
    position_quality: str
    pps_age_ms: float | None
    valid: bool


def _hermite(p0: float, v0: float, p1: float, v1: float, dt: float, u: float) -> float:
    u2, u3 = u * u, u * u * u
    return (
        (2 * u3 - 3 * u2 + 1) * p0
        + (u3 - 2 * u2 + u) * dt * v0
        + (-2 * u3 + 3 * u2) * p1
        + (u3 - u2) * dt * v1
    )


class LapTimer:
    def __init__(
        self,
        gate_a_m: tuple[float, float],
        gate_b_m: tuple[float, float],
        *,
        forward: tuple[float, float],
        min_lap_sec: float = 25.0,
        arm_depth_m: float = 4.0,
        max_uncertainty_ms: float = 100.0,
    ) -> None:
        ax, ay = gate_a_m
        bx, by = gate_b_m
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length <= 0:
            raise ValueError("gate endpoints coincide")
        fn = math.hypot(*forward)
        if fn <= 0:
            raise ValueError("forward vector is zero")
        self.center = ((ax + bx) / 2, (ay + by) / 2)
        self.along = (dx / length, dy / length)
        self.half_width = length / 2
        self.forward = (forward[0] / fn, forward[1] / fn)
        self.normal = self.forward
        self.min_lap_ns = int(min_lap_sec * 1e9)
        self.arm_depth_m = arm_depth_m
        self.max_uncertainty_ms = max_uncertainty_ms
        self.armed = False
        self.last_crossing_ns: int | None = None

    def _distance(self, x: float, y: float) -> float:
        return (x - self.center[0]) * self.normal[0] + (y - self.center[1]) * self.normal[1]

    def _along(self, x: float, y: float) -> float:
        return (x - self.center[0]) * self.along[0] + (y - self.center[1]) * self.along[1]

    def update(self, a: TimedState, b: TimedState) -> LapTiming | None:
        if b.time_ns <= a.time_ns:
            return None
        da, db = self._distance(a.x_m, a.y_m), self._distance(b.x_m, b.y_m)
        if not a.in_pit and da <= -self.arm_depth_m:
            self.armed = True
        if a.in_pit or b.in_pit or not self.armed or not (da <= 0 < db):
            return None
        dt = (b.time_ns - a.time_ns) / 1e9
        va = a.vx_mps * self.normal[0] + a.vy_mps * self.normal[1]
        vb = b.vx_mps * self.normal[0] + b.vy_mps * self.normal[1]
        if max(va, vb) <= 0:
            return None

        lo, hi = 0.0, 1.0
        for _ in range(48):
            mid = (lo + hi) / 2
            dm = _hermite(da, va, db, vb, dt, mid)
            if dm <= 0:
                lo = mid
            else:
                hi = mid
        u = (lo + hi) / 2
        x = _hermite(a.x_m, a.vx_mps, b.x_m, b.vx_mps, dt, u)
        y = _hermite(a.y_m, a.vy_mps, b.y_m, b.vy_mps, dt, u)
        if abs(self._along(x, y)) > self.half_width:
            return None

        crossing = a.time_ns + round(u * (b.time_ns - a.time_ns))
        if self.last_crossing_ns is not None and crossing - self.last_crossing_ns < self.min_lap_ns:
            return None
        normal_speed = max(0.1, abs(_hermite(va, 0.0, vb, 0.0, dt, u)))
        sigma = max(a.position_sigma_m, b.position_sigma_m)
        uncertainty_ms = sigma / normal_speed * 1000.0
        clock = b.clock_quality
        valid_clock = clock in ("LOCKED", "HOLDOVER")
        valid = valid_clock and uncertainty_ms <= self.max_uncertainty_ms
        lap = (
            (crossing - self.last_crossing_ns) / 1e9
            if self.last_crossing_ns is not None
            else None
        )
        self.last_crossing_ns = crossing
        self.armed = False
        return LapTiming(
            lap_time=lap if valid else None,
            crossing_time_ns=crossing,
            source="gnss_ins_hermite",
            uncertainty_ms=uncertainty_ms,
            clock_quality=clock,
            position_quality="valid" if valid else "uncertain",
            pps_age_ms=b.pps_age_ms,
            valid=valid,
        )


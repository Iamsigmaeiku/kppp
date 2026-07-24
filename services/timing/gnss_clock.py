"""ESP monotonic clock -> GNSS UTC model from captured PPS edges."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


class MicrosUnwrapper:
    """Extend a wrapping unsigned hardware counter to a monotonic integer."""

    def __init__(self, bits: int = 32) -> None:
        self.modulus = 1 << bits
        self.half = self.modulus >> 1
        self._last: int | None = None
        self._extended = 0

    def update(self, raw: int) -> int:
        raw %= self.modulus
        if self._last is None:
            self._last = raw
            self._extended = raw
            return self._extended
        delta = (raw - self._last) % self.modulus
        if delta >= self.half:
            raise ValueError("counter moved backwards or sample gap exceeds half-wrap")
        self._extended += delta
        self._last = raw
        return self._extended


@dataclass(frozen=True, slots=True)
class ClockQuality:
    state: str
    samples: int
    drift_ppb: float | None
    residual_ns: float | None
    pps_age_ms: float | None


class GnssClockModel:
    """Linear least-squares PPS model with bounded drift and holdover state."""

    def __init__(
        self,
        *,
        window: int = 32,
        holdover_sec: float = 3.0,
        invalid_sec: float = 30.0,
        max_drift_ppm: float = 200.0,
    ) -> None:
        self._pairs: deque[tuple[int, int]] = deque(maxlen=window)
        self.holdover_us = int(holdover_sec * 1e6)
        self.invalid_us = int(invalid_sec * 1e6)
        self.max_drift = max_drift_ppm * 1e-6
        self._origin_us = 0
        self._origin_ns = 0
        self._ns_per_us = 1000.0
        self._residual_ns: float | None = None

    def observe_pps(self, monotonic_us: int, gnss_utc_ns: int) -> bool:
        if self._pairs and monotonic_us <= self._pairs[-1][0]:
            return False
        self._pairs.append((int(monotonic_us), int(gnss_utc_ns)))
        self._fit()
        return True

    def _fit(self) -> None:
        self._origin_us, self._origin_ns = self._pairs[0]
        if len(self._pairs) == 1:
            self._ns_per_us = 1000.0
            self._residual_ns = 0.0
            return
        xs = [float(x - self._origin_us) for x, _ in self._pairs]
        ys = [float(y - self._origin_ns) for _, y in self._pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 1000.0
        if abs(slope / 1000.0 - 1.0) > self.max_drift:
            # One mis-associated PPS must not corrupt all subsequent IMU time.
            self._pairs.pop()
            if self._pairs:
                self._fit()
            return
        intercept = my - slope * mx
        self._origin_ns += round(intercept)
        self._ns_per_us = slope
        residuals = [
            y - (intercept + slope * x)
            for x, y in zip(xs, ys)
        ]
        self._residual_ns = math.sqrt(sum(r * r for r in residuals) / len(residuals))

    def to_gnss_ns(self, monotonic_us: int) -> int | None:
        if not self._pairs:
            return None
        return self._origin_ns + round((monotonic_us - self._origin_us) * self._ns_per_us)

    def quality(self, now_us: int) -> ClockQuality:
        if not self._pairs:
            return ClockQuality("UNSYNCED", 0, None, None, None)
        age_us = max(0, now_us - self._pairs[-1][0])
        if len(self._pairs) < 3:
            state = "ACQUIRING"
        elif age_us <= self.holdover_us:
            state = "LOCKED"
        elif age_us <= self.invalid_us:
            state = "HOLDOVER"
        else:
            state = "INVALID"
        drift_ppb = (self._ns_per_us / 1000.0 - 1.0) * 1e9
        return ClockQuality(
            state,
            len(self._pairs),
            drift_ppb,
            self._residual_ns,
            age_us / 1000.0,
        )


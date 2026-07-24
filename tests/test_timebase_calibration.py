"""timebase_calibration 純函式測試。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.timebase_calibration import calibrate

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _passings(n: int, lap: float, t0: datetime = T0) -> list[datetime]:
    """n 圈 → n+1 個過線時間（含起點）。"""
    out = [t0]
    for i in range(n):
        out.append(t0 + timedelta(seconds=lap * (i + 1)))
    return out


def test_offset_only():
    gps = _passings(8, 50.0)
    offset = 37.2
    dec = [t + timedelta(seconds=offset) for t in gps]
    r = calibrate(dec, gps)
    assert r.quality in ("good", "marginal")
    assert abs(r.offset_sec - offset) < 0.05
    assert abs(r.drift_sec_per_hour) < 0.5
    assert r.matched_pairs >= 5


def test_with_drift():
    gps = _passings(10, 50.0)
    offset = 12.0
    drift_per_hour = 2.0
    dec = []
    for t in gps:
        hours = (t - gps[0]).total_seconds() / 3600.0
        dec.append(t + timedelta(seconds=offset + drift_per_hour * hours))
    r = calibrate(dec, gps)
    assert r.quality in ("good", "marginal")
    assert abs(r.offset_sec - offset) < 0.2
    assert abs(r.drift_sec_per_hour - drift_per_hour) < 0.5


def test_decoder_missing_one_lap():
    gps = _passings(8, 50.0)
    offset = 5.0
    # decoder 漏掉第 3→4 之間的過線（拿掉 index 3）
    dec_full = [t + timedelta(seconds=offset) for t in gps]
    dec = dec_full[:3] + dec_full[4:]
    r = calibrate(dec, gps)
    assert r.matched_pairs >= 3
    assert r.quality in ("good", "marginal", "failed")
    # 對上的段落 offset 仍應接近
    if r.quality != "failed":
        assert abs(r.offset_sec - offset) < 1.0


def test_gps_missing_one_lap():
    gps_full = _passings(8, 50.0)
    offset = -3.0
    gps = gps_full[:4] + gps_full[5:]
    dec = [t + timedelta(seconds=offset) for t in gps_full]
    r = calibrate(dec, gps)
    assert r.matched_pairs >= 3
    if r.quality != "failed":
        assert abs(r.offset_sec - offset) < 1.0


def test_unrelated_sequences_fail():
    gps = _passings(6, 50.0)
    # decoder 完全不同節奏
    dec = _passings(6, 17.0, t0=T0 + timedelta(hours=3))
    r = calibrate(dec, gps)
    assert r.quality == "failed"
    import math

    assert math.isfinite(r.residual_std_sec)
    assert math.isfinite(r.offset_sec)


def test_too_few_events_failed_finite():
    import math

    r = calibrate([T0], [T0])
    assert r.quality == "failed"
    assert r.matched_pairs == 0
    assert math.isfinite(r.residual_std_sec)

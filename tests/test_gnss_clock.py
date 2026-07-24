from __future__ import annotations

import pytest

from services.timing.gnss_clock import GnssClockModel, MicrosUnwrapper


def test_micros_wrap_is_extended():
    u = MicrosUnwrapper(32)
    assert u.update(0xFFFF_FF00) == 0xFFFF_FF00
    assert u.update(0x0000_0100) == 0x1_0000_0100


def test_micros_backward_is_rejected():
    u = MicrosUnwrapper(32)
    u.update(1000)
    with pytest.raises(ValueError):
        u.update(900)


def test_pps_clock_maps_drift_and_holdover():
    m = GnssClockModel(holdover_sec=2.0, invalid_sec=10.0)
    utc0 = 1_700_000_000_000_000_000
    # ESP clock is +10 ppm fast: 1,000,010 us per true second.
    for i in range(8):
        m.observe_pps(i * 1_000_010, utc0 + i * 1_000_000_000)
    q = m.quality(7 * 1_000_010)
    assert q.state == "LOCKED"
    assert q.residual_ns is not None and q.residual_ns < 10
    assert q.drift_ppb == pytest.approx(-10_000, abs=100)
    assert m.to_gnss_ns(7 * 1_000_010 + 500_005) == pytest.approx(
        utc0 + 7_500_000_000, abs=10
    )
    assert m.quality(7 * 1_000_010 + 3_000_000).state == "HOLDOVER"
    assert m.quality(7 * 1_000_010 + 11_000_000).state == "INVALID"


def test_bad_pps_association_is_not_accepted_into_fit():
    m = GnssClockModel()
    t0 = 1_700_000_000_000_000_000
    for i in range(4):
        m.observe_pps(i * 1_000_000, t0 + i * 1_000_000_000)
    before = m.to_gnss_ns(4_000_000)
    m.observe_pps(4_000_000, t0 + 6_000_000_000)
    assert m.to_gnss_ns(4_000_000) == before

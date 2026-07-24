from __future__ import annotations

import math

from services.webapp.track_coords import enu_to_wgs84, wgs84_to_enu


def test_wgs84_enu_roundtrip() -> None:
    lat, lng, alt = 22.74257, 120.32208, 7.5
    east, north, up = wgs84_to_enu(lat, lng, alt)
    lat2, lng2, alt2 = enu_to_wgs84(east, north, up)
    assert math.isclose(lat2, lat, abs_tol=2e-9)
    assert math.isclose(lng2, lng, abs_tol=2e-9)
    assert math.isclose(alt2, alt, abs_tol=2e-3)


def test_wgs84_axis_orientation() -> None:
    east, north, _ = wgs84_to_enu(22.742404850060208, 120.32183316061305)
    assert east > 0
    assert north > 0

"""accel_dyn / a_lat / a_lon 衍生欄位。"""

from __future__ import annotations

import math

from services.webapp.telemetry import TelemetrySample
from services.webapp.udp_telemetry import _enrich_icm_motion


def test_enrich_icm_motion_basic():
    s = TelemetrySample(ax=0.2, ay=1.0, az=-0.3, accel_mag=math.sqrt(0.2**2 + 1 + 0.09))
    grav = [0.0, 0.0, 0.0]
    out = _enrich_icm_motion(s, grav)
    assert out.a_lon is not None and out.a_lat is not None and out.accel_dyn is not None
    # 首次把 grav 設成當前 a，dyn ≈ 0
    assert out.accel_dyn == 0.0
    assert grav == [0.2, 1.0, -0.3]

    s2 = TelemetrySample(ax=0.5, ay=1.0, az=0.0)
    out2 = _enrich_icm_motion(s2, grav)
    assert out2.a_lon != 0.0 or out2.a_lat != 0.0
    assert out2.accel_dyn > 0.0


def test_enrich_without_grav_fallback():
    s = TelemetrySample(ax=0.4, ay=1.1, az=-0.2)
    out = _enrich_icm_motion(s, None)
    assert out.a_lon == 0.4
    assert out.a_lat == -0.2
    assert out.accel_dyn == math.sqrt(0.4**2 + 0.1**2 + 0.2**2)

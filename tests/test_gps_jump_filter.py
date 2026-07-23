"""GPS 跳點剔除單元測試。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI

from services.webapp.udp_telemetry import (
    GPS_JUMP_RESET_STREAK,
    GPS_MAX_SPEED_MPS,
    UdpTelemetryServer,
    _haversine_m,
)


def _make_server() -> UdpTelemetryServer:
    app = FastAPI()
    app.state = SimpleNamespace(influx_reader=None, telemetry_by_device={})
    return UdpTelemetryServer(
        app, host="127.0.0.1", port=0, device_id="test-dev"
    )


def _offset_lat(lat: float, north_m: float) -> float:
    """近似：1° lat ≈ 111320 m。"""
    return lat + north_m / 111_320.0


def test_jump_filter_accepts_normal_speed():
    """正常行駛（speed < 45 m/s）— 全部接受。"""
    srv = _make_server()
    lat0, lon0, t0 = 25.0, 121.0, 1000.0
    assert srv._accept_gps_jump(lat0, lon0, t0) is True

    # 1 秒移動 20 m → 20 m/s
    lat1 = _offset_lat(lat0, 20.0)
    assert srv._accept_gps_jump(lat1, lon0, t0 + 1.0) is True
    assert srv._rejected_streak == 0
    assert srv._last_accepted_gps == (lat1, lon0, t0 + 1.0)

    lat2 = _offset_lat(lat1, 30.0)  # 再 30 m / 1s = 30 m/s
    assert srv._accept_gps_jump(lat2, lon0, t0 + 2.0) is True
    assert srv._rejected_streak == 0


def test_jump_filter_rejects_single_flyer():
    """單一飛點（speed > 45 m/s）— 拒收，streak=1。"""
    srv = _make_server()
    lat0, lon0, t0 = 25.0, 121.0, 1000.0
    assert srv._accept_gps_jump(lat0, lon0, t0) is True

    # 1 秒飛 100 m → 100 m/s > 45
    lat_fly = _offset_lat(lat0, 100.0)
    dist = _haversine_m(lat0, lon0, lat_fly, lon0)
    assert dist / 1.0 > GPS_MAX_SPEED_MPS

    assert srv._accept_gps_jump(lat_fly, lon0, t0 + 1.0) is False
    assert srv._rejected_streak == 1
    # 基準未更新
    assert srv._last_accepted_gps == (lat0, lon0, t0)


def test_jump_filter_resets_after_streak():
    """連續 5 筆飛點後重置 — 第 6 筆被接受，基準更新。"""
    srv = _make_server()
    lat0, lon0, t0 = 25.0, 121.0, 1000.0
    assert srv._accept_gps_jump(lat0, lon0, t0) is True

    # 連續 5 筆飛點（相對基準 lat0 各飛 200m，時間各 +1s）
    last_fly = None
    for i in range(1, GPS_JUMP_RESET_STREAK + 1):
        lat_fly = _offset_lat(lat0, 200.0 * i)
        last_fly = lat_fly
        t = t0 + float(i)
        accepted = srv._accept_gps_jump(lat_fly, lon0, t)
        if i < GPS_JUMP_RESET_STREAK:
            assert accepted is False
            assert srv._rejected_streak == i
            assert srv._last_accepted_gps == (lat0, lon0, t0)
        else:
            # 第 5 筆：streak 達門檻，重置並接受
            assert accepted is True
            assert srv._rejected_streak == 0
            assert srv._last_accepted_gps == (lat_fly, lon0, t)

    assert last_fly is not None
    # 第 6 筆：相對新基準正常移動 → 接受
    lat6 = _offset_lat(last_fly, 10.0)
    t6 = t0 + float(GPS_JUMP_RESET_STREAK) + 1.0
    assert srv._accept_gps_jump(lat6, lon0, t6) is True
    assert srv._last_accepted_gps == (lat6, lon0, t6)

"""Pixel <-> local meters <-> lat/lng for TKS 橋頭 satellite track image.

Constants must match tools/track_mapping/coord_transform.py and the PNG
served at /webapp-static/tracks/tks_qiaotou_track.png.
"""

from __future__ import annotations

import math
import os

CENTER_LAT = 22.742304850060208
CENTER_LNG = 120.32173316061305
# PNG = Static Maps size=640 zoom=19 scale=2 → physical 1280×1280.
# Same geographic coverage as 640@z19, so physical MPP = z19_mpp / 2 ≈ 0.1377.
MPP = 0.1377
IMG_W, IMG_H = 1280, 1280
CENTER_PX = (IMG_W / 2, IMG_H / 2)

# 起跑線：賽道右側直線「起跑格前方」白色橫線。
# 白線約 y=711、x=925–977；兩端拉到 ~±22m 半寬（對齊 gps_lap_splitter.GATE_HALF_WIDTH_M），
# 吃 GPS 橫漂，避免掠過端點漏切。行進方向：右側直線由南往北。
# 中心 ≈ (45.44, -9.78)；A/B = 中心 ± 22m 沿 +x。
START_GATE_A_M = (23.441, -9.7767)  # 內側（半寬外擴）
START_GATE_B_M = (67.441, -9.7767)  # 外側
GATE_FORWARD_BEARING_DEG = 0.0  # 0° = +y 北
GATE_HALF_WIDTH_M = 22.0

_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)
USE_WGS84_ENU = os.getenv("TRACK_USE_WGS84_ENU", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def px_to_local_m(px: float, py: float) -> tuple[float, float]:
    dx_px = px - CENTER_PX[0]
    dy_px = py - CENTER_PX[1]
    x_m = dx_px * MPP
    y_m = -dy_px * MPP
    return x_m, y_m


def local_m_to_px(x_m: float, y_m: float) -> tuple[float, float]:
    px = CENTER_PX[0] + x_m / MPP
    py = CENTER_PX[1] - y_m / MPP
    return px, py


def _geodetic_to_ecef(lat: float, lng: float, alt_m: float = 0.0) -> tuple[float, float, float]:
    phi = math.radians(lat)
    lam = math.radians(lng)
    sin_phi, cos_phi = math.sin(phi), math.cos(phi)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_phi * sin_phi)
    return (
        (n + alt_m) * cos_phi * math.cos(lam),
        (n + alt_m) * cos_phi * math.sin(lam),
        (n * (1.0 - _WGS84_E2) + alt_m) * sin_phi,
    )


def _ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lng = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - _WGS84_E2))
    alt = 0.0
    for _ in range(10):
        sin_lat = math.sin(lat)
        n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
        alt = p / max(math.cos(lat), 1e-15) - n
        next_lat = math.atan2(z, p * (1.0 - _WGS84_E2 * n / (n + alt)))
        if abs(next_lat - lat) < 1e-13:
            lat = next_lat
            break
        lat = next_lat
    return math.degrees(lat), math.degrees(lng), alt


def wgs84_to_enu(
    lat: float,
    lng: float,
    alt_m: float = 0.0,
    *,
    origin_lat: float = CENTER_LAT,
    origin_lng: float = CENTER_LNG,
    origin_alt_m: float = 0.0,
) -> tuple[float, float, float]:
    """Convert WGS84 geodetic coordinates to a local east/north/up frame."""
    x, y, z = _geodetic_to_ecef(lat, lng, alt_m)
    x0, y0, z0 = _geodetic_to_ecef(origin_lat, origin_lng, origin_alt_m)
    dx, dy, dz = x - x0, y - y0, z - z0
    phi, lam = math.radians(origin_lat), math.radians(origin_lng)
    east = -math.sin(lam) * dx + math.cos(lam) * dy
    north = (
        -math.sin(phi) * math.cos(lam) * dx
        - math.sin(phi) * math.sin(lam) * dy
        + math.cos(phi) * dz
    )
    up = (
        math.cos(phi) * math.cos(lam) * dx
        + math.cos(phi) * math.sin(lam) * dy
        + math.sin(phi) * dz
    )
    return east, north, up


def enu_to_wgs84(
    east_m: float,
    north_m: float,
    up_m: float = 0.0,
    *,
    origin_lat: float = CENTER_LAT,
    origin_lng: float = CENTER_LNG,
    origin_alt_m: float = 0.0,
) -> tuple[float, float, float]:
    """Convert local east/north/up coordinates back to WGS84."""
    phi, lam = math.radians(origin_lat), math.radians(origin_lng)
    dx = (
        -math.sin(lam) * east_m
        - math.sin(phi) * math.cos(lam) * north_m
        + math.cos(phi) * math.cos(lam) * up_m
    )
    dy = (
        math.cos(lam) * east_m
        - math.sin(phi) * math.sin(lam) * north_m
        + math.cos(phi) * math.sin(lam) * up_m
    )
    dz = math.cos(phi) * north_m + math.sin(phi) * up_m
    x0, y0, z0 = _geodetic_to_ecef(origin_lat, origin_lng, origin_alt_m)
    return _ecef_to_geodetic(x0 + dx, y0 + dy, z0 + dz)


def _legacy_local_m_to_latlng(x_m: float, y_m: float) -> tuple[float, float]:
    lat = CENTER_LAT + (y_m / 111320.0)
    lng = CENTER_LNG + (x_m / (111320.0 * math.cos(math.radians(CENTER_LAT))))
    return lat, lng


def _legacy_latlng_to_local_m(lat: float, lng: float) -> tuple[float, float]:
    y_m = (lat - CENTER_LAT) * 111320.0
    x_m = (lng - CENTER_LNG) * (111320.0 * math.cos(math.radians(CENTER_LAT)))
    return x_m, y_m


def local_m_to_latlng(x_m: float, y_m: float) -> tuple[float, float]:
    if USE_WGS84_ENU:
        lat, lng, _ = enu_to_wgs84(x_m, y_m)
        return lat, lng
    return _legacy_local_m_to_latlng(x_m, y_m)


def latlng_to_local_m(lat: float, lng: float) -> tuple[float, float]:
    if USE_WGS84_ENU:
        east, north, _ = wgs84_to_enu(lat, lng)
        return east, north
    return _legacy_latlng_to_local_m(lat, lng)


def latlng_to_px(lat: float, lng: float) -> tuple[float, float]:
    x_m, y_m = latlng_to_local_m(lat, lng)
    return local_m_to_px(x_m, y_m)


def track_js_constants() -> dict[str, float | int]:
    """Constants embedded into live_map.html for client-side projection."""
    return {
        "centerLat": CENTER_LAT,
        "centerLng": CENTER_LNG,
        "mpp": MPP,
        "imgW": IMG_W,
        "imgH": IMG_H,
    }


def start_gate_latlng() -> tuple[tuple[float, float], tuple[float, float]]:
    """起跑線兩端點轉 lat/lng，供 API / 地圖畫線用。"""
    return local_m_to_latlng(*START_GATE_A_M), local_m_to_latlng(*START_GATE_B_M)

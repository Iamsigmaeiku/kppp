"""Pixel <-> local meters <-> lat/lng for TKS 橋頭 satellite track image.

Constants must match tools/track_mapping/coord_transform.py and the PNG
served at /webapp-static/tracks/tks_qiaotou_track.png.
"""

from __future__ import annotations

import math

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


def local_m_to_latlng(x_m: float, y_m: float) -> tuple[float, float]:
    lat = CENTER_LAT + (y_m / 111320.0)
    lng = CENTER_LNG + (x_m / (111320.0 * math.cos(math.radians(CENTER_LAT))))
    return lat, lng


def latlng_to_local_m(lat: float, lng: float) -> tuple[float, float]:
    y_m = (lat - CENTER_LAT) * 111320.0
    x_m = (lng - CENTER_LNG) * (111320.0 * math.cos(math.radians(CENTER_LAT)))
    return x_m, y_m


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

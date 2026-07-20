"""Pixel <-> local meters <-> lat/lng for TKS 橋頭 satellite track image.

Constants must match tools/track_mapping/coord_transform.py and the PNG
served at /webapp-static/tracks/tks_qiaotou_track.png.
"""

from __future__ import annotations

import math

CENTER_LAT = 22.742304850060208
CENTER_LNG = 120.32173316061305
MPP = 0.1377
IMG_W, IMG_H = 1280, 1280
CENTER_PX = (IMG_W / 2, IMG_H / 2)


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

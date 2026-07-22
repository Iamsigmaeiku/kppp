"""Pixel <-> local meters <-> lat/lng for TKS track satellite image."""
from __future__ import annotations

import math

# Must match gettrack.py center used to produce data/tks_qiaotou_track.png
CENTER_LAT = 22.742304850060208
CENTER_LNG = 120.32173316061305
# PNG = Static Maps size=640 zoom=19 scale=2 → physical 1280×1280.
# Same geographic coverage as 640@z19, so physical MPP = z19_mpp / 2 ≈ 0.1377.
MPP = 0.1377
IMG_W, IMG_H = 1280, 1280
CENTER_PX = (IMG_W / 2, IMG_H / 2)


def px_to_local_m(px: float, py: float) -> tuple[float, float]:
    dx_px = px - CENTER_PX[0]
    dy_px = py - CENTER_PX[1]
    x_m = dx_px * MPP
    y_m = -dy_px * MPP
    return x_m, y_m


def local_m_to_latlng(x_m: float, y_m: float) -> tuple[float, float]:
    lat = CENTER_LAT + (y_m / 111320.0)
    lng = CENTER_LNG + (x_m / (111320.0 * math.cos(math.radians(CENTER_LAT))))
    return lat, lng

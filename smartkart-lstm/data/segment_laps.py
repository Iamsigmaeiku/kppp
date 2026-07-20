"""Lap segmentation via start/finish geofence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

EARTH_M_PER_DEG_LAT = 111_320.0


def latlon_to_local_m(
    lat: np.ndarray, lon: np.ndarray, ref_lat: float, ref_lon: float
) -> tuple[np.ndarray, np.ndarray]:
    m_lon = EARTH_M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    x = (lon - ref_lon) * m_lon
    y = (lat - ref_lat) * EARTH_M_PER_DEG_LAT
    return x, y


@dataclass(frozen=True)
class StartLine:
    lat: float
    lon: float
    radius_m: float
    min_lap_sec: float = 35.0
    max_lap_sec: float = 120.0
    debounce_sec: float = 20.0
    confirmed: bool = False


@dataclass
class Lap:
    session_id: str
    device_id: str
    lap_index: int
    start_time: datetime
    end_time: datetime
    lap_time_sec: float
    complete: bool


def start_line_from_cfg(cfg: dict[str, Any]) -> StartLine:
    s = cfg["start_line"]
    return StartLine(
        lat=float(s["lat"]),
        lon=float(s["lon"]),
        radius_m=float(s["radius_m"]),
        min_lap_sec=float(s.get("min_lap_sec", 35.0)),
        max_lap_sec=float(s.get("max_lap_sec", 120.0)),
        debounce_sec=float(s.get("debounce_sec", 20.0)),
        confirmed=bool(s.get("confirmed", False)),
    )


def detect_crossings(
    times: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    line: StartLine,
) -> list[datetime]:
    """Rising-edge entries into the geofence circle, with debounce."""
    x, y = latlon_to_local_m(lat, lon, line.lat, line.lon)
    dist = np.hypot(x, y)
    inside = dist <= line.radius_m
    crossings: list[datetime] = []
    prev = False
    last_ts: datetime | None = None
    for i, now in enumerate(inside):
        if now and not prev:
            ts = times[i]
            # pandas Timestamp -> datetime
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            if last_ts is None or (ts - last_ts).total_seconds() >= line.debounce_sec:
                crossings.append(ts)
                last_ts = ts
        prev = bool(now)
    return crossings


def segment_laps_for_session(
    df: pd.DataFrame,
    *,
    session_id: str,
    device_id: str,
    line: StartLine,
    lat_col: str = "pos_lat",
    lon_col: str = "pos_lon",
) -> list[Lap]:
    """Build complete laps between consecutive geofence crossings.

    First segment (before first proper pair) and incomplete trailing segment
    are discarded / marked incomplete and not returned as training laps.
    """
    g = df.dropna(subset=[lat_col, lon_col]).sort_values("_time")
    if len(g) < 10:
        return []
    times = g["_time"].to_numpy()
    lat = g[lat_col].to_numpy(dtype=float)
    lon = g[lon_col].to_numpy(dtype=float)
    crossings = detect_crossings(times, lat, lon, line)
    if len(crossings) < 2:
        return []

    laps: list[Lap] = []
    lap_i = 0
    for a, b in zip(crossings, crossings[1:]):
        dt = (b - a).total_seconds()
        if dt < line.min_lap_sec or dt > line.max_lap_sec:
            continue
        lap_i += 1
        laps.append(
            Lap(
                session_id=session_id,
                device_id=device_id,
                lap_index=lap_i,
                start_time=a,
                end_time=b,
                lap_time_sec=float(dt),
                complete=True,
            )
        )
    return laps


def segment_all_laps(df: pd.DataFrame, line: StartLine) -> list[Lap]:
    if not line.confirmed:
        raise RuntimeError(
            "start_line.confirmed is false — derive/confirm start line in config.yaml first"
        )
    laps: list[Lap] = []
    for (session_id, device_id), g in df.groupby(["session_id", "device_id"], sort=False):
        laps.extend(
            segment_laps_for_session(
                g, session_id=session_id, device_id=device_id, line=line
            )
        )
    return laps


def derive_start_line_candidate(
    df: pd.DataFrame,
    *,
    lat_col: str = "pos_lat",
    lon_col: str = "pos_lon",
    cell_m: float = 5.0,
) -> dict[str, float]:
    """Density-hotspot candidate (same method used for initial config)."""
    g = df.dropna(subset=[lat_col, lon_col])
    if g.empty:
        raise ValueError("no position points for start-line derivation")
    lat = g[lat_col].to_numpy(dtype=float)
    lon = g[lon_col].to_numpy(dtype=float)
    ref_lat = float(np.median(lat))
    ref_lon = float(np.median(lon))
    x, y = latlon_to_local_m(lat, lon, ref_lat, ref_lon)
    from collections import Counter

    ix = (x // cell_m).astype(int)
    iy = (y // cell_m).astype(int)
    keys = list(zip(ix.tolist(), iy.tolist()))
    (cx, cy), _ = Counter(keys).most_common(1)[0]
    mask = (ix == cx) & (iy == cy)
    return {
        "lat": float(np.mean(lat[mask])),
        "lon": float(np.mean(lon[mask])),
        "radius_m": 15.0,
    }

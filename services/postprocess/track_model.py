"""Versioned track geometry; uncalibrated maps are never production constraints."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRACK_CONSTRAINT_ENABLED = os.getenv("TRACK_CONSTRAINT_ENABLED", "0").lower() in {
    "1", "true", "yes", "on"
}


@dataclass(frozen=True)
class TrackModel:
    version: str
    coordinate_frame: str
    origin_wgs84: tuple[float, float, float]
    centerline_enu: tuple[tuple[float, float], ...]
    boundary_enu: tuple[tuple[float, float], ...]
    pit_lane_enu: tuple[tuple[float, float], ...]
    start_gate_enu: tuple[tuple[float, float], tuple[float, float]]
    calibrated: bool
    calibration_metadata: dict[str, Any]


def _points(value: Any, name: str, minimum: int) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} ENU points")
    try:
        return tuple((float(point[0]), float(point[1])) for point in value)
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"{name} contains an invalid ENU point") from exc


def load_track_model(path: str | Path, *, require_calibrated: bool = True) -> TrackModel:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported track model schema_version")
    calibrated = bool(raw.get("calibrated", False))
    metadata = dict(raw.get("calibration_metadata") or {})
    if require_calibrated and not calibrated:
        raise ValueError("track model is not calibrated; constrained output is forbidden")
    if calibrated and not {"source", "calibrated_at", "horizontal_rms_m"}.issubset(metadata):
        raise ValueError("calibrated track model lacks calibration metadata")
    origin = raw.get("origin_wgs84")
    if not isinstance(origin, list) or len(origin) != 3:
        raise ValueError("origin_wgs84 must be [lat, lon, altitude_m]")
    gate = _points(raw.get("start_gate_enu"), "start_gate_enu", 2)
    if len(gate) != 2:
        raise ValueError("start_gate_enu must have exactly two points")
    return TrackModel(
        version=str(raw.get("coordinate_version", "")),
        coordinate_frame=str(raw.get("coordinate_frame", "")),
        origin_wgs84=(float(origin[0]), float(origin[1]), float(origin[2])),
        centerline_enu=_points(raw.get("centerline_enu"), "centerline_enu", 2),
        boundary_enu=_points(raw.get("boundary_enu"), "boundary_enu", 3),
        pit_lane_enu=_points(raw.get("pit_lane_enu"), "pit_lane_enu", 2),
        start_gate_enu=(gate[0], gate[1]),
        calibrated=calibrated,
        calibration_metadata=metadata,
    )


def constrained_output_allowed(model: TrackModel) -> bool:
    return TRACK_CONSTRAINT_ENABLED and model.calibrated

from __future__ import annotations

import json

import pytest

from services.postprocess.track_model import load_track_model


def _model(calibrated: bool) -> dict:
    return {
        "schema_version": 1,
        "coordinate_version": "tks-2026-01",
        "coordinate_frame": "ENU",
        "origin_wgs84": [22.7, 120.3, 0.0],
        "centerline_enu": [[0, 0], [10, 0], [10, 10]],
        "boundary_enu": [[-2, -2], [12, -2], [12, 12], [-2, 12]],
        "pit_lane_enu": [[2, 1], [8, 1]],
        "start_gate_enu": [[0, -2], [0, 2]],
        "calibrated": calibrated,
        "calibration_metadata": {
            "source": "survey",
            "calibrated_at": "2026-07-24T00:00:00Z",
            "horizontal_rms_m": 0.03,
        },
    }


def test_uncalibrated_track_cannot_be_used(tmp_path) -> None:
    path = tmp_path / "track.json"
    path.write_text(json.dumps(_model(False)), encoding="utf-8")
    with pytest.raises(ValueError, match="not calibrated"):
        load_track_model(path)


def test_versioned_track_model_loads(tmp_path) -> None:
    path = tmp_path / "track.json"
    path.write_text(json.dumps(_model(True)), encoding="utf-8")
    model = load_track_model(path)
    assert model.coordinate_frame == "ENU"
    assert len(model.boundary_enu) == 4

"""Deterministic unit tests for SmartKart GRU pipeline (no live Influx)."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from model.checkpoint_io import StandardScaler, load_checkpoint, save_checkpoint
from model.gru_laptime import LapTimeGRU
from data.build_dataset import resample_prefix, split_by_session
from data.segment_laps import StartLine, detect_crossings, segment_laps_for_session
from incremental_retrain import mix_replay, should_retrain


def _circle_track(n: int = 200, lap_sec: float = 50.0, radius_m: float = 40.0):
    """Synthetic circular track around start line, crossing geofence each lap."""
    line_lat, line_lon = 22.741676, 120.3220
    # start just outside geofence to the east, drive in a circle that passes through origin
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(seconds=i * (lap_sec / n)) for i in range(n * 3)]
    lats, lons = [], []
    # angle 0 at start-line crossing going north
    for i in range(len(times)):
        ang = 2 * math.pi * (i / n)  # 1 lap per n samples
        # circle centered south of start line so it crosses the geofence
        cx_n = -radius_m  # center 40m south
        x = radius_m * math.sin(ang)
        y = cx_n + radius_m * math.cos(ang)
        m_lon = 111320.0 * math.cos(math.radians(line_lat))
        lats.append(line_lat + y / 111320.0)
        lons.append(line_lon + x / m_lon)
    return times, np.array(lats), np.array(lons), line_lat, line_lon


def test_detect_crossings_and_debounce():
    times, lats, lons, la, lo = _circle_track()
    line = StartLine(lat=la, lon=lo, radius_m=8.0, debounce_sec=20.0, confirmed=True)
    crossings = detect_crossings(np.array(times, dtype=object), lats, lons, line)
    assert len(crossings) >= 3


def test_segment_discards_incomplete():
    times, lats, lons, la, lo = _circle_track(n=100, lap_sec=50.0)
    line = StartLine(
        lat=la,
        lon=lo,
        radius_m=8.0,
        min_lap_sec=30.0,
        max_lap_sec=80.0,
        debounce_sec=15.0,
        confirmed=True,
    )
    df = pd.DataFrame(
        {
            "_time": times,
            "pos_lat": lats,
            "pos_lon": lons,
            "session_id": "s1",
            "device_id": "d1",
        }
    )
    laps = segment_laps_for_session(df, session_id="s1", device_id="d1", line=line)
    assert len(laps) >= 1
    for lap in laps:
        assert lap.complete
        assert 30.0 <= lap.lap_time_sec <= 80.0


def test_resample_prefix_shape():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = np.array([t0 + timedelta(seconds=i * 0.1) for i in range(100)])
    values = np.random.randn(100, 4)
    out = resample_prefix(
        times, values, t0=t0, t1=t0 + timedelta(seconds=5), resample_hz=10.0, window_size=50
    )
    assert out is not None
    assert out.shape == (50, 4)


def test_split_by_session_no_leakage():
    meta = pd.DataFrame(
        {
            "session_id": ["a"] * 10 + ["b"] * 10 + ["c"] * 10 + ["d"] * 10 + ["e"] * 10,
            "lap_index": [1] * 50,
        }
    )
    tr, va, mode = split_by_session(meta, val_fraction=0.2, seed=0, min_sessions_for_holdout=4)
    assert mode == "holdout"
    assert set(meta.loc[tr, "session_id"]).isdisjoint(set(meta.loc[va, "session_id"]))


def test_scaler_train_only():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(20, 50, 3)).astype(np.float32)
    # poison val distribution
    X[10:] += 100
    scaler = StandardScaler().fit(X[:10])
    z = scaler.transform(X[:10])
    assert abs(z.mean()) < 1e-5


def test_checkpoint_roundtrip(tmp_path: Path):
    model = LapTimeGRU(input_size=3, hidden_size=16, num_layers=1, dropout=0.1)
    scaler = StandardScaler()
    scaler.fit(np.zeros((4, 50, 3), dtype=np.float32))
    path = tmp_path / "t.pt"
    cfg = {
        "model": {"hidden_size": 16, "num_layers": 1, "dropout": 0.1},
        "dataset": {"window_size": 50},
    }
    save_checkpoint(
        model=model,
        scaler=scaler,
        cfg=cfg,
        feature_names=["a", "b", "c"],
        schema_fingerprint="abc",
        metrics={"x": 1},
        last_source_ts="2026-01-01T00:00:00+00:00",
        path=path,
    )
    m2, s2, ckpt = load_checkpoint(path)
    assert ckpt["schema_fingerprint"] == "abc"
    assert ckpt["feature_names"] == ["a", "b", "c"]
    model.eval()
    m2.eval()
    x = torch.zeros(2, 50, 3)
    with torch.inference_mode():
        y1 = model(x)
        y2 = m2(x)
    assert torch.allclose(y1, y2)


def test_incremental_should_retrain_logic(monkeypatch):
    from incremental_retrain import should_retrain as sr
    import incremental_retrain as mod

    monkeypatch.setattr(mod, "count_points_since", lambda cfg, since: 50)
    ok, n = sr(datetime.now(timezone.utc), cfg={"incremental": {"min_new_samples": 200}})
    assert ok is False and n == 50
    monkeypatch.setattr(mod, "count_points_since", lambda cfg, since: 250)
    ok, n = sr(datetime.now(timezone.utc), cfg={"incremental": {"min_new_samples": 200}})
    assert ok is True


def test_mix_replay_stratified():
    from data.build_dataset import DatasetStats

    N, T, F = 100, 50, 3
    X = np.zeros((N, T, F), dtype=np.float32)
    y = np.ones(N, dtype=np.float32) * 50
    # 80 old, 20 new
    times = []
    sessions = []
    for i in range(N):
        if i < 80:
            times.append("2026-01-01T00:00:00+00:00")
            sessions.append(f"old{i % 4}")
        else:
            times.append("2026-02-01T00:00:00+00:00")
            sessions.append(f"new{i % 2}")
    meta = pd.DataFrame(
        {
            "session_id": sessions,
            "lap_index": [1] * N,
            "end_time": times,
            "device_id": ["d"] * N,
        }
    )
    full = {
        "X": X,
        "y": y,
        "meta": meta,
        "stats": DatasetStats(1, 1, N, (T, F), ["a", "b", "c"], {}),
        "schema_fingerprint": "x",
        "feature_names": ["a", "b", "c"],
        "cfg_snapshot": {},
    }
    mixed = mix_replay(
        full,
        last_trained_ts=datetime(2026, 1, 15, tzinfo=timezone.utc),
        replay_ratio=0.3,
        seed=0,
    )
    assert mixed["stats"].n_windows >= 20
    assert mixed["X"].shape[1:] == (T, F)


def test_warm_start_schema_mismatch(tmp_path: Path):
    model = LapTimeGRU(input_size=3, hidden_size=8)
    scaler = StandardScaler().fit(np.zeros((2, 10, 3), dtype=np.float32))
    path = tmp_path / "c.pt"
    cfg = {"model": {"hidden_size": 8, "num_layers": 1, "dropout": 0.2}, "dataset": {"window_size": 10}}
    save_checkpoint(
        model=model,
        scaler=scaler,
        cfg=cfg,
        feature_names=["a", "b", "c"],
        schema_fingerprint="old",
        metrics={},
        last_source_ts=None,
        path=path,
    )
    _, _, ckpt = load_checkpoint(path)
    assert ckpt["schema_fingerprint"] == "old"

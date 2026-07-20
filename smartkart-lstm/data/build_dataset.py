"""Build partial-lap prefix dataset: start→t resampled to fixed window_size."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Allow `python data/build_dataset.py` from package root or repo root
_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config_util import PKG_ROOT, ensure_dirs, load_config
from data.influx_query import (
    assign_telemetry_sessions,
    merge_asof_position,
    query_position_source,
    query_telemetry,
)
from data.segment_laps import Lap, segment_all_laps, start_line_from_cfg
from model.checkpoint_io import StandardScaler  # noqa: F401 — re-export for tests


@dataclass
class DatasetStats:
    n_sessions: int
    n_laps: int
    n_windows: int
    window_shape: tuple[int, int]
    feature_names: list[str]
    discarded: dict[str, int]


def _schema_fingerprint(cfg: dict) -> str:
    raw = json.dumps(
        {
            "features": cfg["schema"]["features"],
            "fingerprint": cfg["schema"].get("fingerprint"),
            "start_line": {
                k: cfg["start_line"][k]
                for k in ("lat", "lon", "radius_m", "confirmed")
            },
            "window_size": cfg["dataset"]["window_size"],
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def resample_prefix(
    times: np.ndarray,
    values: np.ndarray,
    *,
    t0,
    t1,
    resample_hz: float,
    window_size: int,
) -> np.ndarray | None:
    """Interpolate [t0, t1] onto uniform resample_hz grid, then resize to window_size.

    times: datetime64[ns] or pandas timestamps convertible
    values: (N, F)
    returns (window_size, F) or None if too short / empty
    """
    if len(times) < 2:
        return None
    t_sec = (pd.to_datetime(times, utc=True) - pd.Timestamp(t0)).total_seconds().to_numpy()
    mask = (t_sec >= -0.05) & (t_sec <= (pd.Timestamp(t1) - pd.Timestamp(t0)).total_seconds() + 0.05)
    t_sec = t_sec[mask]
    vals = values[mask]
    if len(t_sec) < 2:
        return None
    duration = float(t_sec[-1] - t_sec[0])
    if duration <= 0:
        return None
    n_uniform = max(2, int(duration * resample_hz) + 1)
    grid = np.linspace(t_sec[0], t_sec[-1], n_uniform)
    # unique times for interp
    order = np.argsort(t_sec)
    t_sec = t_sec[order]
    vals = vals[order]
    uniq_t, uniq_idx = np.unique(t_sec, return_index=True)
    vals = vals[uniq_idx]
    if len(uniq_t) < 2:
        return None
    feat = np.stack(
        [np.interp(grid, uniq_t, vals[:, j]) for j in range(vals.shape[1])],
        axis=1,
    )
    # resize to fixed window via linear index interp
    src_idx = np.linspace(0, len(feat) - 1, window_size)
    out = np.stack(
        [
            np.interp(src_idx, np.arange(len(feat)), feat[:, j])
            for j in range(feat.shape[1])
        ],
        axis=1,
    )
    return out.astype(np.float32)


def build_windows_for_lap(
    lap_df: pd.DataFrame,
    lap: Lap,
    *,
    feature_cols: list[str],
    window_size: int,
    resample_hz: float,
    prefix_stride_sec: float,
    min_prefix_sec: float,
) -> tuple[list[np.ndarray], list[float], list[dict]]:
    """Create (prefix→t, final_lap_time) pairs along the lap."""
    g = lap_df.sort_values("_time").copy()
    for c in feature_cols:
        if c not in g.columns:
            g[c] = np.nan
        g[c] = g[c].ffill().bfill()
    if g[feature_cols].isna().any().any():
        g[feature_cols] = g[feature_cols].fillna(0.0)

    times = g["_time"].to_numpy()
    values = g[feature_cols].to_numpy(dtype=float)
    t0 = pd.Timestamp(lap.start_time)
    t_end = pd.Timestamp(lap.end_time)
    duration = (t_end - t0).total_seconds()
    xs: list[np.ndarray] = []
    ys: list[float] = []
    meta: list[dict] = []

    t = min_prefix_sec
    while t < duration - 0.5:
        t1 = t0 + pd.Timedelta(seconds=t)
        window = resample_prefix(
            times,
            values,
            t0=t0,
            t1=t1,
            resample_hz=resample_hz,
            window_size=window_size,
        )
        if window is not None and np.isfinite(window).all():
            xs.append(window)
            ys.append(float(lap.lap_time_sec))
            meta.append(
                {
                    "session_id": lap.session_id,
                    "device_id": lap.device_id,
                    "lap_index": lap.lap_index,
                    "prefix_sec": float(t),
                    "lap_time_sec": float(lap.lap_time_sec),
                    "start_time": str(lap.start_time),
                    "end_time": str(lap.end_time),
                }
            )
        t += prefix_stride_sec
    return xs, ys, meta


def load_merged_frame(cfg: dict) -> pd.DataFrame:
    tele = query_telemetry(cfg)
    if tele.empty:
        raise SystemExit("no telemetry rows returned from InfluxDB")
    device_ids = cfg["dataset"].get("device_ids")
    if device_ids:
        tele = tele[tele[cfg["schema"]["device_tag"]].isin(device_ids)]
    # Drop probe devices
    tele = tele[~tele[cfg["schema"]["device_tag"]].astype(str).str.startswith("probe")]
    tele = assign_telemetry_sessions(
        tele,
        gap_sec=float(cfg["dataset"]["session_gap_sec"]),
        device_col=cfg["schema"]["device_tag"],
    )
    pos, src = query_position_source(cfg)
    merged = merge_asof_position(
        tele, pos, src, device_col=cfg["schema"]["device_tag"]
    )
    # Prefer fused speed/heading into feature columns when telemetry GPS speed sparse
    if "gps_speed_mps" in merged.columns and "pos_speed" in merged.columns:
        merged["gps_speed_mps"] = merged["gps_speed_mps"].fillna(merged["pos_speed"])
    # Forward-fill sparse GPS onto IMU timeline per device
    dcol = cfg["schema"]["device_tag"]
    for col in ("pos_lat", "pos_lon", "pos_speed", "pos_heading"):
        if col in merged.columns:
            merged[col] = merged.groupby(dcol, sort=False)[col].ffill().bfill()
    return merged


def build_dataset(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    line = start_line_from_cfg(cfg)
    feature_cols = list(cfg["schema"]["features"])
    ds_cfg = cfg["dataset"]

    merged = load_merged_frame(cfg)
    laps = segment_all_laps(merged, line)

    discarded = {
        "sessions_total": int(merged["session_id"].nunique()),
        "laps_complete": len(laps),
        "windows_skipped_short_or_nan": 0,
    }

    all_x: list[np.ndarray] = []
    all_y: list[float] = []
    all_meta: list[dict] = []

    for lap in laps:
        mask = (
            (merged["session_id"] == lap.session_id)
            & (merged["device_id"] == lap.device_id)
            & (merged["_time"] >= pd.Timestamp(lap.start_time))
            & (merged["_time"] <= pd.Timestamp(lap.end_time))
        )
        lap_df = merged.loc[mask]
        xs, ys, meta = build_windows_for_lap(
            lap_df,
            lap,
            feature_cols=feature_cols,
            window_size=int(ds_cfg["window_size"]),
            resample_hz=float(ds_cfg["resample_hz"]),
            prefix_stride_sec=float(ds_cfg["prefix_stride_sec"]),
            min_prefix_sec=float(ds_cfg["min_prefix_sec"]),
        )
        all_x.extend(xs)
        all_y.extend(ys)
        all_meta.extend(meta)

    if not all_x:
        raise SystemExit(
            "no training windows produced — check start_line / data coverage"
        )

    X = np.stack(all_x, axis=0)
    y = np.asarray(all_y, dtype=np.float32)
    meta_df = pd.DataFrame(all_meta)

    stats = DatasetStats(
        n_sessions=int(meta_df["session_id"].nunique()),
        n_laps=int(meta_df.groupby(["session_id", "lap_index"]).ngroups),
        n_windows=int(len(X)),
        window_shape=(int(X.shape[1]), int(X.shape[2])),
        feature_names=feature_cols,
        discarded=discarded,
    )

    out = {
        "X": X,
        "y": y,
        "meta": meta_df,
        "stats": stats,
        "schema_fingerprint": _schema_fingerprint(cfg),
        "feature_names": feature_cols,
        "cfg_snapshot": {
            "features": feature_cols,
            "window_size": ds_cfg["window_size"],
            "start_line": cfg["start_line"],
        },
    }

    print("=== build_dataset ===")
    print(f"sessions : {stats.n_sessions}")
    print(f"laps     : {stats.n_laps}")
    print(f"windows  : {stats.n_windows}")
    print(f"shape    : each window {stats.window_shape}  (full X={X.shape})")
    print(f"features : {feature_cols}")
    print(f"y lap-time sec: min={y.min():.2f} median={np.median(y):.2f} max={y.max():.2f}")
    print(f"schema_fp: {out['schema_fingerprint']}")
    return out


def split_by_session(
    meta: pd.DataFrame,
    *,
    val_fraction: float = 0.2,
    seed: int = 42,
    min_sessions_for_holdout: int = 4,
) -> tuple[np.ndarray, np.ndarray, str]:
    """Return train_idx, val_idx, mode ('holdout'|'loso'|'single')."""
    sessions = sorted(meta["session_id"].unique())
    rng = np.random.default_rng(seed)
    n = len(sessions)
    if n == 1:
        idx = np.arange(len(meta))
        return idx, idx, "single"
    if n < min_sessions_for_holdout:
        # leave-one-session-out: return folds differently; here default last session val
        val_s = sessions[-1]
        train_s = sessions[:-1]
        mode = "loso"
    else:
        n_val = max(1, int(round(n * val_fraction)))
        perm = rng.permutation(sessions)
        val_s = set(perm[:n_val].tolist())
        train_s = [s for s in sessions if s not in val_s]
        mode = "holdout"
        val_s = list(val_s)

    train_idx = meta.index[meta["session_id"].isin(train_s)].to_numpy()
    val_idx = meta.index[meta["session_id"].isin(val_s if isinstance(val_s, list) else [val_s])].to_numpy()
    return train_idx, val_idx, mode


def save_dataset(out: dict, path: Path | None = None) -> Path:
    path = path or (PKG_ROOT / "outputs" / "dataset.npz")
    path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = path.with_suffix(".meta.json")
    np.savez_compressed(
        path,
        X=out["X"],
        y=out["y"],
        feature_names=np.array(out["feature_names"]),
    )
    meta_payload = {
        "meta": out["meta"].to_dict(orient="records"),
        "stats": asdict(out["stats"]),
        "schema_fingerprint": out["schema_fingerprint"],
        "cfg_snapshot": out["cfg_snapshot"],
    }
    # tuples -> lists
    meta_payload["stats"]["window_shape"] = list(meta_payload["stats"]["window_shape"])
    meta_path.write_text(json.dumps(meta_payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"wrote {meta_path}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--save", action="store_true", default=True)
    args = ap.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    out = build_dataset(cfg)
    if args.save:
        save_dataset(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

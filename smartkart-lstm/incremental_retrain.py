"""Batch incremental retrain with InfluxDB replay (no online per-sample updates)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_PKG = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config_util import PKG_ROOT, load_config
from data.build_dataset import build_dataset, save_dataset
from data.influx_query import count_points_since
from model.checkpoint_io import load_checkpoint
from train import train_one


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def should_retrain(
    last_trained_ts: datetime | None,
    *,
    cfg: dict | None = None,
    min_new_samples: int | None = None,
) -> tuple[bool, int]:
    cfg = cfg or load_config()
    threshold = int(
        min_new_samples
        if min_new_samples is not None
        else cfg["incremental"]["min_new_samples"]
    )
    if last_trained_ts is None:
        return True, -1
    # Approximate: raw telemetry points since last train (windows counted after build)
    n = count_points_since(cfg, last_trained_ts)
    return n >= threshold, n


def mix_replay(
    full: dict,
    *,
    last_trained_ts: datetime,
    replay_ratio: float,
    seed: int = 42,
) -> dict:
    """Keep all windows newer than last_trained_ts + stratified historical replay."""
    meta = full["meta"].copy()
    meta["_end"] = pd.to_datetime(meta["end_time"], utc=True)
    cutoff = pd.Timestamp(last_trained_ts)
    new_mask = meta["_end"] > cutoff
    new_idx = meta.index[new_mask].to_numpy()
    old_idx = meta.index[~new_mask].to_numpy()
    if len(new_idx) == 0:
        raise SystemExit("no new windows since last_trained_ts")

    # replay_size = new * ratio / (1 - ratio)
    ratio = float(replay_ratio)
    if ratio <= 0:
        keep = new_idx
    else:
        replay_size = int(len(new_idx) * ratio / max(1e-6, (1.0 - ratio)))
        rng = np.random.default_rng(seed)
        if len(old_idx) == 0:
            keep = new_idx
        else:
            # Stratify by session
            old_meta = meta.loc[old_idx]
            sessions = old_meta["session_id"].unique()
            per = max(1, replay_size // max(1, len(sessions)))
            picked = []
            for s in sessions:
                s_idx = old_meta.index[old_meta["session_id"] == s].to_numpy()
                take = min(len(s_idx), per)
                picked.extend(rng.choice(s_idx, size=take, replace=False).tolist())
            if len(picked) < replay_size:
                remain = list(set(old_idx.tolist()) - set(picked))
                extra = min(len(remain), replay_size - len(picked))
                if extra:
                    picked.extend(rng.choice(remain, size=extra, replace=False).tolist())
            keep = np.concatenate([new_idx, np.asarray(picked, dtype=int)])

    keep = np.unique(keep)
    mixed = {
        "X": full["X"][keep],
        "y": full["y"][keep],
        "meta": meta.loc[keep].drop(columns=["_end"]).reset_index(drop=True),
        "stats": full["stats"],
        "schema_fingerprint": full["schema_fingerprint"],
        "feature_names": full["feature_names"],
        "cfg_snapshot": full["cfg_snapshot"],
    }
    # refresh stats for mixed set
    from data.build_dataset import DatasetStats

    mixed["stats"] = DatasetStats(
        n_sessions=int(mixed["meta"]["session_id"].nunique()),
        n_laps=int(mixed["meta"].groupby(["session_id", "lap_index"]).ngroups),
        n_windows=int(len(mixed["X"])),
        window_shape=(int(mixed["X"].shape[1]), int(mixed["X"].shape[2])),
        feature_names=list(mixed["feature_names"]),
        discarded={"mixed_from_full": True, "new_windows": int(len(new_idx))},
    )
    print(
        f"mixed dataset: new={len(new_idx)} replay~={len(keep) - len(new_idx)} total={len(keep)}"
    )
    return mixed


def retrain(
    checkpoint_path: Path,
    *,
    cfg: dict | None = None,
    force: bool = False,
) -> dict | None:
    cfg = cfg or load_config()
    model, scaler, ckpt = load_checkpoint(checkpoint_path, map_location="cpu")
    del model, scaler  # loaded again inside train_one
    last_ts = parse_ts(ckpt.get("last_source_ts"))
    ok, n = should_retrain(last_ts, cfg=cfg)
    print(f"should_retrain={ok} new_points_approx={n} last_source_ts={last_ts}")
    if not ok and not force:
        print("skip retrain — not enough new samples")
        return None

    full = build_dataset(cfg)
    save_dataset(full)
    if last_ts is not None:
        dataset = mix_replay(
            full,
            last_trained_ts=last_ts,
            replay_ratio=float(cfg["incremental"]["replay_ratio"]),
            seed=int(cfg["train"]["seed"]),
        )
    else:
        dataset = full

    return train_one(
        cfg,
        dataset=dataset,
        warm_start_path=checkpoint_path,
        lr_override=float(cfg["incremental"]["warm_start_lr"]),
        epochs_override=int(cfg["incremental"]["epochs"]),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--checkpoint",
        type=Path,
        default=PKG_ROOT / "checkpoints" / "latest.pt",
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    if not args.checkpoint.exists():
        raise SystemExit(f"missing checkpoint: {args.checkpoint}")
    retrain(args.checkpoint, cfg=cfg, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

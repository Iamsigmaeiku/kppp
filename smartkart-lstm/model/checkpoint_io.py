"""Checkpoint save/load helpers (shared by train + Jetson infer)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

from model.gru_laptime import LapTimeGRU


class StandardScaler:
    def __init__(self) -> None:
        self.mean_: np.ndarray | None = None
        self.std_: np.ndarray | None = None

    def fit(self, x: np.ndarray) -> StandardScaler:
        flat = x.reshape(-1, x.shape[-1])
        self.mean_ = flat.mean(axis=0)
        self.std_ = flat.std(axis=0)
        self.std_ = np.where(self.std_ < 1e-6, 1.0, self.std_)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        assert self.mean_ is not None and self.std_ is not None
        return (x - self.mean_) / self.std_

    def state_dict(self) -> dict:
        return {"mean": self.mean_.tolist(), "std": self.std_.tolist()}

    @classmethod
    def from_state(cls, state: dict) -> StandardScaler:
        obj = cls()
        obj.mean_ = np.asarray(state["mean"], dtype=np.float32)
        obj.std_ = np.asarray(state["std"], dtype=np.float32)
        return obj


def atomic_torch_save(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    tmp.replace(path)


def save_checkpoint(
    *,
    model: LapTimeGRU,
    scaler: StandardScaler,
    cfg: dict,
    feature_names: list[str],
    schema_fingerprint: str,
    metrics: dict,
    last_source_ts: str | None,
    path: Path,
) -> None:
    payload = {
        "model_state": model.state_dict(),
        "model_kwargs": {
            "input_size": len(feature_names),
            "hidden_size": cfg["model"]["hidden_size"],
            "num_layers": cfg["model"]["num_layers"],
            "dropout": cfg["model"]["dropout"],
        },
        "scaler": scaler.state_dict(),
        "feature_names": feature_names,
        "schema_fingerprint": schema_fingerprint,
        "metrics": metrics,
        "last_source_ts": last_source_ts,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cfg_model": cfg["model"],
        "cfg_dataset": {
            "window_size": cfg["dataset"]["window_size"],
            "features": feature_names,
        },
    }
    atomic_torch_save(payload, path)


def load_checkpoint(path: Path, map_location: str | torch.device = "cpu"):
    try:
        ckpt = torch.load(path, map_location=map_location, weights_only=False)
    except TypeError:
        ckpt = torch.load(path, map_location=map_location)
    model = LapTimeGRU(**ckpt["model_kwargs"])
    model.load_state_dict(ckpt["model_state"])
    scaler = StandardScaler.from_state(ckpt["scaler"])
    return model, scaler, ckpt

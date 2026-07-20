"""Train LapTimeGRU with session-grouped split, early stopping, overfit warnings."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

_PKG = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config_util import PKG_ROOT, ensure_dirs, load_config
from data.build_dataset import (
    build_dataset,
    save_dataset,
    split_by_session,
)
from model.checkpoint_io import StandardScaler, load_checkpoint, save_checkpoint
from model.gru_laptime import LapTimeGRU


def set_seed(seed: int) -> None:
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def mae_np(pred: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean(np.abs(pred - y)))


@torch.no_grad()
def evaluate(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[float, float]:
    model.eval()
    losses = []
    preds = []
    ys = []
    loss_fn = nn.L1Loss()
    for xb, yb in loader:
        xb = xb.to(device)
        yb = yb.to(device)
        pred = model(xb)
        losses.append(loss_fn(pred, yb).item())
        preds.append(pred.cpu().numpy())
        ys.append(yb.cpu().numpy())
    pred_a = np.concatenate(preds) if preds else np.array([])
    y_a = np.concatenate(ys) if ys else np.array([])
    return float(np.mean(losses) if losses else 0.0), mae_np(pred_a, y_a)


def train_one(
    cfg: dict,
    *,
    dataset: dict | None = None,
    warm_start_path: Path | None = None,
    lr_override: float | None = None,
    epochs_override: int | None = None,
) -> dict:
    ensure_dirs(cfg)
    set_seed(int(cfg["train"]["seed"]))
    tcfg = cfg["train"]

    if dataset is None:
        dataset = build_dataset(cfg)
        save_dataset(dataset)

    X = dataset["X"]
    y = dataset["y"]
    meta = dataset["meta"].reset_index(drop=True)
    feature_names = list(dataset["feature_names"])
    schema_fp = dataset["schema_fingerprint"]

    train_idx, val_idx, split_mode = split_by_session(
        meta,
        val_fraction=float(tcfg["val_session_fraction"]),
        seed=int(tcfg["seed"]),
        min_sessions_for_holdout=int(tcfg["min_sessions_for_holdout"]),
    )
    print(f"split mode={split_mode} train_n={len(train_idx)} val_n={len(val_idx)}")
    print(f"train sessions={sorted(meta.loc[train_idx, 'session_id'].unique())}")
    print(f"val sessions={sorted(meta.loc[val_idx, 'session_id'].unique())}")

    scaler = StandardScaler().fit(X[train_idx])
    X_train = scaler.transform(X[train_idx])
    X_val = scaler.transform(X[val_idx])
    y_train = y[train_idx]
    y_val = y[val_idx]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device}")

    if warm_start_path is not None:
        model, old_scaler, ckpt = load_checkpoint(warm_start_path, map_location=device)
        if ckpt.get("schema_fingerprint") != schema_fp:
            raise SystemExit(
                f"schema fingerprint mismatch: ckpt={ckpt.get('schema_fingerprint')} data={schema_fp}"
            )
        if ckpt.get("feature_names") != feature_names:
            raise SystemExit("feature_names mismatch — refuse warm start")
        scaler = old_scaler
        X_train = scaler.transform(X[train_idx])
        X_val = scaler.transform(X[val_idx])
        print(f"warm start from {warm_start_path}")
    else:
        model = LapTimeGRU(
            input_size=len(feature_names),
            hidden_size=int(cfg["model"]["hidden_size"]),
            num_layers=int(cfg["model"]["num_layers"]),
            dropout=float(cfg["model"]["dropout"]),
        )
    model.to(device)

    lr = float(lr_override if lr_override is not None else tcfg["lr"])
    epochs = int(epochs_override if epochs_override is not None else tcfg["epochs"])
    opt = torch.optim.Adam(
        model.parameters(), lr=lr, weight_decay=float(tcfg["weight_decay"])
    )
    loss_name = str(tcfg["loss"]).lower()
    loss_fn = nn.L1Loss() if loss_name == "mae" else nn.MSELoss()

    train_loader = DataLoader(
        TensorDataset(
            torch.from_numpy(X_train),
            torch.from_numpy(y_train),
        ),
        batch_size=int(tcfg["batch_size"]),
        shuffle=True,
    )
    val_loader = DataLoader(
        TensorDataset(
            torch.from_numpy(X_val.astype(np.float32)),
            torch.from_numpy(y_val.astype(np.float32)),
        ),
        batch_size=int(tcfg["batch_size"]),
        shuffle=False,
    )

    history = []
    best_val = float("inf")
    best_state = None
    patience = int(tcfg["early_stopping_patience"])
    bad = 0

    for epoch in range(1, epochs + 1):
        model.train()
        train_losses = []
        for xb, yb in train_loader:
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad(set_to_none=True)
            pred = model(xb)
            loss = loss_fn(pred, yb)
            loss.backward()
            opt.step()
            train_losses.append(loss.item())
        train_loss = float(np.mean(train_losses))
        val_loss, val_mae = evaluate(model, val_loader, device)
        # train MAE in-sample
        _, train_mae = evaluate(model, train_loader, device)
        row = {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "train_mae_sec": train_mae,
            "val_mae_sec": val_mae,
        }
        history.append(row)
        print(
            f"epoch {epoch:03d}  train_loss={train_loss:.4f}  val_loss={val_loss:.4f}  "
            f"train_MAE={train_mae:.3f}s  val_MAE={val_mae:.3f}s"
        )
        if val_loss < best_val - 1e-6:
            best_val = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                print(f"early stopping at epoch {epoch} (patience={patience})")
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    _, in_mae = evaluate(model, train_loader, device)
    _, out_mae = evaluate(model, val_loader, device)
    gap = out_mae - in_mae
    ratio = (out_mae / in_mae) if in_mae > 1e-6 else float("inf")

    print("=== final ===")
    print(f"in-sample MAE  : {in_mae:.3f} s")
    print(f"out-of-sample MAE: {out_mae:.3f} s")
    print(f"gap            : {gap:.3f} s  (ratio={ratio:.2f})")
    if split_mode == "single":
        print(
            "WARNING: only 1 session — out-of-sample MAE is NOT trustworthy "
            "(val==train). Do not claim generalization."
        )
        data_enough = False
    else:
        warn = False
        if gap >= float(tcfg["overfit_mae_gap_abs"]) or ratio >= float(
            tcfg["overfit_mae_gap_ratio"]
        ):
            warn = True
            print(
                "WARNING: 疑似 overfit，現有資料量可能不足以支撐此模型 "
                f"(in={in_mae:.3f}s out={out_mae:.3f}s)."
            )
        data_enough = (not warn) and dataset["stats"].n_laps >= 20
        if not data_enough and not warn:
            print(
                f"NOTE: only {dataset['stats'].n_laps} complete laps — "
                "pipeline OK but model not production-ready."
            )
        if data_enough:
            print("Data volume: marginal/OK for pipeline validation, still MOU-scale.")

    # save curves
    out_dir = PKG_ROOT / tcfg["outputs_dir"]
    hist_path = out_dir / "train_history.json"
    hist_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        epochs_x = [h["epoch"] for h in history]
        plt.figure(figsize=(8, 4))
        plt.plot(epochs_x, [h["train_loss"] for h in history], label="train_loss")
        plt.plot(epochs_x, [h["val_loss"] for h in history], label="val_loss")
        plt.xlabel("epoch")
        plt.ylabel("loss")
        plt.legend()
        plt.tight_layout()
        plt.savefig(out_dir / "loss_curve.png", dpi=120)
        plt.close()
        plt.figure(figsize=(8, 4))
        plt.plot(epochs_x, [h["train_mae_sec"] for h in history], label="train_MAE_s")
        plt.plot(epochs_x, [h["val_mae_sec"] for h in history], label="val_MAE_s")
        plt.xlabel("epoch")
        plt.ylabel("MAE (seconds)")
        plt.legend()
        plt.tight_layout()
        plt.savefig(out_dir / "mae_curve.png", dpi=120)
        plt.close()
        print(f"wrote {out_dir / 'loss_curve.png'}")
    except Exception as exc:  # noqa: BLE001
        print(f"plot skipped: {exc}")

    metrics = {
        "in_sample_mae_sec": in_mae,
        "out_of_sample_mae_sec": out_mae,
        "mae_gap_sec": gap,
        "mae_ratio": ratio,
        "split_mode": split_mode,
        "n_windows": int(len(X)),
        "n_laps": int(dataset["stats"].n_laps),
        "n_sessions": int(dataset["stats"].n_sessions),
        "data_enough_for_prod": False,
        "data_enough_for_pipeline_check": dataset["stats"].n_windows >= 50,
    }

    last_source_ts = None
    if len(meta):
        last_source_ts = str(meta["end_time"].max())

    ckpt_dir = PKG_ROOT / tcfg["checkpoint_dir"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    latest = ckpt_dir / "latest.pt"
    stamped = ckpt_dir / f"laptime_gru_{ts}.pt"
    for path in (latest, stamped):
        save_checkpoint(
            model=model,
            scaler=scaler,
            cfg=cfg,
            feature_names=feature_names,
            schema_fingerprint=schema_fp,
            metrics=metrics,
            last_source_ts=last_source_ts,
            path=path,
        )
    print(f"checkpoint: {latest}")
    print(f"checkpoint: {stamped}")

    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return {"metrics": metrics, "history": history, "latest": str(latest)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--warm-start", type=Path, default=None)
    args = ap.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    train_one(cfg, warm_start_path=args.warm_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

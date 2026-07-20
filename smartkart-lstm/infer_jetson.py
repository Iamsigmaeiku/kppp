"""Jetson Orin inference + latency benchmark (pure PyTorch CUDA, no TensorRT)."""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import torch

_PKG = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from config_util import PKG_ROOT, load_config
from model.checkpoint_io import load_checkpoint


def resolve_device(prefer: str) -> torch.device:
    if prefer.startswith("cuda") and torch.cuda.is_available():
        return torch.device(prefer)
    if prefer == "cuda" and not torch.cuda.is_available():
        print("WARNING: CUDA not available — falling back to CPU")
        return torch.device("cpu")
    return torch.device(prefer)


@torch.inference_mode()
def benchmark(
    model: torch.nn.Module,
    x: torch.Tensor,
    *,
    warmup: int,
    n: int,
    device: torch.device,
) -> dict:
    model.eval()
    # warmup
    for _ in range(warmup):
        _ = model(x)
        if device.type == "cuda":
            torch.cuda.synchronize()

    times_ms: list[float] = []
    for _ in range(n):
        if device.type == "cuda":
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        _ = model(x)
        if device.type == "cuda":
            torch.cuda.synchronize()
        times_ms.append((time.perf_counter() - t0) * 1000.0)

    times_ms.sort()
    p95 = times_ms[int(0.95 * (len(times_ms) - 1))]
    return {
        "n": n,
        "warmup": warmup,
        "mean_ms": statistics.mean(times_ms),
        "p50_ms": statistics.median(times_ms),
        "p95_ms": p95,
        "min_ms": times_ms[0],
        "max_ms": times_ms[-1],
    }


def export_onnx_stub(model, dummy: torch.Tensor, path: Path) -> None:
    # TODO(phase 2): 如果要做即時彎道回饋,才需要以下優化
    # torch.onnx.export(
    #     model, dummy_input, "laptime_gru.onnx", opset_version=17,
    #     input_names=["telemetry_window"], output_names=["laptime_pred"],
    #     dynamic_axes={"telemetry_window": {0: "batch"}},
    # )
    # 之後用 trtexec --onnx=laptime_gru.onnx --saveEngine=laptime_gru.engine 建 TensorRT engine
    _ = (model, dummy, path)
    print("ONNX/TensorRT export skipped (phase-2 stub only — GRU RNN ops often painful on TRT)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--checkpoint",
        type=Path,
        default=PKG_ROOT / "checkpoints" / "latest.pt",
    )
    ap.add_argument("--device", default=None)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--warmup", type=int, default=None)
    ap.add_argument("--export-onnx-stub", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    infer_cfg = cfg["infer"]
    device = resolve_device(args.device or infer_cfg["device"])
    warmup = int(args.warmup if args.warmup is not None else infer_cfg["warmup"])
    n = int(args.n if args.n is not None else infer_cfg["benchmark_n"])

    if not args.checkpoint.exists():
        raise SystemExit(f"missing checkpoint: {args.checkpoint}")

    model, scaler, ckpt = load_checkpoint(args.checkpoint, map_location=device)
    model.to(device).eval()

    print("=== infer_jetson ===")
    print(f"checkpoint : {args.checkpoint}")
    print(f"torch      : {torch.__version__}")
    print(f"cuda avail : {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"gpu        : {torch.cuda.get_device_name(0)}")
        print(f"capability : {torch.cuda.get_device_capability(0)}")
    print(f"device     : {device}")
    print(f"features   : {ckpt.get('feature_names')}")
    print(f"schema_fp  : {ckpt.get('schema_fingerprint')}")
    print(f"metrics    : {ckpt.get('metrics')}")

    window = int(cfg["dataset"]["window_size"])
    feat = len(ckpt["feature_names"])
    # synthetic unit-scale window → inverse-ish via zeros mean; just for latency
    raw = np.zeros((1, window, feat), dtype=np.float32)
    scaled = scaler.transform(raw)
    x = torch.from_numpy(scaled).to(device)

    with torch.inference_mode():
        pred = model(x).item()
    print(f"smoke pred : {pred:.3f} s (zeros input — not meaningful)")

    stats = benchmark(model, x, warmup=warmup, n=n, device=device)
    print(
        f"latency    : mean={stats['mean_ms']:.3f} ms  "
        f"p50={stats['p50_ms']:.3f} ms  p95={stats['p95_ms']:.3f} ms  "
        f"(N={stats['n']}, warmup={stats['warmup']}, batch=1)"
    )

    if args.export_onnx_stub:
        export_onnx_stub(model, x, PKG_ROOT / "outputs" / "laptime_gru.onnx")

    out = PKG_ROOT / "outputs" / "infer_benchmark.json"
    import json

    payload = {
        "torch": torch.__version__,
        "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "device": str(device),
        "checkpoint": str(args.checkpoint),
        **stats,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

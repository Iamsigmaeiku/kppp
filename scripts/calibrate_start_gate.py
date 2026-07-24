"""校準 GPS 虛擬起跑線：疊軌跡到賽道 PNG，換算像素→本地 m，並比對 decoder 圈速。

用法：
    python scripts/calibrate_start_gate.py --session-id sess-YYYYMMDD-HHMMSS

    # 指定像素端點（在輸出圖上量白線兩端）與行進方位角：
    python scripts/calibrate_start_gate.py --session-id sess-... \\
        --gate-a-px 900,700 --gate-b-px 1000,700 --bearing 0 --tid AABBCC...

輸出：
  - PNG：軌跡疊在 tks_qiaotou_track.png 上（含目前 / CLI gate）
  - 終端：像素→local_m 換算、GPS 分圈 vs decoder 圈速並排
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 盡量載入專案根 .env，跟 webapp / ingest 同一套 Influx 設定
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _parse_xy(text: str) -> tuple[float, float]:
    parts = text.replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("需要 x,y")
    return float(parts[0]), float(parts[1])


def _print_gate_diagnostics(
    points,
    gate_a_m: tuple[float, float],
    gate_b_m: tuple[float, float],
    forward_bearing_deg: float,
) -> None:
    """過線前後 dt 分佈 + 異號但橫向出界（漏切候選）。"""
    from services.decoder_ingest.gps_lap_splitter import (
        MAX_GAP_SEC,
        _along_offset,
        _forward_normal,
        _gate_frame,
        _signed_across,
        _zero_cross_s,
    )
    from services.webapp.track_coords import latlng_to_local_m

    if len(points) < 2:
        print("\ndiagnostics: <2 points, skip")
        return

    locals_m = [latlng_to_local_m(p.lat, p.lon) for p in points]
    center, along, across, half_len = _gate_frame(gate_a_m, gate_b_m)
    forward_n = _forward_normal(forward_bearing_deg)

    near_dts: list[float] = []
    miss_lateral = 0
    miss_gap = 0
    accepted_raw = 0
    for i in range(len(points) - 1):
        m1, m2 = locals_m[i], locals_m[i + 1]
        dt = (points[i + 1].recorded_at - points[i].recorded_at).total_seconds()
        if dt < 0:
            continue
        d1 = _signed_across(m1, center, across)
        d2 = _signed_across(m2, center, across)
        s = _zero_cross_s(d1, d2)
        if s is None:
            continue
        move = (m2[0] - m1[0], m2[1] - m1[1])
        if move[0] * forward_n[0] + move[1] * forward_n[1] <= 0:
            continue
        x_m = m1[0] + (m2[0] - m1[0]) * s
        y_m = m1[1] + (m2[1] - m1[1]) * s
        lateral = abs(_along_offset((x_m, y_m), center, along))
        # 過線附近的弦：交點在半寬*1.5 內都算「靠近 gate」
        if lateral <= half_len * 1.5:
            near_dts.append(dt)
        if lateral > half_len:
            miss_lateral += 1
            continue
        if dt > MAX_GAP_SEC:
            miss_gap += 1
            continue
        accepted_raw += 1

    print("\n--- gate diagnostics ---")
    print(f"half_len={half_len:.2f}m  MAX_GAP={MAX_GAP_SEC}s")
    print(f"raw_forward_crossings_in_half={accepted_raw}")
    print(f"miss_candidates lateral_oob={miss_lateral}  gap_skip={miss_gap}")
    if near_dts:
        near_dts.sort()
        p50 = near_dts[len(near_dts) // 2]
        print(
            f"near-gate segment dt: n={len(near_dts)}  "
            f"min={near_dts[0]:.3f}s  p50={p50:.3f}s  max={near_dts[-1]:.3f}s"
        )
        over5 = sum(1 for d in near_dts if d > 5.0)
        over15 = sum(1 for d in near_dts if d > MAX_GAP_SEC)
        print(f"near-gate dt>5s={over5}  dt>MAX_GAP={over15}")
    else:
        print("near-gate segment dt: none")


def _build_overlay(
    *,
    track_png: Path,
    out_png: Path,
    points_px: list[tuple[float, float]],
    gate_a_px: tuple[float, float] | None,
    gate_b_px: tuple[float, float] | None,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(track_png).convert("RGB"))
    fig, ax = plt.subplots(figsize=(10, 10), dpi=128)
    ax.imshow(img)
    if points_px:
        xs = [p[0] for p in points_px]
        ys = [p[1] for p in points_px]
        ax.plot(xs, ys, color="#00e5ff", linewidth=0.8, alpha=0.85)
        ax.scatter(xs[0], ys[0], c="#73d13d", s=28, zorder=5, label="start")
        ax.scatter(xs[-1], ys[-1], c="#ff4d4f", s=28, zorder=5, label="end")
    if gate_a_px and gate_b_px:
        ax.plot(
            [gate_a_px[0], gate_b_px[0]],
            [gate_a_px[1], gate_b_px[1]],
            color="white",
            linewidth=3,
            solid_capstyle="round",
            label="gate",
        )
        ax.plot(
            [gate_a_px[0], gate_b_px[0]],
            [gate_a_px[1], gate_b_px[1]],
            color="#111",
            linewidth=5,
            alpha=0.35,
            zorder=3,
        )
    ax.set_xlim(0, img.shape[1])
    ax.set_ylim(img.shape[0], 0)
    ax.set_title("GPS track overlay (click pixels → --gate-a-px / --gate-b-px)")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)


async def _run(args: argparse.Namespace) -> int:
    from services.decoder_ingest.config import load_influx_config
    from services.decoder_ingest.gps_lap_splitter import split_laps_by_gate
    from services.decoder_ingest.influx_reader import InfluxReader
    from services.webapp.track_coords import (
        GATE_FORWARD_BEARING_DEG,
        START_GATE_A_M,
        START_GATE_B_M,
        latlng_to_px,
        local_m_to_px,
        px_to_local_m,
    )

    cfg = load_influx_config()
    reader = InfluxReader(cfg)

    bounds = await reader._session_time_bounds(args.session_id)
    if bounds is None:
        print(f"無法解析 session_id={args.session_id}", file=sys.stderr)
        return 1
    start, stop = bounds
    points, source = await reader._query_track_points(
        device_id=reader._TRACK_DEVICE_ID,
        start=start,
        stop=stop,
    )
    print(f"session={args.session_id}")
    print(f"bounds={start.isoformat()} → {stop.isoformat()}")
    print(f"gps_points={len(points)} source={source}")

    if args.gate_a_px and args.gate_b_px:
        gate_a_m = px_to_local_m(*args.gate_a_px)
        gate_b_m = px_to_local_m(*args.gate_b_px)
        bearing = args.bearing
        print(f"CLI gate px A={args.gate_a_px} → m {gate_a_m}")
        print(f"CLI gate px B={args.gate_b_px} → m {gate_b_m}")
        print(f"bearing={bearing}")
        print(
            "回填 track_coords.py：\n"
            f"  START_GATE_A_M = {gate_a_m}\n"
            f"  START_GATE_B_M = {gate_b_m}\n"
            f"  GATE_FORWARD_BEARING_DEG = {bearing}"
        )
    else:
        gate_a_m = START_GATE_A_M
        gate_b_m = START_GATE_B_M
        bearing = GATE_FORWARD_BEARING_DEG
        print(f"使用 track_coords gate A={gate_a_m} B={gate_b_m} bearing={bearing}")

    # 示範幾個參考像素換算，方便在圖上對白線
    for label, px in (("center", (640.0, 640.0)), ("sample", (900.0, 700.0))):
        print(f"px_to_local_m{px} → {px_to_local_m(*px)}  # {label}")

    track_png = ROOT / "services/webapp/static/tracks/tks_qiaotou_track.png"
    out_png = Path(args.out) if args.out else ROOT / "tmp" / f"gate_calibrate_{args.session_id}.png"
    points_px = [latlng_to_px(p.lat, p.lon) for p in points]
    gate_a_px = local_m_to_px(*gate_a_m)
    gate_b_px = local_m_to_px(*gate_b_m)
    _build_overlay(
        track_png=track_png,
        out_png=out_png,
        points_px=points_px,
        gate_a_px=gate_a_px,
        gate_b_px=gate_b_px,
    )
    print(f"overlay → {out_png}")

    _print_gate_diagnostics(points, gate_a_m, gate_b_m, bearing)

    gps_laps = split_laps_by_gate(points, gate_a_m, gate_b_m, bearing)
    complete = [lap for lap in gps_laps if lap.is_complete]
    print(f"\nGPS laps: total={len(gps_laps)} complete={len(complete)}")
    for lap in gps_laps:
        flag = "OK" if lap.is_complete else "open"
        print(
            f"  #{lap.lap_number:>2}  {lap.lap_time:8.3f}s  [{flag}]  "
            f"pts={len(lap.points)}"
        )

    # decoder 對照
    tid = args.tid
    if not tid:
        summary = await reader.get_session_summary(args.session_id)
        if summary:
            # 取圈數最多的那台
            best = max(summary, key=lambda r: r.lap_count)
            tid = best.transponder_id
            print(f"\nauto tid={tid} (lap_count={best.lap_count})")
    if tid:
        dec = await reader.get_lap_history(args.session_id, tid)
        print(f"decoder laps ({tid}): {len(dec)}")
        print(f"{'lap':>4}  {'gps':>10}  {'decoder':>10}  {'diff':>8}")
        n = min(len(complete), len(dec))
        for i in range(n):
            g = complete[i].lap_time
            d = dec[i].lap_time
            print(f"{i+1:>4}  {g:10.3f}  {d:10.3f}  {g - d:+8.3f}")
        if len(complete) != len(dec):
            print(
                f"WARN: lap count mismatch: GPS complete={len(complete)} vs decoder={len(dec)} "
                "(check gate / bearing)"
            )
        else:
            diffs = [abs(complete[i].lap_time - dec[i].lap_time) for i in range(n)]
            if diffs:
                print(
                    f"max|diff|={max(diffs):.3f}s  "
                    f"{'PASS ±1s' if max(diffs) <= 1.0 else 'FAIL >1s'}"
                )
    else:
        print("\n無 decoder tid（略過對照）")

    await reader.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--tid", default=None, help="decoder transponder id（預設取圈數最多）")
    parser.add_argument("--gate-a-px", type=_parse_xy, default=None, help="像素 x,y")
    parser.add_argument("--gate-b-px", type=_parse_xy, default=None, help="像素 x,y")
    parser.add_argument("--bearing", type=float, default=None, help="行進方位角度（0=北）")
    parser.add_argument("--out", default=None, help="輸出 PNG 路徑")
    args = parser.parse_args()

    from services.webapp.track_coords import GATE_FORWARD_BEARING_DEG

    if args.bearing is None:
        args.bearing = GATE_FORWARD_BEARING_DEG
    if (args.gate_a_px is None) ^ (args.gate_b_px is None):
        parser.error("--gate-a-px 與 --gate-b-px 必須成對提供")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

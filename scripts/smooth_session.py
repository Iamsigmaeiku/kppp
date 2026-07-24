"""對單一 session 跑離線 RTS 平滑並寫入 Influx track_smoothed。

用法：
    python scripts/smooth_session.py --session-id sess-YYYYMMDD-HHMMSS
    python scripts/smooth_session.py --session-id sess-... --dry-run --plot
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _plot(
    *,
    track_png: Path,
    out_png: Path,
    raw_px: list[tuple[float, float]],
    smooth_px: list[tuple[float, float]],
    gap_px: list[tuple[float, float]],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(track_png).convert("RGB"))
    fig, ax = plt.subplots(figsize=(10, 10), dpi=128)
    ax.imshow(img)
    if raw_px:
        ax.plot(
            [p[0] for p in raw_px],
            [p[1] for p in raw_px],
            color="#ff7875",
            linewidth=0.6,
            alpha=0.55,
            label="raw GPS",
        )
    if smooth_px:
        ax.plot(
            [p[0] for p in smooth_px],
            [p[1] for p in smooth_px],
            color="#36cfc9",
            linewidth=1.2,
            alpha=0.95,
            label="RTS smoothed",
        )
    if gap_px:
        ax.scatter(
            [p[0] for p in gap_px],
            [p[1] for p in gap_px],
            c="#faad14",
            s=8,
            zorder=5,
            label="gap",
        )
    ax.set_xlim(0, img.shape[1])
    ax.set_ylim(img.shape[0], 0)
    ax.set_title("raw GPS vs RTS smoothed")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)


async def _run(args: argparse.Namespace) -> int:
    from services.decoder_ingest.config import load_influx_config
    from services.decoder_ingest.influx_reader import InfluxReader
    from services.postprocess.session_smoother import smooth_session
    from services.webapp.track_coords import latlng_to_px

    cfg = load_influx_config()
    reader = InfluxReader(cfg)
    try:
        result = await smooth_session(
            reader,
            args.session_id,
            dry_run=args.dry_run,
            use_speed=not args.no_speed,
            model=args.model,
        )
    finally:
        await reader.close()

    print(
        f"session={result.session_id} device={result.device_id} "
        f"in={result.n_input} out={result.n_output} gap_pts={result.n_gap} "
        f"{'(dry-run)' if args.dry_run else 'written'}"
    )

    if args.plot:
        from services.decoder_ingest.config import load_influx_config as _lic
        from services.decoder_ingest.influx_reader import InfluxReader as _IR
        from services.postprocess.session_smoother import fetch_gps_smooth_inputs
        from services.webapp.track_coords import local_m_to_latlng

        cfg2 = _lic()
        r2 = _IR(cfg2)
        try:
            bounds = await r2._session_time_bounds(args.session_id)
            if bounds is None:
                print("plot skipped: no bounds", file=sys.stderr)
                return 0
            raw = await fetch_gps_smooth_inputs(
                r2, device_id=result.device_id, start=bounds[0], stop=bounds[1]
            )
        finally:
            await r2.close()

        track_png = ROOT / "services/webapp/static/tracks/tks_qiaotou_track.png"
        out_png = (
            Path(args.out)
            if args.out
            else ROOT / "tmp" / f"smooth_{args.session_id}.png"
        )
        raw_px = []
        for s in raw:
            lat, lon = local_m_to_latlng(s.x_m, s.y_m)
            raw_px.append(latlng_to_px(lat, lon))
        # A gap marker is a hard segment boundary.  NaN prevents matplotlib
        # from drawing a fictitious straight line across missing measurements.
        smooth_px = [
            (float("nan"), float("nan")) if o.gap else latlng_to_px(o.lat, o.lon)
            for o in result.outputs
        ]
        gap_px = [latlng_to_px(o.lat, o.lon) for o in result.outputs if o.gap]
        _plot(
            track_png=track_png,
            out_png=out_png,
            raw_px=raw_px,
            smooth_px=smooth_px,
            gap_px=gap_px,
        )
        print(f"plot → {out_png}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--no-speed", action="store_true", help="關掉 |v| 量測更新")
    parser.add_argument("--model", choices=("cv", "ctrv"), default="cv")
    parser.add_argument("--out", default=None, help="plot 輸出路徑")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

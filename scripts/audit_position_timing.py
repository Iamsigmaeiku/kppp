"""Audit one real session, or replay a previously exported row set."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.audit.session_audit import audit_rows, draw_overlay
from services.decoder_ingest.config import load_influx_config
from services.decoder_ingest.influx_reader import InfluxReader


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


async def _query_session(session_id: str) -> list[dict[str, Any]]:
    cfg = load_influx_config()
    reader = InfluxReader(cfg)
    # Full-session IMU/GPS pivots can exceed the library's short default HTTP
    # timeout.  This is an offline audit, so favour completeness over UI-like
    # latency.
    reader._client = InfluxDBClientAsync(
        url=cfg.url,
        token=cfg.token,
        org=cfg.org,
        timeout=120_000,
    )
    try:
        bounds = await reader._session_time_bounds(session_id)
        if bounds is None:
            raise ValueError(f"invalid session id: {session_id}")
        start, stop = bounds
        start_s = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        stop_s = stop.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        measurements = (
            "kart_telemetry",
            "dr_position",
            "track_smoothed",
            "decoder_raw_events",
        )
        rows: list[dict[str, Any]] = []
        for measurement in measurements:
            extra = (
                f' and r.session_id == "{session_id}"'
                if measurement == "decoder_raw_events"
                else ""
            )
            query = (
                f'from(bucket: "{cfg.bucket}") '
                f'|> range(start: time(v: "{start_s}"), stop: time(v: "{stop_s}")) '
                f'|> filter(fn: (r) => r._measurement == "{measurement}"{extra}) '
                f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
                f'|> group() |> sort(columns: ["_time"])'
            )
            tables = None
            for attempt in range(3):
                try:
                    tables = await reader._query(query)
                    break
                except (asyncio.CancelledError, TimeoutError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
                    await reader.close()
                    reader._client = InfluxDBClientAsync(
                        url=cfg.url,
                        token=cfg.token,
                        org=cfg.org,
                        timeout=120_000,
                    )
            assert tables is not None
            for table in tables:
                for record in table.records:
                    values = {
                        str(k): _jsonable(v)
                        for k, v in record.values.items()
                        if not str(k).startswith("_") and k not in ("result", "table")
                    }
                    ts = record.get_time()
                    values["source"] = measurement
                    values["time_ns"] = int(ts.timestamp() * 1e9) if ts else None
                    rows.append(values)
        return rows
    finally:
        await reader.close()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GNSS/IMU/decoder session data-quality audit")
    p.add_argument("--session-id", required=True)
    p.add_argument("--input-json", type=Path, help="offline rows JSON; skips Influx")
    p.add_argument("--plot", action="store_true")
    p.add_argument(
        "--json",
        nargs="?",
        const="-",
        default=None,
        metavar="PATH",
        help="write report JSON; no PATH prints stdout",
    )
    p.add_argument("--output-dir", type=Path, default=ROOT / "tmp" / "audit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.input_json:
        data = json.loads(args.input_json.read_text(encoding="utf-8"))
        rows = data["rows"] if isinstance(data, dict) and "rows" in data else data
    else:
        rows = asyncio.run(_query_session(args.session_id))
    if not isinstance(rows, list):
        raise SystemExit("input JSON must be a list or {'rows': [...]}")
    report = audit_rows(rows, session_id=args.session_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    default_json = args.output_dir / f"{args.session_id}.audit.json"
    if args.json is not None:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text + "\n", encoding="utf-8")
    else:
        default_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(default_json)
    if args.plot:
        png = args.output_dir / f"{args.session_id}.overlay.png"
        draw_overlay(rows, png)
        print(png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

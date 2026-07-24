"""Compare valid independent_lap outputs with authoritative decoder lap times."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

from services.decoder_ingest.config import load_influx_config
from services.timing.lap_compare import compare_lap_sequences


def _query(session_id: str, transponder_id: str | None) -> tuple[list[float], list[float]]:
    load_dotenv(ROOT / ".env", override=False)
    cfg = load_influx_config()
    tid_filter = (
        f' and r.transponder_id == "{transponder_id.upper()}"'
        if transponder_id
        else ""
    )
    decoder_flux = (
        f'from(bucket: "{cfg.bucket}") |> range(start: -90d) '
        f'|> filter(fn:(r) => r._measurement == "decoder_raw_events" '
        f'and r.session_id == "{session_id}"{tid_filter} '
        f'and r._field == "last_lap_time") |> sort(columns:["_time"])'
    )
    independent_flux = (
        f'from(bucket: "{cfg.bucket}") |> range(start: -90d) '
        f'|> filter(fn:(r) => r._measurement == "independent_lap" '
        f'and r.session_id == "{session_id}" and r._field == "lap_time") '
        f'|> sort(columns:["_time"])'
    )
    with InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org) as client:
        decoder_tables = client.query_api().query(decoder_flux, org=cfg.org)
        independent_tables = client.query_api().query(independent_flux, org=cfg.org)
    decoder = [
        float(record.get_value())
        for table in decoder_tables
        for record in table.records
        if record.get_value() is not None and float(record.get_value()) > 0
    ]
    independent = [
        float(record.get_value())
        for table in independent_tables
        for record in table.records
        if record.get_value() is not None and float(record.get_value()) > 0
    ]
    return decoder, independent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transponder-id")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    decoder, independent = _query(args.session_id, args.transponder_id)
    result = {
        "session_id": args.session_id,
        "transponder_id": args.transponder_id,
        "decoder_source": "decoder event lap_time derived from decoder tick",
        "decoder_laps": len(decoder),
        "independent_laps": len(independent),
        **asdict(compare_lap_sequences(decoder, independent)),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if decoder and independent else 2


if __name__ == "__main__":
    raise SystemExit(main())

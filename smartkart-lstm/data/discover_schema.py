"""Explore InfluxDB schema — do NOT invent measurement/field names.

Reads INFLUX_URL / INFLUX_TOKEN / INFLUX_ORG / INFLUX_BUCKET from env (.env).
Prints measurements, field keys, tag keys, time range, sample counts, Hz stats.
Writes a machine-readable snapshot to outputs/schema_snapshot.json.
Never prints the token.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

# Prefer Tailscale IP of chuck when LAN URL is unreachable from Windows.
DEFAULT_TAILSCALE_INFLUX = "http://100.102.122.104:8086"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _cfg() -> dict[str, str]:
    url = _env("INFLUX_URL")
    if not url:
        raise SystemExit("INFLUX_URL missing")
    # From Windows over Tailscale: rewrite LAN IP to chuck MagicDNS/Tailscale IP
    if "192.168." in url and _env("INFLUX_USE_TAILSCALE", "1") in ("1", "true", "yes"):
        # Keep path/port; swap host
        from urllib.parse import urlparse, urlunparse

        p = urlparse(url)
        tp = urlparse(DEFAULT_TAILSCALE_INFLUX)
        url = urlunparse((p.scheme, f"{tp.hostname}:{p.port or 8086}", p.path, "", "", ""))
    token = _env("INFLUX_TOKEN")
    org = _env("INFLUX_ORG", "kpp")
    bucket = _env("INFLUX_BUCKET", "decoder")
    if not token:
        raise SystemExit("INFLUX_TOKEN missing")
    return {"url": url, "token": token, "org": org, "bucket": bucket}


def _query(qa, flux: str, org: str):
    return qa.query(flux, org=org)


def list_measurements(qa, bucket: str, org: str) -> list[str]:
    flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{bucket}")
'''
    tables = _query(qa, flux, org)
    out: list[str] = []
    for table in tables:
        for rec in table.records:
            v = rec.get_value()
            if v is not None:
                out.append(str(v))
    return sorted(set(out))


def list_field_keys(qa, bucket: str, org: str, measurement: str) -> list[str]:
    flux = f'''
import "influxdata/influxdb/schema"
schema.fieldKeys(bucket: "{bucket}", predicate: (r) => r._measurement == "{measurement}")
'''
    tables = _query(qa, flux, org)
    out: list[str] = []
    for table in tables:
        for rec in table.records:
            v = rec.get_value()
            if v is not None:
                out.append(str(v))
    return sorted(set(out))


def list_tag_keys(qa, bucket: str, org: str, measurement: str) -> list[str]:
    flux = f'''
import "influxdata/influxdb/schema"
schema.tagKeys(bucket: "{bucket}", predicate: (r) => r._measurement == "{measurement}")
'''
    tables = _query(qa, flux, org)
    out: list[str] = []
    for table in tables:
        for rec in table.records:
            v = rec.get_value()
            if v is not None and not str(v).startswith("_"):
                out.append(str(v))
    return sorted(set(out))


def list_tag_values(
    qa, bucket: str, org: str, measurement: str, tag: str, limit: int = 50
) -> list[str]:
    flux = f'''
import "influxdata/influxdb/schema"
schema.tagValues(
  bucket: "{bucket}",
  tag: "{tag}",
  predicate: (r) => r._measurement == "{measurement}",
  start: 0,
)
  |> limit(n: {limit})
'''
    try:
        tables = _query(qa, flux, org)
    except Exception as exc:  # noqa: BLE001
        return [f"<error: {exc}>"]
    out: list[str] = []
    for table in tables:
        for rec in table.records:
            v = rec.get_value()
            if v is not None:
                out.append(str(v))
    return out


def time_range_and_count(
    qa, bucket: str, org: str, measurement: str, sample_field: str | None = None
) -> dict:
    """min/max _time + approximate row count for one field (cheap)."""
    field_filter = (
        f' and r._field == "{sample_field}"' if sample_field else ""
    )
    flux_first = f'''
from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{measurement}"{field_filter})
  |> first()
  |> keep(columns: ["_time"])
'''
    flux_last = f'''
from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{measurement}"{field_filter})
  |> last()
  |> keep(columns: ["_time"])
'''
    # Count can be expensive on dense IMU; cap via sample window estimate if needed.
    flux_count = f'''
from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{measurement}"{field_filter})
  |> count()
  |> group()
  |> sum(column: "_value")
'''
    first = last = None
    count = 0
    try:
        for table in _query(qa, flux_first, org):
            for rec in table.records:
                first = rec.get_time()
                break
        for table in _query(qa, flux_last, org):
            for rec in table.records:
                last = rec.get_time()
                break
        for table in _query(qa, flux_count, org):
            for rec in table.records:
                count = int(rec.get_value() or 0)
                break
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "first": None, "last": None, "days": 0.0, "error": str(exc)}

    days = 0.0
    if first and last:
        days = (last - first).total_seconds() / 86400.0
    return {
        "count": count,
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "days": round(days, 3),
        "count_field": sample_field,
    }


def sample_hz(
    qa, bucket: str, org: str, measurement: str, field: str | None, max_points: int = 5000
) -> dict:
    """Estimate sampling rate from consecutive timestamps of one field."""
    if not field:
        return {"median_hz": None, "p95_dt_ms": None, "n_intervals": 0}
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")
  |> keep(columns: ["_time"])
  |> sort(columns: ["_time"])
  |> limit(n: {max_points})
'''
    times: list[datetime] = []
    try:
        for table in _query(qa, flux, org):
            for rec in table.records:
                t = rec.get_time()
                if t is not None:
                    times.append(t)
    except Exception as exc:  # noqa: BLE001
        return {"median_hz": None, "p95_dt_ms": None, "n_intervals": 0, "error": str(exc)}

    if len(times) < 3:
        return {"median_hz": None, "p95_dt_ms": None, "n_intervals": 0}

    dts = []
    for a, b in zip(times, times[1:]):
        dt = (b - a).total_seconds()
        if 0 < dt < 10.0:  # ignore large gaps
            dts.append(dt)
    if not dts:
        return {"median_hz": None, "p95_dt_ms": None, "n_intervals": 0}

    dts_sorted = sorted(dts)
    med = statistics.median(dts_sorted)
    p95 = dts_sorted[int(0.95 * (len(dts_sorted) - 1))]
    return {
        "median_hz": round(1.0 / med, 2) if med > 0 else None,
        "p95_dt_ms": round(p95 * 1000.0, 2),
        "median_dt_ms": round(med * 1000.0, 2),
        "n_intervals": len(dts),
        "sample_field": field,
    }


def preferred_hz_field(fields: list[str]) -> str | None:
    for cand in ("ax", "gz", "gps_lat", "lat_dr", "last_lap_time", "lat", "speed_mps"):
        if cand in fields:
            return cand
    return fields[0] if fields else None


def main() -> int:
    cfg = _cfg()
    print("=== SmartKart Influx schema discovery ===")
    print(f"url   : {cfg['url']}")
    print(f"org   : {cfg['org']}")
    print(f"bucket: {cfg['bucket']}")
    print(f"token : <redacted len={len(cfg['token'])}>")
    print()

    client = InfluxDBClient(
        url=cfg["url"], token=cfg["token"], org=cfg["org"], timeout=120_000
    )
    try:
        qa = client.query_api()
        measurements = list_measurements(qa, cfg["bucket"], cfg["org"])
        print(f"measurements ({len(measurements)}):")
        for m in measurements:
            print(f"  - {m}")
        print()

        snapshot: dict = {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "url": cfg["url"],
            "org": cfg["org"],
            "bucket": cfg["bucket"],
            "measurements": {},
        }

        for m in measurements:
            fields = list_field_keys(qa, cfg["bucket"], cfg["org"], m)
            tags = list_tag_keys(qa, cfg["bucket"], cfg["org"], m)
            hz_field = preferred_hz_field(fields)
            tr = time_range_and_count(qa, cfg["bucket"], cfg["org"], m, hz_field)
            hz = sample_hz(qa, cfg["bucket"], cfg["org"], m, hz_field)

            tag_values: dict[str, list[str]] = {}
            for tag in tags:
                if tag in ("device_id", "car_id", "session_id", "device", "event_type", "transponder_id"):
                    tag_values[tag] = list_tag_values(
                        qa, cfg["bucket"], cfg["org"], m, tag, limit=40
                    )

            entry = {
                "fields": fields,
                "tags": tags,
                "tag_values_sample": tag_values,
                "time_range": tr,
                "hz": hz,
            }
            snapshot["measurements"][m] = entry

            print(f"--- measurement: {m} ---")
            print(f"  fields ({len(fields)}): {', '.join(fields) if fields else '(none)'}")
            print(f"  tags   ({len(tags)}): {', '.join(tags) if tags else '(none)'}")
            for tag, vals in tag_values.items():
                preview = ", ".join(vals[:12])
                more = f" …(+{len(vals)-12})" if len(vals) > 12 else ""
                print(f"    {tag} values: {preview}{more}")
            print(
                f"  time   : {tr.get('first')} -> {tr.get('last')} "
                f"({tr.get('days')} days, count~={tr.get('count')})"
            )
            print(
                f"  hz     : median={hz.get('median_hz')} Hz "
                f"median_dt={hz.get('median_dt_ms')} ms "
                f"p95_dt={hz.get('p95_dt_ms')} ms "
                f"(field={hz.get('sample_field')}, n={hz.get('n_intervals')})"
            )
            if "error" in tr:
                print(f"  time_range error: {tr['error']}")
            if "error" in hz:
                print(f"  hz error: {hz['error']}")
            print()

        out_dir = PKG / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "schema_snapshot.json"
        out_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"wrote {out_path}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

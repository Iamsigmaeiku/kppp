"""Dump recent Influx fields + telemetry status for ESP hotspot path."""
import os
import sys

import paramiko

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "00000000").strip()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    hostname=host,
    username=user,
    password=password,
    allow_agent=False,
    look_for_keys=False,
    timeout=20,
)

script = r"""
cd ~/kpp && .venv/bin/python - <<'PY'
import json, os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path.home() / "kpp" / ".env")
from influxdb_client import InfluxDBClient

bucket = os.environ.get("INFLUX_BUCKET", "decoder")
client = InfluxDBClient(
    url=os.environ["INFLUX_URL"],
    token=os.environ["INFLUX_TOKEN"],
    org=os.environ["INFLUX_ORG"],
)
q = client.query_api()

print("=== env device map / token present ===")
print("TELEMETRY_UDP_DEVICE_ID=", os.environ.get("TELEMETRY_UDP_DEVICE_ID"))
print("TELEMETRY_DEVICE_CAR_MAP=", os.environ.get("TELEMETRY_DEVICE_CAR_MAP"))
print("has_INGEST_TOKEN=", bool(os.environ.get("TELEMETRY_INGEST_TOKEN")))

print("\n=== last kart_telemetry fields by device (15m) ===")
flux = f'''
from(bucket: "{bucket}")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> group(columns: ["device_id", "_field"])
  |> last()
'''
rows = []
for t in q.query(flux):
    for r in t.records:
        rows.append((r.values.get("device_id"), r.get_field(), r.get_value()))
for device in sorted({d for d,_,_ in rows if d}):
    print(f"-- device={device}")
    for d,f,v in sorted(rows):
        if d == device:
            print(f"  {f}={v}")

print("\n=== last dr_position (15m) ===")
flux2 = f'''
from(bucket: "{bucket}")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "dr_position")
  |> group(columns: ["device_id", "_field"])
  |> last()
'''
n=0
for t in q.query(flux2):
    for r in t.records:
        n+=1
        print(r.values.get("device_id"), r.get_field(), r.get_value())
print("dr records", n)

print("\n=== gps_fix tagged points count 15m ===")
flux3 = f'''
from(bucket: "{bucket}")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" and r.gps_fix == "1")
  |> filter(fn: (r) => r._field == "gps_lat")
  |> count()
'''
for t in q.query(flux3):
    for r in t.records:
        print("gps_fix lat count=", r.get_value(), "device=", r.values.get("device_id"))

print("\n=== packet-v2 GPS window (2h) ===")
for aggregate in ("first", "last", "count"):
    flux_window = f'''
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> filter(fn: (r) => r._field == "gps_packet_seq")
  |> group(columns: ["device_id"])
  |> {aggregate}()
'''
    for table in q.query(flux_window):
        for record in table.records:
            print(
                aggregate,
                record.values.get("device_id"),
                record.values.get("_time"),
                record.get_value(),
            )

flux_packets = f'''
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> filter(fn: (r) => r._field == "gps_packet_seq")
  |> group(columns: ["device_id"])
  |> sort(columns: ["_time"])
'''
packet_rows = []
for table in q.query(flux_packets):
    for record in table.records:
        packet_rows.append(
            (
                record.values.get("device_id"),
                record.get_time(),
                int(record.get_value()),
            )
        )
for device in sorted({row[0] for row in packet_rows}):
    rows_d = [row for row in packet_rows if row[0] == device]
    gaps = []
    resets = 0
    missing = 0
    for previous, current in zip(rows_d, rows_d[1:]):
        dt = (current[1] - previous[1]).total_seconds()
        if dt > 1.0:
            gaps.append(dt)
        delta = current[2] - previous[2]
        if delta <= 0:
            resets += 1
        elif delta > 1:
            missing += delta - 1
    print(
        "summary",
        device,
        f"points={len(rows_d)}",
        f"gaps_gt_1s={len(gaps)}",
        f"max_gap_s={max(gaps, default=0):.3f}",
        f"seq_resets_or_reorders={resets}",
        f"seq_missing_within_runs={missing}",
    )
    run_start = 0
    run_no = 0
    for idx in range(1, len(rows_d) + 1):
        is_boundary = (
            idx == len(rows_d)
            or (rows_d[idx][1] - rows_d[idx - 1][1]).total_seconds() > 1.0
        )
        if not is_boundary:
            continue
        run = rows_d[run_start:idx]
        run_no += 1
        print(
            f"  run={run_no}",
            f"start={run[0][1].isoformat()}",
            f"stop={run[-1][1].isoformat()}",
            f"points={len(run)}",
            f"seq={run[0][2]}..{run[-1][2]}",
        )
        run_start = idx

print("\n=== last track_smoothed by session (2h) ===")
flux4 = f'''
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "track_smoothed")
  |> filter(fn: (r) => r._field == "lat_s")
  |> group(columns: ["device_id", "session_id", "algo"])
  |> last()
'''
for t in q.query(flux4):
    for r in t.records:
        print(
            "track",
            r.values.get("device_id"),
            r.values.get("session_id"),
            r.values.get("algo"),
            r.get_time(),
        )
PY
"""
_, o, e = c.exec_command(script)
out = (o.read() + e.read()).decode("utf-8", "replace")
print(out[-8000:])
c.close()

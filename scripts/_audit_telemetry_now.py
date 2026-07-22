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
PY
"""
_, o, e = c.exec_command(script)
out = (o.read() + e.read()).decode("utf-8", "replace")
print(out[-8000:])
c.close()

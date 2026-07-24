"""One-shot: IMU/GPS freshness on local Influx (.env)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient

root = Path(__file__).resolve().parents[1]
env = root / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

url = os.environ["INFLUX_URL"]
token = os.environ["INFLUX_TOKEN"]
org = os.environ["INFLUX_ORG"]
bucket = os.environ["INFLUX_BUCKET"]

FIELDS = [
    "imu_ax",
    "imu_gx",
    "accel_x",
    "gx",
    "gps_lat",
    "roll_deg",
    "pitch_deg",
    "yaw_deg",
    "q0",
]

client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000)
q = client.query_api()
flux = f"""
from(bucket: "{bucket}")
  |> range(start: -20m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> filter(fn: (r) => {" or ".join(f'r._field == "{f}"' for f in FIELDS)})
  |> group(columns: ["_field", "device_id"])
  |> last()
"""
now = datetime.now(timezone.utc)
print("now", now.isoformat())
rows = []
for table in q.query(flux):
    for r in table.records:
        rows.append(
            (r.get_time(), r.values.get("device_id"), r.get_field(), r.get_value())
        )
rows.sort(key=lambda x: x[0] or now)
for ts, dev, f, v in rows:
    age = (now - ts).total_seconds() if ts else None
    print(f"{age:8.1f}s  {dev}  {f}={v}")

flux2 = f"""
from(bucket: "{bucket}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> filter(fn: (r) => r._field == "imu_ax" or r._field == "accel_x" or r._field == "gx" or r._field == "imu_gx")
  |> group(columns: ["_field", "device_id"])
  |> count()
"""
print("--- counts last 2m ---")
for table in q.query(flux2):
    for r in table.records:
        print(r.values.get("device_id"), r.get_field(), r.get_value())

flux3 = f"""
from(bucket: "{bucket}")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> keep(columns: ["_field", "device_id"])
  |> distinct(column: "_field")
"""
print("--- distinct fields last 30m ---")
seen: set[tuple] = set()
for table in q.query(flux3):
    for r in table.records:
        seen.add((r.values.get("device_id"), r.get_value()))
for item in sorted(seen):
    print(item)

flux4 = f"""
from(bucket: "{bucket}")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> filter(fn: (r) =>
      r._field == "gps_lat" or r._field == "ax" or r._field == "ay" or r._field == "az"
      or r._field == "gx" or r._field == "gy" or r._field == "gz"
      or r._field == "imu_temp_c" or r._field == "quat_w" or r._field == "roll_deg"
  )
  |> group(columns: ["_field", "device_id"])
  |> last()
"""
print("--- last ax/gx/gps ---")
for table in q.query(flux4):
    for r in table.records:
        ts = r.get_time()
        age = (now - ts).total_seconds() if ts else None
        print(f"{age:8.1f}s  {r.values.get('device_id')}  {r.get_field()}={r.get_value()}")

flux5 = f"""
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" and r.device_id == "esp32-kart-01")
  |> filter(fn: (r) => r._field == "imu_fault" or r._field == "imu_temp_c" or r._field == "ax" or r._field == "gps_lat")
  |> aggregateWindow(every: 2m, fn: last, createEmpty: false)
"""
print("--- 2m series (age_s field value) ---")
series = []
for table in q.query(flux5):
    for r in table.records:
        ts = r.get_time()
        age = (now - ts).total_seconds() if ts else None
        series.append((age, r.get_field(), r.get_value()))
for age, f, v in sorted(series, key=lambda x: -(x[0] or 0))[-40:]:
    print(f"{age:8.0f}  {f:12}  {v}")
client.close()

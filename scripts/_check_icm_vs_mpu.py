"""ICM vs MPU freshness + last burst values."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient

root = Path(__file__).resolve().parents[1]
for line in (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

c = InfluxDBClient(
    url=os.environ["INFLUX_URL"],
    token=os.environ["INFLUX_TOKEN"],
    org=os.environ["INFLUX_ORG"],
)
q = c.query_api()
now = datetime.now(timezone.utc)
fields = [
    "ax",
    "ay",
    "az",
    "gx",
    "imu_temp_c",
    "mpu_ax",
    "mpu_ay",
    "mpu_az",
    "mpu_temp_c",
    "imu_fault",
    "gps_lat",
]
or_fields = " or ".join(f'r._field == "{f}"' for f in fields)
flux = f"""
from(bucket:"decoder")
  |> range(start: -30m)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01")
  |> filter(fn:(r)=> {or_fields})
  |> group(columns:["_field"])
  |> last()
"""
print("last samples:")
rows = []
for t in q.query(flux):
    for r in t.records:
        age = (now - r.get_time()).total_seconds()
        rows.append((age, r.get_field(), r.get_value()))
for age, f, v in sorted(rows):
    print(f"{age:8.1f}s  {f:12} = {v}")

# counts per minute for ax vs mpu_ax
flux2 = """
from(bucket:"decoder")
  |> range(start: -20m)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01")
  |> filter(fn:(r)=>r._field=="ax" or r._field=="mpu_ax")
  |> aggregateWindow(every: 30s, fn: count, createEmpty: false)
"""
print("\n30s counts:")
for t in q.query(flux2):
    for r in t.records:
        age = (now - r.get_time()).total_seconds()
        print(f"{age:8.0f}s  {r.get_field():8} n={r.get_value()}")
c.close()

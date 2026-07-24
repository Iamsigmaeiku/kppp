"""Check track_smoothed rts_cv_lag freshness."""
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
flux = """
from(bucket:"decoder")
  |> range(start: -15m)
  |> filter(fn:(r)=>r._measurement=="track_smoothed")
  |> filter(fn:(r)=>r._field=="lat_s")
  |> group(columns:["algo","device_id"])
  |> count()
"""
print("counts 15m:")
for t in q.query(flux):
    for r in t.records:
        print(r.values.get("algo"), r.values.get("device_id"), r.get_value())

flux2 = """
from(bucket:"decoder")
  |> range(start: -15m)
  |> filter(fn:(r)=>r._measurement=="track_smoothed" and r.algo=="rts_cv_lag")
  |> filter(fn:(r)=>r._field=="lat_s")
  |> last()
"""
print("last rts_cv_lag:")
for t in q.query(flux2):
    for r in t.records:
        age = (now - r.get_time()).total_seconds()
        print(f"age={age:.1f}s lat={r.get_value()} device={r.values.get('device_id')}")
c.close()

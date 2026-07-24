"""Analyze GPS gaps for latest session track."""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from influxdb_client import InfluxDBClient

root = Path("/home/evan/kpp")
for line in (root / ".env").read_text().splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(),v.strip())

c = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=os.environ["INFLUX_ORG"])
q = c.query_api()

# last 2h gps_fix=1 points
flux = '''
from(bucket:"decoder")
  |> range(start: -2h)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r.gps_fix=="1")
  |> filter(fn:(r)=>r._field=="gps_lat" or r._field=="gps_lon" or r._field=="gps_satellites" or r._field=="gps_hdop" or r._field=="gps_speed_mps")
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> group()
  |> sort(columns:["_time"])
'''
rows=[]
for t in q.query(flux):
  for r in t.records:
    lat,lon=r.values.get("gps_lat"),r.values.get("gps_lon")
    ts=r.get_time()
    if lat is None or lon is None or ts is None: continue
    rows.append((ts, float(lat), float(lon), r.values.get("gps_satellites"), r.values.get("gps_hdop"), r.values.get("gps_speed_mps")))

print(f"n_gps_fix={len(rows)}")
if len(rows)<2:
  raise SystemExit(0)

# gaps
gaps=[]
for i in range(1,len(rows)):
  dt=(rows[i][0]-rows[i-1][0]).total_seconds()
  dlat=(rows[i][1]-rows[i-1][1])*111320
  dlon=(rows[i][2]-rows[i-1][2])*111320*0.92
  dist=(dlat*dlat+dlon*dlon)**0.5
  if dt>2.0 or dist>40:
    gaps.append((dt, dist, rows[i-1][0], rows[i][0], rows[i-1][1], rows[i-1][2], rows[i][1], rows[i][2], rows[i-1][3], rows[i][3]))

gaps.sort(key=lambda g:-g[0])
print(f"gaps dt>2s or dist>40m: {len(gaps)}")
print("top gaps by dt:")
for g in gaps[:15]:
  print(f"  dt={g[0]:6.1f}s dist={g[1]:6.1f}m  {g[2].isoformat()} -> {g[3].isoformat()}  sv={g[8]}->{g[9]}  ({g[4]:.5f},{g[5]:.5f})->({g[6]:.5f},{g[7]:.5f})")

# also count non-fix gps_lat in same window for comparison
flux2='''
from(bucket:"decoder")
  |> range(start: -2h)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01")
  |> filter(fn:(r)=>r._field=="gps_lat")
  |> count()
'''
flux3='''
from(bucket:"decoder")
  |> range(start: -2h)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r.gps_fix=="1")
  |> filter(fn:(r)=>r._field=="gps_lat")
  |> count()
'''
print("all gps_lat count:", [r.get_value() for t in q.query(flux2) for r in t.records])
print("gps_fix=1 count:", [r.get_value() for t in q.query(flux3) for r in t.records])
c.close()

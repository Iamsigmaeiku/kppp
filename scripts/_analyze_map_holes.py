"""Find in-motion GPS holes that create map gaps (dt large AND dist large)."""
from __future__ import annotations

import os
from pathlib import Path

from influxdb_client import InfluxDBClient

root = Path("/home/evan/kpp")
for line in (root / ".env").read_text().splitlines():
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

# Focus on afternoon race window (local 14:00-17:00 = UTC 06:00-09:00)
flux = """
from(bucket:"decoder")
  |> range(start: 2026-07-23T06:00:00Z, stop: 2026-07-23T09:30:00Z)
  |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r.gps_fix=="1")
  |> filter(fn:(r)=>r._field=="gps_lat" or r._field=="gps_lon" or r._field=="gps_satellites" or r._field=="gps_speed_mps")
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> group()
  |> sort(columns:["_time"])
"""
rows = []
for t in q.query(flux):
    for r in t.records:
        lat, lon, ts = r.values.get("gps_lat"), r.values.get("gps_lon"), r.get_time()
        if lat is None or lon is None or ts is None:
            continue
        rows.append(
            (
                ts,
                float(lat),
                float(lon),
                r.values.get("gps_satellites"),
                r.values.get("gps_speed_mps"),
            )
        )
print(f"n={len(rows)}")

# Map-visible holes: dt>3 OR dist>50 while moving
holes = []
for i in range(1, len(rows)):
    dt = (rows[i][0] - rows[i - 1][0]).total_seconds()
    dlat = (rows[i][1] - rows[i - 1][1]) * 111320
    dlon = (rows[i][2] - rows[i - 1][2]) * 111320 * 0.92
    dist = (dlat * dlat + dlon * dlon) ** 0.5
    spd = rows[i - 1][4] or 0
    if dist > 40 and dt > 1.5:
        holes.append((dt, dist, spd, rows[i - 1], rows[i]))

holes.sort(key=lambda h: -h[1])
print(f"spatial jumps dist>40 & dt>1.5: {len(holes)}")
for dt, dist, spd, a, b in holes[:20]:
    # local UTC+8
    print(
        f"  dist={dist:6.1f}m dt={dt:5.1f}s spd0={spd} "
        f"sv={a[3]}->{b[3]}  "
        f"{a[0].strftime('%H:%M:%S')}Z ({a[1]:.5f},{a[2]:.5f}) -> "
        f"{b[0].strftime('%H:%M:%S')}Z ({b[1]:.5f},{b[2]:.5f})"
    )

# Rate histogram during 06:50-07:10 local display = still UTC
# Inter-sample dt histogram when speed > 5
import collections

buckets = collections.Counter()
moving_gaps = []
for i in range(1, len(rows)):
    dt = (rows[i][0] - rows[i - 1][0]).total_seconds()
    spd = rows[i][4] or rows[i - 1][4] or 0
    if spd and float(spd) > 3:
        if dt < 0.5:
            buckets["<0.5"] += 1
        elif dt < 1:
            buckets["0.5-1"] += 1
        elif dt < 2:
            buckets["1-2"] += 1
        elif dt < 5:
            buckets["2-5"] += 1
        else:
            buckets[">=5"] += 1
            moving_gaps.append((dt, rows[i - 1][0], rows[i][0], spd))

print("moving (spd>3) inter-sample dt buckets:", dict(buckets))
print("moving gaps >=5s:", len(moving_gaps))
for dt, t0, t1, spd in sorted(moving_gaps, reverse=True)[:12]:
    print(f"  dt={dt:5.1f}s spd={spd}  {t0.isoformat()} -> {t1.isoformat()}")
c.close()

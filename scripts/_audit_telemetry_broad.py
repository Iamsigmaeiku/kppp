"""Broader audit: devices, gps history, dashboard status cache."""
import os
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    "100.102.122.104",
    username="evan",
    password=os.environ.get("KPP_PI_PASS", "00000000"),
    allow_agent=False,
    look_for_keys=False,
    timeout=20,
)

script = r"""
cd ~/kpp && .venv/bin/python - <<'PY'
import os, json, urllib.request
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path.home() / "kpp" / ".env")
from influxdb_client import InfluxDBClient

bucket = os.environ.get("INFLUX_BUCKET", "decoder")
client = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=os.environ["INFLUX_ORG"])
q = client.query_api()

print("=== distinct device_id last 2h kart_telemetry ===")
flux = f'''
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> keep(columns: ["device_id"])
  |> distinct(column: "device_id")
'''
for t in q.query(flux):
    for r in t.records:
        print("device:", r.values.get("device_id") or r.get_value())

print("\n=== any gps_* / lat_dr last 2h ===")
flux2 = f'''
from(bucket: "{bucket}")
  |> range(start: -2h)
  |> filter(fn: (r) => (r._measurement == "kart_telemetry" or r._measurement == "dr_position"))
  |> filter(fn: (r) => r._field == "gps_lat" or r._field == "gps_lon" or r._field == "gps_satellites" or r._field == "gps_fresh" or r._field == "gps_speed_mps" or r._field == "lat_dr" or r._field == "lon_dr" or r._field == "imu_fault")
  |> group(columns: ["_measurement", "device_id", "_field"])
  |> last()
'''
n=0
for t in q.query(flux2):
    for r in t.records:
        n+=1
        print(r.get_measurement(), r.values.get("device_id"), r.get_field(), r.get_value(), r.get_time())
print("gps-ish records", n)

print("\n=== field write rates last 5m for esp32-kart-01 ===")
flux3 = f'''
from(bucket: "{bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" and r.device_id == "esp32-kart-01")
  |> group(columns: ["_field"])
  |> count()
'''
for t in q.query(flux3):
    for r in t.records:
        print(r.get_field(), r.get_value())

# local status endpoint (no auth? may 302)
print("\n=== curl telemetry status ===")
import subprocess
print(subprocess.getoutput("curl -s -o /tmp/ts.json -w '%{http_code}' http://127.0.0.1:8000/api/telemetry/status"))
print(subprocess.getoutput("head -c 2000 /tmp/ts.json"))
PY
"""
_, o, e = c.exec_command(script)
print((o.read() + e.read()).decode("utf-8", "replace")[-9000:])
c.close()

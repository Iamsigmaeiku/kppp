"""Query Influx on chuck for recent ESP telemetry."""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    "100.102.122.104",
    username="evan",
    password="00000000",
    allow_agent=False,
    look_for_keys=False,
)

py = r"""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path.home() / "kpp" / ".env")
from influxdb_client import InfluxDBClient

url = os.environ.get("INFLUX_URL", "")
token = os.environ.get("INFLUX_TOKEN", "")
org = os.environ.get("INFLUX_ORG", "")
bucket = os.environ.get("INFLUX_BUCKET", "decoder")
print("influx", url, "bucket", bucket)

client = InfluxDBClient(url=url, token=token, org=org)
q = client.query_api()

flux = '''
from(bucket: "''' + bucket + '''")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" or r._measurement == "dr_position")
  |> filter(fn: (r) => r.device_id == "esp32-kart-01")
  |> group(columns: ["_measurement", "_field"])
  |> last()
  |> limit(n: 20)
'''
tables = q.query(flux)
n = 0
for t in tables:
    for r in t.records:
        n += 1
        print(r.get_measurement(), r.get_field(), r.get_value(), r.get_time())
print("records", n)
"""

cmd = f"cd ~/kpp && .venv/bin/python -c {repr(py)}"
_, o, e = c.exec_command(cmd, get_pty=True)
print((o.read() + e.read()).decode("utf-8", "replace")[-4000:])

# UDP recv counter
_, o, _ = c.exec_command(
    "awk '/^Udp:/ {print; getline; print}' /proc/net/snmp | head -4"
)
print("snmp:\n", o.read().decode())

c.close()

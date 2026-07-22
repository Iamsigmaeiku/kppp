"""One-shot Influx field dump on chuck."""
import os
import sys

import paramiko

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "").strip()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
kwargs = dict(hostname=host, username=user, timeout=20)
if password:
    kwargs.update(password=password, allow_agent=False, look_for_keys=False)
else:
    kwargs.update(allow_agent=True, look_for_keys=True)
try:
    c.connect(**kwargs)
except paramiko.PasswordRequiredException:
    sys.exit("ERROR: set KPP_PI_PASS for SSH")
script = r"""
cd ~/kpp && .venv/bin/python - <<'PY'
import os
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
flux = f'''
from(bucket: "{bucket}")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" and r.device_id == "esp32-kart-01")
  |> group(columns: ["_field"])
  |> last()
'''
n = 0
for t in client.query_api().query(flux):
    for r in t.records:
        n += 1
        print(f"{r.get_field()}\t{r.get_value()}")
print("--- count", n)
PY
"""
_, o, e = c.exec_command(script)
out = (o.read() + e.read()).decode("utf-8", "replace")
print(out)
c.close()

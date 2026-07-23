import os
import paramiko
from datetime import datetime, timezone

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "00000000").strip()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, allow_agent=False, look_for_keys=False, timeout=20)
cmd = r"""cd ~/kpp && .venv/bin/python - <<'PY'
import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone
load_dotenv(Path.home() / "kpp" / ".env")
from influxdb_client import InfluxDBClient
c = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=os.environ["INFLUX_ORG"])
q = c.query_api()
for mins in [1, 2, 5]:
    flux = f'''from(bucket:"decoder") |> range(start:-{mins}m) |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r._field=="gps_lat") |> count()'''
    for t in q.query(flux):
        for r in t.records:
            print(f"gps_pts_{mins}m", r.get_value())
flux = '''from(bucket:"decoder") |> range(start:-1h) |> filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r._field=="gps_lat") |> last()'''
for t in q.query(flux):
    for r in t.records:
        print("last_gps", r.get_time())
print("server_now", datetime.now(timezone.utc))
PY"""
_, o, e = c.exec_command(cmd)
print((o.read() + e.read()).decode())
c.close()

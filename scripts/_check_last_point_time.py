import os
from pathlib import Path
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient
from datetime import datetime, timezone

import sys
from pathlib import Path as _P
_env = _P("/home/evan/kpp/.env") if sys.platform != "win32" else _P(__file__).resolve().parents[1] / ".env"
if not _env.exists():
    _env = Path.home() / "kpp" / ".env"
load_dotenv(_env)
client = InfluxDBClient(
    url=os.environ["INFLUX_URL"],
    token=os.environ["INFLUX_TOKEN"],
    org=os.environ["INFLUX_ORG"],
)
q = client.query_api()
for label, flux in [
    ("last_gps", '''from(bucket:"decoder")|>range(start:-1h)|>filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r._field=="gps_lat")|>last()'''),
    ("pts_2m", '''from(bucket:"decoder")|>range(start:-2m)|>filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r._field=="gps_lat")|>count()'''),
    ("pts_5m", '''from(bucket:"decoder")|>range(start:-5m)|>filter(fn:(r)=>r._measurement=="kart_telemetry" and r.device_id=="esp32-kart-01" and r._field=="gps_lat")|>count()'''),
]:
    for t in q.query(flux):
        for r in t.records:
            if label == "last_gps":
                print(label, r.get_time(), "val", r.get_value())
            else:
                print(label, r.get_value())
print("now_utc", datetime.now(timezone.utc))

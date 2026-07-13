"""Archive current snapshot into session_archive with car numbers (run on Pi)."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv("/home/evan/kpp/.env")

CAR_MAP = {
    "140211084277": "17",
    "140201B81B77": "15",
    "14021124C877": "11",
    "140215494F77": "19",
    "140210998E77": "20",
    "148210E3C477": "14",  # live UID (148 prefix)
    "140210E3C477": "14",
    "140210B98377": "13",
    "140215359577": "12",
    "140211241C77": "18",
    "140210D7E877": "16",
}

env_path = "/home/evan/kpp/.env"
raw = open(env_path, encoding="utf-8").read()
new_map = ",".join(f"{k}:{v}" for k, v in CAR_MAP.items())
if re.search(r"^CAR_NUMBER_MAP=.*$", raw, flags=re.M):
    raw2 = re.sub(r"^CAR_NUMBER_MAP=.*$", f"CAR_NUMBER_MAP={new_map}", raw, flags=re.M)
else:
    raw2 = raw.rstrip() + f"\nCAR_NUMBER_MAP={new_map}\n"
open(env_path, "w", encoding="utf-8").write(raw2)
print("updated CAR_NUMBER_MAP in .env")

snap = json.load(open("/home/evan/kpp/services/decoder_ingest/session_snapshot.json"))
states = snap.get("states") or {}
if not states:
    raise SystemExit("snapshot empty — nothing to archive")

client = InfluxDBClient(
    url=os.getenv("INFLUX_URL"),
    token=os.getenv("INFLUX_TOKEN"),
    org=os.getenv("INFLUX_ORG"),
)
q = client.query_api()
bucket = os.getenv("INFLUX_BUCKET", "decoder")
# decoder_raw_events 的 transponder_id 是 field 不是 tag，只靠 session_id 計數
flux = f"""
from(bucket: "{bucket}")
  |> range(start: -12h)
  |> filter(fn: (r) => r._measurement == "decoder_raw_events" and r._field == "last_lap_time")
  |> keep(columns: ["_time", "session_id"])
"""
sid_counts: dict[str, int] = {}
for t in q.query(flux):
    for r in t.records:
        sid = r.values.get("session_id")
        if sid:
            sid_counts[sid] = sid_counts.get(sid, 0) + 1
print("raw session_ids:", sid_counts)

# 跳過已知空殼 auto_idle 歸檔（best 全 0），挑事件最多的一節
skip = {"sess-20260712-033531"}
candidates = {k: v for k, v in sid_counts.items() if k not in skip}
if not candidates:
    candidates = sid_counts
if not candidates:
    session_id = datetime.now(timezone.utc).strftime("sess-%Y%m%d-%H%M%S")
    started = datetime.now(timezone.utc)
else:
    session_id = sorted(candidates.items(), key=lambda x: x[1], reverse=True)[0][0]
    try:
        started = datetime.strptime(session_id[5:], "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        started = datetime.now(timezone.utc)

print("archiving as", session_id, "started", started.isoformat())
ended = datetime.now(timezone.utc)
points = []
for tid, st in states.items():
    best = float(st.get("best_lap_time") or 0.0)
    last = float(st.get("last_lap_time") or 0.0)
    laps = int(st.get("lap_count") or 0)
    hist = st.get("lap_history") or []
    tid_u = tid.upper()
    registered = tid_u in {k.upper() for k in CAR_MAP}
    car_tag = CAR_MAP[tid_u] if registered else f"?{tid}"
    points.append(
        Point("session_archive")
        .tag("session_id", session_id)
        .tag("transponder_id", tid_u)
        .tag("car_number", car_tag)
        .field("registered", registered)
        .field("lap_count", laps)
        .field("best_lap_time", best)
        .field("last_lap_time", last)
        .field("lap_history_json", json.dumps(hist))
        .field("reset_trigger", "manual")
        .field("session_started_at", started.timestamp())
        .time(ended)
    )
    print(f"  {car_tag:>3} {tid} laps={laps} best={best:.3f}")

write = client.write_api(write_options=SYNCHRONOUS)
write.write(bucket=bucket, org=os.getenv("INFLUX_ORG"), record=points)
print(f"wrote {len(points)} archive points")

open("/home/evan/kpp/services/decoder_ingest/session_snapshot.json", "w").write(
    json.dumps({"states": {}}, indent=2)
)
print("cleared snapshot")

flux_v = f"""
from(bucket: "{bucket}")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "session_archive" and r.session_id == "{session_id}")
  |> filter(fn: (r) => r._field == "best_lap_time")
"""
rows = []
for t in q.query(flux_v):
    for r in t.records:
        rows.append((r.values.get("car_number"), r.values.get("transponder_id"), r.get_value()))
rows.sort(key=lambda x: x[2] if x[2] else 999)
print("verify best laps:")
for i, (car, tid, best) in enumerate(rows, 1):
    print(f"  {i}. car={car} tid={tid} best={best}")
print("SESSION_ID=" + session_id)

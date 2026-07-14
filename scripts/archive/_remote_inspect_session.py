"""Inspect Influx + snapshot on chuck (run on Pi)."""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

load_dotenv("/home/evan/kpp/.env")
client = InfluxDBClient(
    url=os.getenv("INFLUX_URL"),
    token=os.getenv("INFLUX_TOKEN"),
    org=os.getenv("INFLUX_ORG"),
)
q = client.query_api()
bucket = os.getenv("INFLUX_BUCKET", "decoder")

flux = f"""
from(bucket: "{bucket}")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "decoder_raw_events" and r._field == "last_lap_time")
  |> keep(columns: ["_time", "session_id", "transponder_id", "_value"])
"""
tables = q.query(flux)
by: dict[str, dict[str, int]] = {}
for t in tables:
    for r in t.records:
        sid = r.values.get("session_id")
        tid = r.values.get("transponder_id")
        by.setdefault(sid, {}).setdefault(tid, 0)
        by[sid][tid] += 1
print("raw event sessions:")
for sid, tids in sorted(by.items(), key=lambda x: x[0], reverse=True)[:10]:
    print(sid, "cars", len(tids), "events", sum(tids.values()))
    print(" ", sorted(tids.keys()))

snap = json.load(open("/home/evan/kpp/services/decoder_ingest/session_snapshot.json"))
print("\nsnapshot n=", len(snap.get("states") or {}))
for tid, st in sorted(
    snap["states"].items(),
    key=lambda kv: -(kv[1].get("best_lap_time") or 0)
    if kv[1].get("best_lap_time")
    else 999,
):
    print(
        tid,
        "laps",
        st.get("lap_count"),
        "best",
        st.get("best_lap_time"),
        "last",
        st.get("last_lap_time"),
        "hist",
        len(st.get("lap_history") or []),
    )

flux_a = f"""
from(bucket: "{bucket}")
  |> range(start: -2d)
  |> filter(fn: (r) => r._measurement == "session_archive")
  |> filter(fn: (r) => r.session_id == "sess-20260712-033531")
  |> pivot(rowKey: ["_time", "transponder_id", "car_number"], columnKey: ["_field"], valueColumn: "_value")
"""
print("\narchive sess-20260712-033531:")
for t in q.query(flux_a):
    for r in t.records:
        print(
            r.values.get("transponder_id"),
            r.values.get("car_number"),
            "laps",
            r.values.get("lap_count"),
            "best",
            r.values.get("best_lap_time"),
            "trig",
            r.values.get("reset_trigger"),
        )

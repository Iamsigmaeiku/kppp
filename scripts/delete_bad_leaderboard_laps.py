"""刪除全站榜異常圈速（ex: 14 號 26.196s 提早歸檔）。

用法（在 repo root）：
  python scripts/delete_bad_leaderboard_laps.py
  python scripts/delete_bad_leaderboard_laps.py --max-lap 35 --car 14 --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-lap", type=float, default=35.0, help="刪除 best_lap_time < 此值")
    ap.add_argument("--car", default="", help="可選：只刪特定車號")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.getenv("INFLUX_URL", "").strip()
    token = os.getenv("INFLUX_TOKEN", "").strip()
    org = os.getenv("INFLUX_ORG", "kpp").strip()
    bucket = os.getenv("INFLUX_BUCKET", "decoder").strip()
    if not url or not token:
        print("INFLUX_URL / INFLUX_TOKEN 未設定", file=sys.stderr)
        return 1

    client = InfluxDBClient(url=url, token=token, org=org)
    query_api = client.query_api()
    delete_api = client.delete_api()

    car_filter = f' and r.car_number == "{args.car}"' if args.car else ""
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -3650d)
  |> filter(fn: (r) => r._measurement == "session_archive"
      and r._field == "best_lap_time"
      and r._value > 0.0
      and r._value < {args.max_lap:.3f}{car_filter})
  |> group()
'''
    tables = query_api.query(flux)
    hits: list[dict] = []
    for table in tables:
        for rec in table.records:
            hits.append(
                {
                    "time": rec.get_time(),
                    "value": rec.get_value(),
                    "session_id": rec.values.get("session_id"),
                    "transponder_id": rec.values.get("transponder_id"),
                    "car_number": rec.values.get("car_number"),
                }
            )

    if not hits:
        print(f"沒找到 best_lap_time < {args.max_lap} 的 archive 點")
        client.close()
        return 0

    print(f"找到 {len(hits)} 筆：")
    for h in hits:
        print(
            f"  car={h['car_number']} best={h['value']:.3f}s "
            f"sess={h['session_id']} tid={h['transponder_id']} @{h['time']}"
        )

    if args.dry_run:
        print("dry-run：未刪除")
        client.close()
        return 0

    # 對每個 (session_id, transponder_id) 刪整段 session_archive（該車該節）
    seen: set[tuple[str, str]] = set()
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    stop = datetime.now(timezone.utc)
    for h in hits:
        key = (str(h["session_id"] or ""), str(h["transponder_id"] or ""))
        if key in seen or not key[0]:
            continue
        seen.add(key)
        predicate = (
            f'_measurement="session_archive" '
            f'AND session_id="{key[0]}" '
            f'AND transponder_id="{key[1]}"'
        )
        print(f"delete {predicate}")
        delete_api.delete(start, stop, predicate, bucket=bucket, org=org)

    print(f"已刪 {len(seen)} 組 series")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

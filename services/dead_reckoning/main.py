"""Poll kart_telemetry (gz + GPS fields), run dead reckoning, write position_est measurement."""
from __future__ import annotations

import logging
import time

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .config import (
    BUCKET,
    GPS_COURSE_MIN_SPEED_MPS,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    MEASUREMENT_IMU,
    MEASUREMENT_POSITION,
    POLL_INTERVAL_SEC,
)
from .reckoner import DeadReckoner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [dead_reckoning] %(message)s",
)
logger = logging.getLogger(__name__)


def poll_loop(interval_sec: float = POLL_INTERVAL_SEC) -> None:
    if not INFLUX_TOKEN:
        raise SystemExit("INFLUX_TOKEN is empty — set it in .env")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)

    reckoner = DeadReckoner(gps_course_min_speed_mps=GPS_COURSE_MIN_SPEED_MPS)
    last_ts = None

    flux = f'''
from(bucket:"{BUCKET}")
  |> range(start: -2s)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_IMU}")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''

    logger.info(
        "polling %s/%s -> %s @ %s (interval=%.3fs)",
        BUCKET,
        MEASUREMENT_IMU,
        MEASUREMENT_POSITION,
        INFLUX_URL,
        interval_sec,
    )

    try:
        while True:
            tables = query_api.query(flux)
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    if last_ts is not None and ts <= last_ts:
                        continue

                    gz = record.values.get("gz")
                    if gz is None:
                        continue

                    dt = (ts - last_ts).total_seconds() if last_ts else interval_sec
                    if dt <= 0 or dt > 1.0:
                        dt = interval_sec

                    fused = reckoner.update(
                        dt=dt,
                        gz_dps=float(gz),
                        gps_lat=record.values.get("gps_lat"),
                        gps_lon=record.values.get("gps_lon"),
                        gps_speed_mps=record.values.get("gps_speed_mps"),
                        gps_course_deg=record.values.get("gps_course_deg"),
                    )
                    last_ts = ts

                    if fused is None:
                        continue  # 還沒有第一個 GPS fix，無法輸出位置

                    point = (
                        Point(MEASUREMENT_POSITION)
                        .tag("source", fused.source)
                        .field("lat_est", fused.lat)
                        .field("lon_est", fused.lon)
                        .field("heading_deg", fused.heading_deg)
                        .field("speed_mps", fused.speed_mps)
                        .time(ts, WritePrecision.NS)
                    )
                    device_id = record.values.get("device_id")
                    if device_id:
                        point = point.tag("device_id", str(device_id))

                    write_api.write(bucket=BUCKET, record=point)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        client.close()


def main() -> None:
    poll_loop()


if __name__ == "__main__":
    main()

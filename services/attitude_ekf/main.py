"""Poll kart_telemetry IMU samples, run Attitude EKF, write attitude measurement."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .config import (
    BUCKET,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    MEASUREMENT_ATTITUDE,
    MEASUREMENT_IMU,
    POLL_INTERVAL_SEC,
)
from .ekf import AttitudeEKF

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [attitude_ekf] %(message)s",
)
logger = logging.getLogger(__name__)


def _flux_for_imu(last_ts: datetime | None) -> str:
    if last_ts is None:
        range_clause = "|> range(start: -2s)"
    else:
        start = last_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        range_clause = f'|> range(start: time(v: "{start}"))'

    return f'''
from(bucket:"{BUCKET}")
  {range_clause}
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_IMU}")
  |> filter(fn: (r) => r._field == "gx" or r._field == "gy" or r._field == "ax" or r._field == "ay" or r._field == "az")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''


def poll_loop(interval_sec: float = POLL_INTERVAL_SEC) -> None:
    if not INFLUX_TOKEN:
        raise SystemExit("INFLUX_TOKEN is empty — set it in .env")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)

    ekf = AttitudeEKF()
    last_ts: datetime | None = None
    latest_gx: float | None = None
    latest_gy: float | None = None

    logger.info(
        "polling %s/%s -> %s @ %s (interval=%.3fs)",
        BUCKET,
        MEASUREMENT_IMU,
        MEASUREMENT_ATTITUDE,
        INFLUX_URL,
        interval_sec,
    )

    try:
        while True:
            loop_started = time.perf_counter()

            if latest_gx is not None and latest_gy is not None:
                ekf.predict(latest_gx, latest_gy, interval_sec)

            tables = query_api.query(_flux_for_imu(last_ts))
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    if last_ts is not None and ts <= last_ts:
                        continue

                    gx = record.values.get("gx")
                    gy = record.values.get("gy")
                    ax = record.values.get("ax")
                    ay = record.values.get("ay")
                    az = record.values.get("az")
                    if None in (gx, gy, ax, ay, az):
                        continue

                    latest_gx = float(gx)
                    latest_gy = float(gy)
                    roll, pitch = ekf.update(float(ax), float(ay), float(az))

                    point = (
                        Point(MEASUREMENT_ATTITUDE)
                        .field("roll_deg", float(roll * 57.29578))
                        .field("pitch_deg", float(pitch * 57.29578))
                        .time(ts, WritePrecision.NS)
                    )
                    device_id = record.values.get("device_id")
                    if device_id:
                        point = point.tag("device_id", str(device_id))

                    write_api.write(bucket=BUCKET, record=point)
                    last_ts = ts

            sleep_for = interval_sec - (time.perf_counter() - loop_started)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        client.close()


def main() -> None:
    poll_loop()


if __name__ == "__main__":
    main()

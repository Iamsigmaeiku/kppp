"""Poll kart_telemetry IMU samples, run Attitude EKF, write attitude measurement."""
from __future__ import annotations

import logging
import time

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


def poll_loop(interval_sec: float = POLL_INTERVAL_SEC) -> None:
    if not INFLUX_TOKEN:
        raise SystemExit("INFLUX_TOKEN is empty — set it in .env")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()
    write_api = client.write_api(write_options=SYNCHRONOUS)

    ekf = AttitudeEKF()
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
        MEASUREMENT_ATTITUDE,
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

                    gx = record.values.get("gx")
                    gy = record.values.get("gy")
                    ax = record.values.get("ax")
                    ay = record.values.get("ay")
                    az = record.values.get("az")
                    if None in (gx, gy, ax, ay, az):
                        continue

                    dt = (ts - last_ts).total_seconds() if last_ts else interval_sec
                    if dt <= 0 or dt > 1.0:
                        dt = interval_sec

                    ekf.predict(float(gx), float(gy), dt)
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
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        client.close()


def main() -> None:
    poll_loop()


if __name__ == "__main__":
    main()

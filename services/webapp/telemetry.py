"""ESP32 遙測 ingest：Bearer token 驗證後寫入 InfluxDB measurement kart_telemetry。

若 sample 含 DR 欄位，另寫 measurement dr_position（不覆蓋 gps_* raw）。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telemetry")

MEASUREMENT = "kart_telemetry"
MEASUREMENT_DR = "dr_position"


class TelemetrySample(BaseModel):
    ax: float | None = None
    ay: float | None = None
    az: float | None = None
    gx: float | None = None
    gy: float | None = None
    gz: float | None = None
    imu_temp_c: float | None = None
    accel_mag: float | None = None
    dht_temp_c: float | None = None
    dht_humidity: float | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_speed_mps: float | None = None
    gps_course_deg: float | None = None
    gps_alt_m: float | None = None
    gps_hdop: float | None = None
    gps_satellites: int | None = None
    hall_adc: float | None = None
    hall_hz: float | None = None
    lat_dr: float | None = None
    lon_dr: float | None = None
    dr_heading_deg: float | None = None
    dr_speed_mps: float | None = None
    ts_ms: int | None = None


class TelemetryIngestBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    car_id: str | None = Field(default=None, max_length=32)
    samples: list[TelemetrySample] = Field(min_length=1, max_length=200)


def _require_ingest_token(request: Request, authorization: str | None) -> None:
    expected = request.app.state.web_config.telemetry_ingest_token
    if not expected:
        raise HTTPException(status_code=503, detail="telemetry ingest not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


def _sample_time(sample: TelemetrySample) -> datetime:
    if sample.ts_ms is not None:
        return datetime.fromtimestamp(sample.ts_ms / 1000.0, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _sample_to_point(device_id: str, car_id: str | None, sample: TelemetrySample) -> Point:
    point = Point(MEASUREMENT).tag("device_id", device_id)
    if car_id:
        point = point.tag("car_id", car_id)

    fields: dict[str, Any] = {
        "ax": sample.ax,
        "ay": sample.ay,
        "az": sample.az,
        "gx": sample.gx,
        "gy": sample.gy,
        "gz": sample.gz,
        "imu_temp_c": sample.imu_temp_c,
        "dht_temp_c": sample.dht_temp_c,
        "dht_humidity": sample.dht_humidity,
        "gps_lat": sample.gps_lat,
        "gps_lon": sample.gps_lon,
        "gps_speed_mps": sample.gps_speed_mps,
        "gps_course_deg": sample.gps_course_deg,
        "gps_alt_m": sample.gps_alt_m,
        "gps_hdop": sample.gps_hdop,
        "gps_satellites": sample.gps_satellites,
        "hall_adc": sample.hall_adc,
        "hall_hz": sample.hall_hz,
    }
    accel_mag = sample.accel_mag
    if accel_mag is None and sample.ax is not None and sample.ay is not None and sample.az is not None:
        accel_mag = math.sqrt(sample.ax**2 + sample.ay**2 + sample.az**2)
    fields["accel_mag"] = accel_mag

    for key, value in fields.items():
        if value is not None:
            point = point.field(key, float(value))

    if sample.gps_lat is not None and sample.gps_lon is not None:
        point = point.tag("gps_fix", "1")

    return point.time(_sample_time(sample))


def _sample_to_dr_point(
    device_id: str, car_id: str | None, sample: TelemetrySample
) -> Point | None:
    if sample.lat_dr is None or sample.lon_dr is None:
        return None
    point = Point(MEASUREMENT_DR).tag("device_id", device_id)
    if car_id:
        point = point.tag("car_id", car_id)
    point = point.field("lat_dr", float(sample.lat_dr)).field(
        "lon_dr", float(sample.lon_dr)
    )
    if sample.dr_heading_deg is not None:
        point = point.field("heading_deg", float(sample.dr_heading_deg))
    if sample.dr_speed_mps is not None:
        point = point.field("speed_mps", float(sample.dr_speed_mps))
    return point.time(_sample_time(sample))


@router.post("/ingest")
async def ingest_telemetry(
    request: Request,
    body: TelemetryIngestBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_ingest_token(request, authorization)

    influx_cfg = request.app.state.influx_reader._config
    points: list[Point] = []
    for s in body.samples:
        points.append(_sample_to_point(body.device_id, body.car_id, s))
        dr_pt = _sample_to_dr_point(body.device_id, body.car_id, s)
        if dr_pt is not None:
            points.append(dr_pt)

    client = InfluxDBClientAsync(
        url=influx_cfg.url, token=influx_cfg.token, org=influx_cfg.org
    )
    try:
        write_api = client.write_api()
        await write_api.write(bucket=influx_cfg.bucket, org=influx_cfg.org, record=points)
    except Exception as exc:
        logger.exception("telemetry ingest write failed")
        raise HTTPException(status_code=502, detail=f"influx write failed: {exc}") from exc
    finally:
        await client.close()

    # 快取最後一包（全域 + per-device），給 /telemetry 狀態列用
    last = body.samples[-1]
    entry = {
        "device_id": body.device_id,
        "car_id": body.car_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sample": last.model_dump(),
    }
    request.app.state.telemetry_last = entry
    by_device = getattr(request.app.state, "telemetry_by_device", None)
    if not isinstance(by_device, dict):
        by_device = {}
        request.app.state.telemetry_by_device = by_device
    by_device[body.device_id] = entry

    return {"status": "ok", "written": len(points)}


@router.get("/status")
async def telemetry_status(request: Request) -> dict:
    last = getattr(request.app.state, "telemetry_last", None)
    by_device = getattr(request.app.state, "telemetry_by_device", None)
    devices = by_device if isinstance(by_device, dict) else {}
    return {"last": last, "devices": devices}

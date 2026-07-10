"""ESP32 遙測 ingest：Bearer token 驗證後寫入 InfluxDB measurement kart_telemetry。"""

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
    }
    accel_mag = sample.accel_mag
    if accel_mag is None and sample.ax is not None and sample.ay is not None and sample.az is not None:
        accel_mag = math.sqrt(sample.ax**2 + sample.ay**2 + sample.az**2)
    fields["accel_mag"] = accel_mag

    for key, value in fields.items():
        if value is not None:
            point = point.field(key, float(value))

    if sample.ts_ms is not None:
        point = point.time(datetime.fromtimestamp(sample.ts_ms / 1000.0, tz=timezone.utc))
    else:
        point = point.time(datetime.now(timezone.utc))
    return point


@router.post("/ingest")
async def ingest_telemetry(
    request: Request,
    body: TelemetryIngestBody,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_ingest_token(request, authorization)

    influx_cfg = request.app.state.influx_reader._config
    points = [_sample_to_point(body.device_id, body.car_id, s) for s in body.samples]

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

    # 快取最後一包，給 /telemetry 狀態列用
    last = body.samples[-1]
    request.app.state.telemetry_last = {
        "device_id": body.device_id,
        "car_id": body.car_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sample": last.model_dump(),
    }

    return {"status": "ok", "written": len(points)}


@router.get("/status")
async def telemetry_status(request: Request) -> dict:
    last = getattr(request.app.state, "telemetry_last", None)
    return {"last": last}

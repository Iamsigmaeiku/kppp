"""ESP32 遙測 ingest：Bearer token 驗證後寫入 InfluxDB measurement kart_telemetry。

若 sample 含 DR 欄位，另寫 measurement dr_position（不覆蓋 gps_* raw）。
若 sample 含 gps_tracks[]，另寫 measurement gps_track。
雙板主路徑改走 UDP（見 udp_telemetry.py）；本 HTTP endpoint 仍作 fallback。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telemetry")

MEASUREMENT = "kart_telemetry"
MEASUREMENT_ATTITUDE = "attitude"
MEASUREMENT_DR = "dr_position"
MEASUREMENT_GPS_TRACK = "gps_track"


class GpsTrackFix(BaseModel):
    """Dual-GPS Route：同一 measurement，用 device tag 分線。"""

    device: Literal["m10180c", "neo6m"]
    lat: float
    lon: float
    alt: float | None = None
    speed_mps: float | None = None
    course_deg: float | None = None
    hdop: float | None = None
    sats: int | None = None


class TelemetrySample(BaseModel):
    ax: float | None = None
    ay: float | None = None
    az: float | None = None
    gx: float | None = None
    gy: float | None = None
    gz: float | None = None
    imu_temp_c: float | None = None
    accel_mag: float | None = None
    accel_dyn: float | None = None  # |a - g| after LPF gravity removal
    a_lon: float | None = None  # horizontal dynamical (X when gravity≈+Y)
    a_lat: float | None = None  # horizontal dynamical (Z when gravity≈+Y)
    dht_temp_c: float | None = None
    dht_humidity: float | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_speed_mps: float | None = None
    gps_course_deg: float | None = None
    gps_alt_m: float | None = None
    gps_hdop: float | None = None
    gps_satellites: int | None = None
    gps_fresh: float | None = None  # 1=fresh fix, 0=held stale
    # MPU6050 on sensor_node (type 0x04) — secondary IMU
    mpu_ax: float | None = None
    mpu_ay: float | None = None
    mpu_az: float | None = None
    mpu_gx: float | None = None
    mpu_gy: float | None = None
    mpu_gz: float | None = None
    mpu_temp_c: float | None = None
    imu_fault: float | None = None  # 1 if dual-IMU consistency fail
    lat_dr: float | None = None
    lon_dr: float | None = None
    dr_heading_deg: float | None = None
    dr_speed_mps: float | None = None
    gps_tracks: list[GpsTrackFix] | None = None
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
    # ESP micros()/1000 不是 Unix ms；小於 2020-01-01 的一律當板子 uptime，改用伺服器時間
    if sample.ts_ms is not None and sample.ts_ms >= 1_577_836_800_000:
        return datetime.fromtimestamp(sample.ts_ms / 1000.0, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _is_fake_zero_imu(sample: TelemetrySample) -> bool:
    """DHT/GPS-only placeholder：ax=ay=az=0（或缺）且 accel_mag≈0 → 不要灌假零進 Influx。"""
    ax, ay, az = sample.ax, sample.ay, sample.az
    if ax is None and ay is None and az is None and sample.accel_mag is None:
        return False  # 根本沒送 IMU；下面也不會寫
    vals = [v for v in (ax, ay, az) if v is not None]
    if vals and any(abs(v) > 1e-6 for v in vals):
        return False
    if sample.accel_mag is not None and abs(sample.accel_mag) > 1e-6:
        return False
    # 全 0 或只送了 0
    return bool(vals) or (sample.accel_mag is not None and abs(sample.accel_mag) <= 1e-6)


def _sample_to_point(device_id: str, car_id: str | None, sample: TelemetrySample) -> Point:
    point = Point(MEASUREMENT).tag("device_id", device_id)
    if car_id:
        point = point.tag("car_id", car_id)

    skip_imu = _is_fake_zero_imu(sample)
    fields: dict[str, Any] = {
        "dht_temp_c": sample.dht_temp_c,
        "dht_humidity": sample.dht_humidity,
        "gps_lat": sample.gps_lat,
        "gps_lon": sample.gps_lon,
        "gps_speed_mps": sample.gps_speed_mps,
        "gps_course_deg": sample.gps_course_deg,
        "gps_alt_m": sample.gps_alt_m,
        "gps_hdop": sample.gps_hdop,
        "gps_satellites": sample.gps_satellites,
        "gps_fresh": sample.gps_fresh,
        "mpu_ax": sample.mpu_ax,
        "mpu_ay": sample.mpu_ay,
        "mpu_az": sample.mpu_az,
        "mpu_gx": sample.mpu_gx,
        "mpu_gy": sample.mpu_gy,
        "mpu_gz": sample.mpu_gz,
        "mpu_temp_c": sample.mpu_temp_c,
        "imu_fault": sample.imu_fault,
    }
    if not skip_imu:
        fields.update(
            {
                "ax": sample.ax,
                "ay": sample.ay,
                "az": sample.az,
                "gx": sample.gx,
                "gy": sample.gy,
                "gz": sample.gz,
                "imu_temp_c": sample.imu_temp_c,
                "accel_dyn": sample.accel_dyn,
                "a_lon": sample.a_lon,
                "a_lat": sample.a_lat,
            }
        )
        accel_mag = sample.accel_mag
        if (
            accel_mag is None
            and sample.ax is not None
            and sample.ay is not None
            and sample.az is not None
        ):
            accel_mag = math.sqrt(sample.ax**2 + sample.ay**2 + sample.az**2)
        fields["accel_mag"] = accel_mag

    for key, value in fields.items():
        if value is not None:
            point = point.field(key, float(value))

    # 只標真 fresh fix（gps_fresh>=0.5）；None/0 不當 gps_fix，避免 50Hz 重複點
    if (
        sample.gps_lat is not None
        and sample.gps_lon is not None
        and sample.gps_fresh is not None
        and sample.gps_fresh >= 0.5
    ):
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


def _gps_track_points(
    device_id: str, car_id: str | None, sample: TelemetrySample
) -> list[Point]:
    """Hybrid dual-GPS：每顆模組獨立 Point，tag device + device_id。"""
    if not sample.gps_tracks:
        return []
    ts = _sample_time(sample)
    points: list[Point] = []
    for fix in sample.gps_tracks:
        point = (
            Point(MEASUREMENT_GPS_TRACK)
            .tag("device_id", device_id)
            .tag("device", fix.device)
            .field("lat", float(fix.lat))
            .field("lon", float(fix.lon))
        )
        if car_id:
            point = point.tag("car_id", car_id)
        if fix.alt is not None:
            point = point.field("alt", float(fix.alt))
        if fix.speed_mps is not None:
            point = point.field("speed", float(fix.speed_mps))
        if fix.course_deg is not None:
            point = point.field("course", float(fix.course_deg))
        if fix.hdop is not None:
            point = point.field("hdop", float(fix.hdop))
        if fix.sats is not None:
            point = point.field("sats", float(fix.sats))
        points.append(point.time(ts))
    return points


async def _latest_attitude_by_device(
    request: Request, device_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not device_ids:
        return {}

    quoted_ids = ", ".join(f'"{device_id}"' for device_id in sorted(set(device_ids)))
    flux = f'''
from(bucket: "{request.app.state.influx_reader._config.bucket}")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_ATTITUDE}")
  |> filter(fn: (r) => contains(value: r.device_id, set: [{quoted_ids}]))
  |> filter(fn: (r) => r._field == "roll_deg" or r._field == "pitch_deg")
  |> group(columns: ["device_id", "_field"])
  |> last()
  |> pivot(rowKey: ["device_id", "_time"], columnKey: ["_field"], valueColumn: "_value")
'''

    out: dict[str, dict[str, Any]] = {}
    try:
        tables = await request.app.state.influx_reader._query(flux)
    except Exception:
        logger.exception("telemetry attitude query failed")
        return out

    for table in tables:
        for record in table.records:
            device_id = record.values.get("device_id")
            if not device_id:
                continue
            out[str(device_id)] = {
                "roll_deg": record.values.get("roll_deg"),
                "pitch_deg": record.values.get("pitch_deg"),
                "attitude_at": (
                    record.get_time().astimezone(timezone.utc).isoformat()
                    if record.get_time() is not None
                    else None
                ),
            }
    return out


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
        points.extend(_gps_track_points(body.device_id, body.car_id, s))
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
    sample_dict = last.model_dump()
    by_device = getattr(request.app.state, "telemetry_by_device", None)
    if not isinstance(by_device, dict):
        by_device = {}
        request.app.state.telemetry_by_device = by_device

    # IMU 抽樣常夾不到當下 GPS 欄位：保留上一包 GPS，避免狀態列狂閃「尚未定位」
    prev = by_device.get(body.device_id)
    if sample_dict.get("gps_lat") is None and isinstance(prev, dict):
        prev_sample = prev.get("sample") or {}
        if isinstance(prev_sample, dict) and prev_sample.get("gps_lat") is not None:
            for k in (
                "gps_lat",
                "gps_lon",
                "gps_speed_mps",
                "gps_course_deg",
                "gps_alt_m",
                "gps_hdop",
                "gps_satellites",
                "gps_fresh",
            ):
                if sample_dict.get(k) is None and prev_sample.get(k) is not None:
                    sample_dict[k] = prev_sample[k]

    entry = {
        "device_id": body.device_id,
        "car_id": body.car_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sample": sample_dict,
    }
    request.app.state.telemetry_last = entry
    by_device[body.device_id] = entry

    try:
        from .position_ws import notify_position_from_ingest

        await notify_position_from_ingest(
            request,
            device_id=body.device_id,
            car_id=body.car_id,
            received_at=entry["received_at"],
            sample=sample_dict,
        )
    except Exception:
        logger.exception("position broadcast failed device_id=%s", body.device_id)

    return {"status": "ok", "written": len(points)}


@router.post("/frame-ingest")
async def ingest_frame_batch(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """wifi_node 熱點模式：POST 與 UDP 相同之 0xAA55 封包串（application/octet-stream）。"""
    _require_ingest_token(request, authorization)
    body = await request.body()
    if not body:
        return {"status": "ok", "frames": 0}

    server = getattr(request.app.state, "udp_telemetry", None)
    if server is None:
        raise HTTPException(
            status_code=503, detail="binary telemetry decoder not running on server"
        )
    frames = server.feed_bytes(body)
    return {"status": "ok", "frames": frames}


@router.get("/status")
async def telemetry_status(request: Request) -> dict:
    from .deps import get_current_user
    from .telemetry_access import can_view_telemetry

    user = await get_current_user(request)
    if not can_view_telemetry(user):
        raise HTTPException(status_code=403, detail="telemetry access denied")

    last = getattr(request.app.state, "telemetry_last", None)
    by_device = getattr(request.app.state, "telemetry_by_device", None)
    devices = by_device if isinstance(by_device, dict) else {}

    device_ids = [str(k) for k in devices.keys()]
    if not device_ids and isinstance(last, dict) and last.get("device_id"):
        device_ids = [str(last["device_id"])]

    attitude_by_device = await _latest_attitude_by_device(request, device_ids)

    out_devices: dict[str, Any] = {}
    for device_id, entry in devices.items():
        if not isinstance(entry, dict):
            out_devices[str(device_id)] = entry
            continue
        merged = dict(entry)
        sample = dict(entry.get("sample") or {})
        sample.update(attitude_by_device.get(str(device_id), {}))
        merged["sample"] = sample
        out_devices[str(device_id)] = merged

    out_last = last
    if isinstance(last, dict) and last.get("device_id"):
        out_last = dict(last)
        sample = dict(last.get("sample") or {})
        sample.update(attitude_by_device.get(str(last["device_id"]), {}))
        out_last["sample"] = sample
    return {"last": out_last, "devices": out_devices}

"""Session 級 RTS 平滑：Influx 讀 → smooth_track → 寫 track_smoothed。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from services.decoder_ingest.config import InfluxConfig
from services.decoder_ingest.influx_reader import InfluxReader
from services.postprocess.rts_smoother import (
    SmoothInput,
    SmoothOutput,
    smooth_track,
    smooth_track_ctrv,
)
from services.webapp.track_coords import latlng_to_local_m

logger = logging.getLogger(__name__)

MEASUREMENT = "track_smoothed"
@dataclass(slots=True)
class SmoothRunResult:
    session_id: str
    device_id: str
    n_input: int
    n_output: int
    n_gap: int
    outputs: list[SmoothOutput]


async def fetch_gps_smooth_inputs(
    reader: InfluxReader,
    *,
    device_id: str,
    start: datetime,
    stop: datetime,
) -> list[SmoothInput]:
    """從 kart_telemetry 撈 gps_fix=1 的位置 + 可選 hdop/speed/hall。"""
    start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    stop_iso = stop.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    bucket = reader._config.bucket
    query = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}")) '
        f'|> filter(fn: (r) => r._measurement == "kart_telemetry" '
        f'and r.device_id == "{device_id}" and r.gps_fix == "1") '
        f'|> filter(fn: (r) => r._field == "gps_lat" or r._field == "gps_lon" '
        f'or r._field == "gps_hdop" or r._field == "gps_speed_mps" '
        f'or r._field == "gps_course_deg" or r._field == "gps_h_acc_mm" '
        f'or r._field == "pps_age_ms" or r._field == "hall_hz") '
        f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
        f'|> group() '
        f'|> sort(columns: ["_time"])'
    )
    tables = await reader._query(query)
    out: list[SmoothInput] = []
    for table in tables:
        for record in table.records:
            v = record.values
            lat = v.get("gps_lat")
            lon = v.get("gps_lon")
            ts = record.get_time()
            if lat is None or lon is None or ts is None:
                continue
            x_m, y_m = latlng_to_local_m(float(lat), float(lon))
            hdop = v.get("gps_hdop")
            speed = v.get("gps_speed_mps")
            hall = v.get("hall_hz")
            course = v.get("gps_course_deg")
            h_acc_mm = v.get("gps_h_acc_mm")
            pps_age = v.get("pps_age_ms")
            out.append(
                SmoothInput(
                    t=ts,
                    x_m=x_m,
                    y_m=y_m,
                    hdop=float(hdop) if hdop is not None else None,
                    speed_mps=float(speed) if speed is not None else None,
                    hall_hz=float(hall) if hall is not None else None,
                    course_deg=float(course) if course is not None else None,
                    h_acc_m=float(h_acc_mm) / 1000.0 if h_acc_mm is not None else None,
                    pps_age_ms=float(pps_age) if pps_age is not None else None,
                )
            )
    return out


def _delete_existing(
    cfg: InfluxConfig,
    *,
    session_id: str,
    start: datetime,
    stop: datetime,
) -> None:
    """刪同 session 舊 track_smoothed（冪等重跑）。"""
    client = InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org)
    try:
        predicate = f'_measurement="{MEASUREMENT}" AND session_id="{session_id}"'
        client.delete_api().delete(start, stop, predicate, bucket=cfg.bucket, org=cfg.org)
    finally:
        client.close()


def _write_outputs(
    cfg: InfluxConfig,
    *,
    session_id: str,
    device_id: str,
    outputs: list[SmoothOutput],
    model: str,
) -> None:
    client = InfluxDBClient(url=cfg.url, token=cfg.token, org=cfg.org)
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        batch: list[Point] = []
        for o in outputs:
            p = (
                Point(MEASUREMENT)
                .tag("device_id", device_id)
                .tag("session_id", session_id)
                .tag("algo", f"rts_{model}")
                .field("lat_s", float(o.lat))
                .field("lon_s", float(o.lon))
                .field("speed_mps", float(o.speed_mps))
                .field("sigma_m", float(o.sigma_m))
                .field("gap", 1.0 if o.gap else 0.0)
                .time(o.t)
            )
            batch.append(p)
            if len(batch) >= 500:
                write_api.write(bucket=cfg.bucket, org=cfg.org, record=batch)
                batch.clear()
        if batch:
            write_api.write(bucket=cfg.bucket, org=cfg.org, record=batch)
    finally:
        client.close()


async def smooth_session(
    reader: InfluxReader,
    session_id: str,
    *,
    device_id: str | None = None,
    dry_run: bool = False,
    use_speed: bool = True,
    model: str = "cv",
) -> SmoothRunResult:
    """對單一 session 跑 RTS 並（可選）寫回 Influx。"""
    device_id = device_id or reader._TRACK_DEVICE_ID
    bounds = await reader._session_time_bounds(session_id)
    if bounds is None:
        raise ValueError(f"無法解析 session bounds: {session_id}")
    start, stop = bounds
    inputs = await fetch_gps_smooth_inputs(
        reader, device_id=device_id, start=start, stop=stop
    )
    if model == "ctrv":
        outputs = smooth_track_ctrv(inputs)
    elif model == "cv":
        outputs = smooth_track(inputs, use_speed=use_speed)
    else:
        raise ValueError(f"unsupported smoother model: {model}")
    n_gap = sum(1 for o in outputs if o.gap)
    result = SmoothRunResult(
        session_id=session_id,
        device_id=device_id,
        n_input=len(inputs),
        n_output=len(outputs),
        n_gap=n_gap,
        outputs=outputs,
    )
    if dry_run:
        logger.info(
            "smooth dry-run session=%s in=%d out=%d gap=%d",
            session_id,
            result.n_input,
            result.n_output,
            n_gap,
        )
        return result

    cfg = reader._config
    _delete_existing(cfg, session_id=session_id, start=start, stop=stop)
    _write_outputs(
        cfg,
        session_id=session_id,
        device_id=device_id,
        outputs=outputs,
        model=model,
    )
    logger.info(
        "smooth wrote session=%s points=%d gap=%d",
        session_id,
        result.n_output,
        n_gap,
    )
    return result

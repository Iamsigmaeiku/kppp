"""Orin / 邊緣機：訂閱 Influx GPS → fixed-lag RTS → 寫回 track_smoothed。

設計：
- 不跑在 chuck（Pi）熱路徑；Orin 透過 Tailscale 打 chuck:8086。
- 延遲 lag_sec（預設 3s）後才 commit，近似即時平滑。
- algo tag = rts_cv_lag（與賽後 batch rts_cv 區分）。

用法：
  python -m services.postprocess.realtime_smoother
  # 或
  INFLUX_URL=http://100.102.122.104:8086 python -m services.postprocess.realtime_smoother \\
      --device-id esp32-kart-01 --lag 3
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from services.postprocess.rts_smoother import (
    FixedLagState,
    SmoothInput,
    SmoothOutput,
    fixed_lag_commit,
)
from services.webapp.track_coords import latlng_to_local_m

logger = logging.getLogger("realtime_smoother")

MEASUREMENT = "track_smoothed"
ALGO = "rts_cv_lag"
DEFAULT_DEVICE = "esp32-kart-01"


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


def _cfg() -> dict[str, str]:
    url = os.environ.get("INFLUX_URL", "").strip()
    token = os.environ.get("INFLUX_TOKEN", "").strip()
    org = os.environ.get("INFLUX_ORG", "kpp").strip()
    bucket = os.environ.get("INFLUX_BUCKET", "decoder").strip()
    if not url or not token:
        raise SystemExit(
            "需要 INFLUX_URL / INFLUX_TOKEN（.env 或環境變數）。"
            "Orin 範例：INFLUX_URL=http://100.102.122.104:8086"
        )
    return {"url": url, "token": token, "org": org, "bucket": bucket}


def _resolve_session_id(dashboard_base: str | None) -> str:
    """盡量從 chuck dashboard 拿 current session；失敗則 live。"""
    if not dashboard_base:
        return "live"
    url = dashboard_base.rstrip("/") + "/api/session/current"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            import json

            data = json.loads(resp.read().decode())
            sid = (data.get("session_id") or "").strip()
            if sid:
                return sid
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        pass
    return "live"


def _query_gps(
    client: InfluxDBClient,
    *,
    bucket: str,
    org: str,
    device_id: str,
    start: datetime,
    stop: datetime | None = None,
) -> list[SmoothInput]:
    start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    stop_clause = ""
    if stop is not None:
        stop_iso = stop.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        stop_clause = f', stop: time(v: "{stop_iso}")'
    flux = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: time(v: "{start_iso}"){stop_clause}) '
        f'|> filter(fn: (r) => r._measurement == "kart_telemetry" '
        f'and r.device_id == "{device_id}" and r.gps_fix == "1") '
        f'|> filter(fn: (r) => r._field == "gps_lat" or r._field == "gps_lon" '
        f'or r._field == "gps_hdop" or r._field == "gps_speed_mps" '
        f'or r._field == "hall_hz") '
        f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
        f'|> group() '
        f'|> sort(columns: ["_time"])'
    )
    tables = client.query_api().query(flux, org=org)
    out: list[SmoothInput] = []
    for table in tables:
        for record in table.records:
            v = record.values
            lat, lon, ts = v.get("gps_lat"), v.get("gps_lon"), record.get_time()
            if lat is None or lon is None or ts is None:
                continue
            x_m, y_m = latlng_to_local_m(float(lat), float(lon))
            hdop = v.get("gps_hdop")
            speed = v.get("gps_speed_mps")
            hall = v.get("hall_hz")
            out.append(
                SmoothInput(
                    t=ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                    x_m=x_m,
                    y_m=y_m,
                    hdop=float(hdop) if hdop is not None else None,
                    speed_mps=float(speed) if speed is not None else None,
                    hall_hz=float(hall) if hall is not None else None,
                )
            )
    return out


def _write(
    client: InfluxDBClient,
    *,
    bucket: str,
    org: str,
    session_id: str,
    device_id: str,
    outputs: list[SmoothOutput],
) -> None:
    if not outputs:
        return
    write_api = client.write_api(write_options=SYNCHRONOUS)
    batch: list[Point] = []
    for o in outputs:
        batch.append(
            Point(MEASUREMENT)
            .tag("device_id", device_id)
            .tag("session_id", session_id)
            .tag("algo", ALGO)
            .field("lat_s", float(o.lat))
            .field("lon_s", float(o.lon))
            .field("speed_mps", float(o.speed_mps))
            .field("sigma_m", float(o.sigma_m))
            .field("gap", 1.0 if o.gap else 0.0)
            .time(o.t)
        )
    write_api.write(bucket=bucket, org=org, record=batch)


def run_loop(
    *,
    device_id: str,
    lag_sec: float,
    poll_sec: float,
    keep_sec: float,
    dashboard_base: str | None,
    use_speed: bool,
) -> None:
    cfg = _cfg()
    state = FixedLagState(samples=[])
    cursor = datetime.now(timezone.utc) - timedelta(seconds=max(15.0, lag_sec * 3))
    n_written = 0
    logger.info(
        "realtime smoother start device=%s lag=%.1fs influx=%s",
        device_id,
        lag_sec,
        cfg["url"],
    )
    with InfluxDBClient(
        url=cfg["url"], token=cfg["token"], org=cfg["org"], timeout=30_000
    ) as client:
        # ping
        try:
            health = client.health()
            logger.info("influx health status=%s", getattr(health, "status", health))
        except Exception:
            logger.exception("influx health check failed — still looping")

        while True:
            loop_t0 = time.monotonic()
            try:
                now = datetime.now(timezone.utc)
                # 重疊 1s 防漏；fixed_lag 內建 ts 去重
                q_start = cursor - timedelta(seconds=1.0)
                new_pts = _query_gps(
                    client,
                    bucket=cfg["bucket"],
                    org=cfg["org"],
                    device_id=device_id,
                    start=q_start,
                    stop=now,
                )
                if new_pts:
                    cursor = max(cursor, max(s.t for s in new_pts))
                committed = fixed_lag_commit(
                    state,
                    new_pts,
                    lag_sec=lag_sec,
                    keep_sec=keep_sec,
                    use_speed=use_speed,
                )
                if committed:
                    sid = _resolve_session_id(dashboard_base)
                    _write(
                        client,
                        bucket=cfg["bucket"],
                        org=cfg["org"],
                        session_id=sid,
                        device_id=device_id,
                        outputs=committed,
                    )
                    n_written += len(committed)
                    logger.info(
                        "commit n=%d session=%s last_t=%s total_written=%d buf=%d",
                        len(committed),
                        sid,
                        committed[-1].t.isoformat(),
                        n_written,
                        len(state.samples),
                    )
            except Exception:
                logger.exception("poll/write failed")
            elapsed = time.monotonic() - loop_t0
            time.sleep(max(0.05, poll_sec - elapsed))


def main(argv: list[str] | None = None) -> int:
    _load_env()
    p = argparse.ArgumentParser(description="Fixed-lag RTS → track_smoothed (Orin)")
    p.add_argument("--device-id", default=os.environ.get("TRACK_DEVICE_ID", DEFAULT_DEVICE))
    p.add_argument("--lag", type=float, default=float(os.environ.get("RTS_LAG_SEC", "3")))
    p.add_argument("--poll", type=float, default=float(os.environ.get("RTS_POLL_SEC", "0.5")))
    p.add_argument("--keep", type=float, default=float(os.environ.get("RTS_KEEP_SEC", "60")))
    p.add_argument(
        "--dashboard",
        default=os.environ.get("KPP_DASHBOARD_URL", "http://100.102.122.104:8000"),
        help="chuck dashboard base（拿 current session_id）；空字串=永遠 live",
    )
    p.add_argument("--no-speed", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    dash = (args.dashboard or "").strip() or None
    try:
        run_loop(
            device_id=args.device_id,
            lag_sec=args.lag,
            poll_sec=args.poll,
            keep_sec=args.keep,
            dashboard_base=dash,
            use_speed=not args.no_speed,
        )
    except KeyboardInterrupt:
        logger.info("stopped")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

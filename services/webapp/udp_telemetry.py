"""UDP binary telemetry from wifi_node → Influx + live-map cache.

Frame: [0xAA 0x55][type][len][payload][crc16-ccitt LE]
  0x01 ICM IMU, 0x02 GPS, 0x03 fused pose, 0x04 MPU6050
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from datetime import datetime, timezone
from typing import Any, NamedTuple

from fastapi import FastAPI
from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from .telemetry import (
    TelemetrySample,
    _sample_to_dr_point,
    _sample_to_point,
)

logger = logging.getLogger(__name__)

SYNC0 = 0xAA
SYNC1 = 0x55
TYPE_IMU = 0x01
TYPE_GPS = 0x02
TYPE_FUSED = 0x03
TYPE_MPU = 0x04
TYPE_DBG = 0x05

# ICM-42688 / MPU6050 ±16g / ±2000 dps
ACCEL_SENS = 2048.0
GYRO_SENS = 16.4

IMU_WRITE_MIN_SEC = 1.0 / 50.0  # ≤50 Hz into Influx
MPU_WRITE_MIN_SEC = 1.0 / 50.0

GPS_WEEK_MS = 604_800_000  # GPS 週長（毫秒）
ITOW_EPOCH_GUARD_MS = 1_577_836_800_000  # 2020-01-01 Unix ms
GPS_MAX_SPEED_MPS = 45.0  # 跳點剔除速度上限（公尺/秒）
GPS_JUMP_RESET_STREAK = 5  # 連續飛點後重置基準
GPS_MIN_FIX_TYPE = 3  # 最低 fix 類型（3=3D）
GPS_MIN_SV = 6  # 最低衛星數
GPS_MAX_H_ACC_MM = 15_000  # 水平精度上限（毫米）
GPS_REANCHOR_DRIFT_MS = 5_000  # |fix_time - now| 超過此值重新錨定（毫秒）
GPS_ITOW_WRAP_GUARD_MS = 1_000  # iTOW 回捲偵測：低於錨定值減此量（毫秒）
DROP_WARN_INTERVAL_SEC = 30.0  # drop 統計警告間隔（秒）
_EARTH_RADIUS_M = 6_371_000.0  # 地球半徑（公尺）


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """兩點球面距離（公尺）。"""
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class FrameParser:
    def __init__(self) -> None:
        self._state = 0
        self._type = 0
        self._len = 0
        self._idx = 0
        self._buf = bytearray(4 + 255 + 2)

    def reset(self) -> None:
        self._state = 0
        self._type = 0
        self._len = 0
        self._idx = 0

    def feed(self, b: int) -> bytes | None:
        """Return complete valid frame bytes, or None."""
        if self._state == 0:
            if b == SYNC0:
                self._buf[0] = b
                self._state = 1
            return None
        if self._state == 1:
            if b == SYNC1:
                self._buf[1] = b
                self._state = 2
            elif b != SYNC0:
                self._state = 0
            return None
        if self._state == 2:
            self._type = b
            self._buf[2] = b
            self._state = 3
            return None
        if self._state == 3:
            self._len = b
            self._buf[3] = b
            self._idx = 0
            self._state = 4
            return None
        # payload + crc
        self._buf[4 + self._idx] = b
        self._idx += 1
        if self._idx < self._len + 2:
            return None
        flen = 4 + self._len + 2
        got = self._buf[4 + self._len] | (self._buf[5 + self._len] << 8)
        expect = crc16_ccitt(bytes(self._buf[2 : 4 + self._len]))
        frame = bytes(self._buf[:flen])
        self.reset()
        if got != expect:
            return None
        return frame


def _parse_imu(payload: bytes) -> list[TelemetrySample]:
    if not payload:
        return []
    count = payload[0]
    sample_sz = 18
    out: list[TelemetrySample] = []
    off = 1
    for _ in range(count):
        if off + sample_sz > len(payload):
            break
        ts_us, ax, ay, az, gx, gy, gz, temp = struct.unpack_from(
            "<Ihhhhhhh", payload, off
        )
        off += sample_sz
        ax_g = ax / ACCEL_SENS
        ay_g = ay / ACCEL_SENS
        az_g = az / ACCEL_SENS
        out.append(
            TelemetrySample(
                ax=ax_g,
                ay=ay_g,
                az=az_g,
                gx=gx / GYRO_SENS,
                gy=gy / GYRO_SENS,
                gz=gz / GYRO_SENS,
                imu_temp_c=(temp / 132.48) + 25.0 if abs(temp) > 20 else float(temp),
                accel_mag=math.sqrt(ax_g * ax_g + ay_g * ay_g + az_g * az_g),
                ts_ms=ts_us // 1000 if ts_us > 1_000_000_000 else None,
            )
        )
    return out


def _parse_mpu(payload: bytes) -> list[TelemetrySample]:
    """Same layout as 0x01 but maps to mpu_* fields."""
    if not payload:
        return []
    count = payload[0]
    sample_sz = 18
    out: list[TelemetrySample] = []
    off = 1
    for _ in range(count):
        if off + sample_sz > len(payload):
            break
        ts_us, ax, ay, az, gx, gy, gz, temp = struct.unpack_from(
            "<Ihhhhhhh", payload, off
        )
        off += sample_sz
        # MPU6050 temp: Temp_C = temp/340 + 36.53
        out.append(
            TelemetrySample(
                mpu_ax=ax / ACCEL_SENS,
                mpu_ay=ay / ACCEL_SENS,
                mpu_az=az / ACCEL_SENS,
                mpu_gx=gx / GYRO_SENS,
                mpu_gy=gy / GYRO_SENS,
                mpu_gz=gz / GYRO_SENS,
                mpu_temp_c=(temp / 340.0) + 36.53,
                ts_ms=ts_us // 1000 if ts_us > 1_000_000_000 else None,
            )
        )
    return out


class GpsRawFields(NamedTuple):
    itow: int
    lat: int
    lon: int
    height: int
    vel_n: int
    vel_e: int
    vel_d: int
    g_speed: int
    head_mot: int
    h_acc: int
    v_acc: int
    s_acc: int
    num_sv: int
    fix_type: int


def _parse_gps_raw(payload: bytes) -> GpsRawFields | None:
    """Unpack GPS payload；不做品質閘門。"""
    if len(payload) < 50:
        return None
    fields = struct.unpack_from("<IiiiiiiiiIIIBB", payload, 0)
    return GpsRawFields(*fields)


def _parse_fused(payload: bytes) -> TelemetrySample | None:
    if len(payload) < 31:
        return None
    (
        ts_us,
        lat,
        lon,
        height,
        vel_n,
        vel_e,
        vel_d,
        yaw,
        pitch,
        roll,
        pos_std_cm,
        flags,
    ) = struct.unpack_from("<IiiihhhhhhHB", payload, 0)
    if not (flags & 0x01):
        return None
    speed = math.sqrt(vel_n * vel_n + vel_e * vel_e) * 0.01  # cm/s → m/s
    # ts_us 是 ESP micros，不是 Unix；不要當 ts_ms
    _ = ts_us
    return TelemetrySample(
        lat_dr=lat * 1e-7,
        lon_dr=lon * 1e-7,
        dr_heading_deg=yaw * 0.01,
        dr_speed_mps=speed,
        gps_alt_m=height * 1e-3,
        imu_fault=1.0 if (flags & 0x08) else 0.0,
    )


def _parse_dbg(payload: bytes) -> TelemetrySample | None:
    """0x05 sensor debug：即使還沒 0x02 GPS 包，也能顯示搜星數。"""
    if len(payload) < 6:
        return None
    _rx_bps, _pvt_hz, fix_type, num_sv, _res = struct.unpack_from("<HBBBB", payload, 0)
    fresh = 0.0
    if fix_type >= 3:
        fresh = 1.0
    elif fix_type >= 2:
        fresh = 0.5
    return TelemetrySample(gps_satellites=float(num_sv), gps_fresh=fresh)


class UdpTelemetryServer:
    def __init__(
        self,
        app: FastAPI,
        host: str,
        port: int,
        device_id: str,
        car_id: str | None = None,
    ) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._device_id = device_id
        self._car_id = car_id
        self._parser = FrameParser()
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: asyncio.DatagramProtocol | None = None
        self._gps_q: asyncio.Queue[TelemetrySample] = asyncio.Queue(maxsize=256)
        self._imu_q: asyncio.Queue[TelemetrySample] = asyncio.Queue(maxsize=512)
        self._writer_task: asyncio.Task[None] | None = None
        self._drop_warn_task: asyncio.Task[None] | None = None
        self._influx_client: InfluxDBClientAsync | None = None
        self._write_api: Any = None
        self._last_imu_write = 0.0
        self._last_mpu_write = 0.0
        self._crc_err = 0
        self._ok = 0
        self._gps_drops = 0
        self._imu_drops = 0
        # iTOW 錨定狀態
        self._anchor_itow: int | None = None
        self._anchor_server_time: float | None = None  # Unix 秒
        self._last_gps_ts_ms: int | None = None
        # 跳點剔除狀態
        self._last_accepted_gps: tuple[float, float, float] | None = None
        self._rejected_streak = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        class Proto(asyncio.DatagramProtocol):
            def __init__(self, outer: UdpTelemetryServer) -> None:
                self._outer = outer

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                self._outer._on_datagram(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning("udp telemetry error: %s", exc)

        influx_reader = getattr(self._app.state, "influx_reader", None)
        if influx_reader is not None:
            influx_cfg = influx_reader._config
            self._influx_client = InfluxDBClientAsync(
                url=influx_cfg.url, token=influx_cfg.token, org=influx_cfg.org
            )
            self._write_api = self._influx_client.write_api()

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: Proto(self),
            local_addr=(self._host, self._port),
        )
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="udp-tel-writer"
        )
        self._drop_warn_task = asyncio.create_task(
            self._drop_warn_loop(), name="udp-tel-drop-warn"
        )
        logger.info(
            "UDP telemetry listening on %s:%d device_id=%s",
            self._host,
            self._port,
            self._device_id,
        )

    async def stop(self) -> None:
        for task in (self._writer_task, self._drop_warn_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._writer_task = None
        self._drop_warn_task = None
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._influx_client is not None:
            await self._influx_client.close()
            self._influx_client = None
            self._write_api = None

    def feed_bytes(self, data: bytes) -> int:
        """Parse concatenated 0xAA55 frames (same as one UDP datagram). Returns frame count."""
        frames = 0
        for b in data:
            frame = self._parser.feed(b)
            if frame is None:
                continue
            self._ok += 1
            frames += 1
            self._handle_frame(frame)
        return frames

    def _on_datagram(self, data: bytes) -> None:
        self.feed_bytes(data)

    def _rebuild_fix_time(self, itow_ms: int) -> int:
        """以 iTOW 相對偏移重建 Unix ms 時間戳。"""
        now_s = time.time()
        now_ms = int(now_s * 1000)

        need_reanchor = (
            self._anchor_itow is None
            or self._anchor_server_time is None
            or itow_ms < self._anchor_itow - GPS_ITOW_WRAP_GUARD_MS
        )
        if not need_reanchor:
            assert self._anchor_itow is not None
            assert self._anchor_server_time is not None
            fix_ms = int(
                self._anchor_server_time * 1000
                + (itow_ms - self._anchor_itow)
            )
            if abs(fix_ms - now_ms) > GPS_REANCHOR_DRIFT_MS:
                need_reanchor = True

        if need_reanchor:
            self._anchor_itow = itow_ms
            self._anchor_server_time = now_s
            fix_ms = now_ms
        else:
            assert self._anchor_itow is not None
            assert self._anchor_server_time is not None
            fix_ms = int(
                self._anchor_server_time * 1000
                + (itow_ms - self._anchor_itow)
            )

        if self._last_gps_ts_ms is not None and fix_ms < self._last_gps_ts_ms:
            logger.debug(
                "gps out-of-order ts_ms=%s last=%s device=%s",
                fix_ms,
                self._last_gps_ts_ms,
                self._device_id,
            )
        self._last_gps_ts_ms = fix_ms
        return fix_ms

    def _accept_gps_jump(
        self, lat: float, lon: float, fix_time_s: float
    ) -> bool:
        """跳點剔除：速度 > GPS_MAX_SPEED_MPS 拒收；連續飛點後重置基準。"""
        prev = self._last_accepted_gps
        if prev is None:
            self._last_accepted_gps = (lat, lon, fix_time_s)
            self._rejected_streak = 0
            return True

        prev_lat, prev_lon, prev_t = prev
        dt = fix_time_s - prev_t
        if dt <= 0:
            # 同時間或倒退：當跳點拒收，但仍計 streak
            self._rejected_streak += 1
            if self._rejected_streak >= GPS_JUMP_RESET_STREAK:
                self._last_accepted_gps = (lat, lon, fix_time_s)
                self._rejected_streak = 0
                return True
            return False

        dist = _haversine_m(prev_lat, prev_lon, lat, lon)
        speed = dist / dt
        if speed > GPS_MAX_SPEED_MPS:
            self._rejected_streak += 1
            if self._rejected_streak >= GPS_JUMP_RESET_STREAK:
                self._last_accepted_gps = (lat, lon, fix_time_s)
                self._rejected_streak = 0
                return True
            return False

        self._last_accepted_gps = (lat, lon, fix_time_s)
        self._rejected_streak = 0
        return True

    def _handle_gps_payload(self, payload: bytes) -> TelemetrySample | None:
        raw = _parse_gps_raw(payload)
        if raw is None:
            return None

        if (
            raw.fix_type < GPS_MIN_FIX_TYPE
            or raw.num_sv < GPS_MIN_SV
            or raw.h_acc > GPS_MAX_H_ACC_MM
        ):
            return TelemetrySample(
                gps_fresh=0.0, gps_satellites=raw.num_sv
            )

        fix_ms = self._rebuild_fix_time(raw.itow)
        lat = raw.lat * 1e-7
        lon = raw.lon * 1e-7
        if not self._accept_gps_jump(lat, lon, fix_ms / 1000.0):
            return TelemetrySample(
                gps_fresh=0.0, gps_satellites=raw.num_sv
            )

        h_acc_m = raw.h_acc * 1e-3
        return TelemetrySample(
            gps_lat=lat,
            gps_lon=lon,
            gps_alt_m=raw.height * 1e-3,
            gps_speed_mps=raw.g_speed * 1e-3,
            gps_course_deg=raw.head_mot * 1e-5,
            gps_hdop=max(h_acc_m / 5.0, 0.5),
            gps_satellites=raw.num_sv,
            gps_fresh=1.0,
            ts_ms=fix_ms,
        )

    def _enqueue(self, sample: TelemetrySample, *, gps: bool) -> None:
        q = self._gps_q if gps else self._imu_q
        try:
            q.put_nowait(sample)
        except asyncio.QueueFull:
            if gps:
                self._gps_drops += 1
            else:
                self._imu_drops += 1

    def _handle_frame(self, frame: bytes) -> None:
        typ = frame[2]
        plen = frame[3]
        payload = frame[4 : 4 + plen]
        if typ == TYPE_IMU:
            now = time.monotonic()
            if now - self._last_imu_write < IMU_WRITE_MIN_SEC:
                parsed = _parse_imu(payload)
                if parsed:
                    self._update_cache(parsed[-1])
                return
            self._last_imu_write = now
            samples = _parse_imu(payload)
            if samples:
                self._enqueue(samples[-1], gps=False)
            return
        if typ == TYPE_MPU:
            now = time.monotonic()
            if now - self._last_mpu_write < MPU_WRITE_MIN_SEC:
                parsed = _parse_mpu(payload)
                if parsed:
                    self._update_cache(parsed[-1])
                return
            self._last_mpu_write = now
            samples = _parse_mpu(payload)
            if samples:
                self._enqueue(samples[-1], gps=False)
            return
        if typ == TYPE_GPS:
            s = self._handle_gps_payload(payload)
            if s is not None:
                self._enqueue(s, gps=True)
            return
        if typ == TYPE_FUSED:
            s = _parse_fused(payload)
            if s is not None:
                self._enqueue(s, gps=True)
            return
        if typ == TYPE_DBG:
            s = _parse_dbg(payload)
            if s is not None:
                self._enqueue(s, gps=False)

    def _update_cache(self, sample: TelemetrySample) -> None:
        sample_dict = sample.model_dump()
        by_device = getattr(self._app.state, "telemetry_by_device", None)
        if not isinstance(by_device, dict):
            by_device = {}
            self._app.state.telemetry_by_device = by_device
        prev = by_device.get(self._device_id)
        if isinstance(prev, dict):
            prev_sample = prev.get("sample") or {}
            if isinstance(prev_sample, dict):
                for k, v in prev_sample.items():
                    if sample_dict.get(k) is None and v is not None:
                        sample_dict[k] = v
        entry = {
            "device_id": self._device_id,
            "car_id": self._car_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "sample": sample_dict,
            "source": "udp",
        }
        self._app.state.telemetry_last = entry
        by_device[self._device_id] = entry

    async def _drop_warn_loop(self) -> None:
        while True:
            await asyncio.sleep(DROP_WARN_INTERVAL_SEC)
            if self._gps_drops or self._imu_drops:
                logger.warning(
                    "udp telemetry queue drops gps=%d imu=%d device=%s",
                    self._gps_drops,
                    self._imu_drops,
                    self._device_id,
                )
                self._gps_drops = 0
                self._imu_drops = 0

    async def _writer_loop(self) -> None:
        while True:
            batch: list[TelemetrySample] = []
            # 優先 drain GPS
            try:
                while True:
                    batch.append(self._gps_q.get_nowait())
            except asyncio.QueueEmpty:
                pass

            if not batch:
                # 阻塞等任一佇列有資料：先等 GPS，timeout 後查 IMU
                try:
                    sample = await asyncio.wait_for(self._gps_q.get(), timeout=0.05)
                    batch.append(sample)
                    try:
                        while True:
                            batch.append(self._gps_q.get_nowait())
                    except asyncio.QueueEmpty:
                        pass
                except asyncio.TimeoutError:
                    sample = await self._imu_q.get()
                    batch.append(sample)

            # 再從 IMU 補一批（總量上限 20）
            try:
                while len(batch) < 20:
                    batch.append(self._imu_q.get_nowait())
            except asyncio.QueueEmpty:
                pass

            await self._write_samples(batch)

    async def _write_samples(self, samples: list[TelemetrySample]) -> None:
        if not samples:
            return
        influx_reader = getattr(self._app.state, "influx_reader", None)
        if influx_reader is None or self._write_api is None:
            for s in samples:
                self._update_cache(s)
            return

        influx_cfg = influx_reader._config
        points: list[Point] = []
        for s in samples:
            points.append(_sample_to_point(self._device_id, self._car_id, s))
            dr = _sample_to_dr_point(self._device_id, self._car_id, s)
            if dr is not None:
                points.append(dr)
            self._update_cache(s)

        last = samples[-1]
        sample_dict = (getattr(self._app.state, "telemetry_by_device", {}) or {}).get(
            self._device_id, {}
        ).get("sample") or last.model_dump()

        try:
            await self._write_api.write(
                bucket=influx_cfg.bucket, org=influx_cfg.org, record=points
            )
        except Exception:
            logger.exception("udp telemetry influx write failed")

        try:
            await _notify_position(
                self._app,
                device_id=self._device_id,
                car_id=self._car_id,
                sample=sample_dict if isinstance(sample_dict, dict) else last.model_dump(),
            )
        except Exception:
            logger.exception("udp position broadcast failed")


async def _notify_position(
    app: Any, device_id: str, car_id: str | None, sample: dict[str, Any]
) -> None:
    web_config = getattr(app.state, "web_config", None)
    device_car_map = getattr(web_config, "telemetry_device_car_map", {}) or {}
    from .position_ws import notify_position_from_ingest, resolve_car_id

    resolved = resolve_car_id(device_id, car_id, device_car_map)
    received_at = datetime.now(timezone.utc).isoformat()

    class _R:
        pass

    req = _R()
    req.app = app
    await notify_position_from_ingest(
        req,  # type: ignore[arg-type]
        device_id=device_id,
        car_id=resolved,
        received_at=received_at,
        sample=sample,
    )


def load_udp_settings() -> tuple[str, int, str]:
    import os

    host = os.getenv("TELEMETRY_UDP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("TELEMETRY_UDP_PORT", "9500") or "9500")
    device_id = (
        os.getenv("TELEMETRY_UDP_DEVICE_ID", "esp32-kart-01").strip() or "esp32-kart-01"
    )
    return host, port, device_id

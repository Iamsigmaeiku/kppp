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
from typing import Any

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


def _parse_gps(payload: bytes) -> TelemetrySample | None:
    if len(payload) < 50:
        return None
    (
        _itow,
        lat,
        lon,
        height,
        vel_n,
        vel_e,
        vel_d,
        g_speed,
        head_mot,
        h_acc,
        v_acc,
        s_acc,
        num_sv,
        fix_type,
    ) = struct.unpack_from("<IiiiiiiiiIIIBB", payload, 0)
    if fix_type < 2:
        return TelemetrySample(gps_fresh=0.0, gps_satellites=num_sv)
    # rough HDOP proxy from hAcc (mm): hdop ≈ h_acc_m / 5
    h_acc_m = h_acc * 1e-3
    return TelemetrySample(
        gps_lat=lat * 1e-7,
        gps_lon=lon * 1e-7,
        gps_alt_m=height * 1e-3,
        gps_speed_mps=g_speed * 1e-3,
        gps_course_deg=head_mot * 1e-5,
        gps_hdop=max(h_acc_m / 5.0, 0.5),
        gps_satellites=num_sv,
        gps_fresh=1.0 if fix_type >= 3 else 0.5,
    )


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
        self._write_q: asyncio.Queue[TelemetrySample] = asyncio.Queue(maxsize=512)
        self._writer_task: asyncio.Task[None] | None = None
        self._last_imu_write = 0.0
        self._last_mpu_write = 0.0
        self._crc_err = 0
        self._ok = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        class Proto(asyncio.DatagramProtocol):
            def __init__(self, outer: UdpTelemetryServer) -> None:
                self._outer = outer

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                self._outer._on_datagram(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning("udp telemetry error: %s", exc)

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: Proto(self),
            local_addr=(self._host, self._port),
        )
        self._writer_task = asyncio.create_task(self._writer_loop(), name="udp-tel-writer")
        logger.info(
            "UDP telemetry listening on %s:%d device_id=%s",
            self._host,
            self._port,
            self._device_id,
        )

    async def stop(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self._transport:
            self._transport.close()

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

    def _handle_frame(self, frame: bytes) -> None:
        typ = frame[2]
        plen = frame[3]
        payload = frame[4 : 4 + plen]
        samples: list[TelemetrySample] = []
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
                samples = [samples[-1]]
        elif typ == TYPE_MPU:
            now = time.monotonic()
            if now - self._last_mpu_write < MPU_WRITE_MIN_SEC:
                parsed = _parse_mpu(payload)
                if parsed:
                    self._update_cache(parsed[-1])
                return
            self._last_mpu_write = now
            samples = _parse_mpu(payload)
            if samples:
                samples = [samples[-1]]
        elif typ == TYPE_GPS:
            s = _parse_gps(payload)
            if s:
                samples = [s]
        elif typ == TYPE_FUSED:
            s = _parse_fused(payload)
            if s:
                samples = [s]
        elif typ == TYPE_DBG:
            s = _parse_dbg(payload)
            if s:
                samples = [s]
        for s in samples:
            try:
                self._write_q.put_nowait(s)
            except asyncio.QueueFull:
                try:
                    self._write_q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._write_q.put_nowait(s)
                except asyncio.QueueFull:
                    pass

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

    async def _writer_loop(self) -> None:
        while True:
            sample = await self._write_q.get()
            batch = [sample]
            # small coalesce
            try:
                while len(batch) < 20:
                    batch.append(self._write_q.get_nowait())
            except asyncio.QueueEmpty:
                pass
            await self._write_samples(batch)

    async def _write_samples(self, samples: list[TelemetrySample]) -> None:
        influx_reader = getattr(self._app.state, "influx_reader", None)
        if influx_reader is None:
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

        client = InfluxDBClientAsync(
            url=influx_cfg.url, token=influx_cfg.token, org=influx_cfg.org
        )
        try:
            write_api = client.write_api()
            await write_api.write(
                bucket=influx_cfg.bucket, org=influx_cfg.org, record=points
            )
        except Exception:
            logger.exception("udp telemetry influx write failed")
        finally:
            await client.close()

        try:
            from .position_ws import notify_position_from_ingest

            # Build a fake request-like object? notify needs Request.
            # Inline broadcast instead via a thin helper.
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

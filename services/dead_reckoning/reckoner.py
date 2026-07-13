"""Dead-reckoning fusion: gyro yaw-rate integration between GPS fixes, hard-reset on each new fix.

heading（羅盤方位角，0=北，順時針為正，跟 GPS course-over-ground 同一慣例）：
  heading += gyro_z * dt
  x_east  += speed * sin(heading) * dt
  y_north += speed * cos(heading) * dt

x/y 是相對第一個 GPS fix 的本地平面座標（equirectangular 近似，賽道尺度足夠準）。

gz（陀螺儀 yaw rate）正負號跟實際順時針/逆時針的對應要靠實測校正：如果 DR 軌跡
轉彎方向跟真實方向相反，把 gyro_z 那行的正負號反過來即可（見 README 校正章節）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_M_PER_DEG_LAT = 111_320.0


@dataclass
class FusedState:
    lat: float
    lon: float
    heading_deg: float
    speed_mps: float
    source: str  # "gps" | "dr"


class DeadReckoner:
    def __init__(self, gps_course_min_speed_mps: float = 1.0) -> None:
        self._ref_lat: float | None = None
        self._ref_lon: float | None = None
        self._x_m = 0.0
        self._y_m = 0.0
        self._heading_rad = 0.0
        self._speed_mps = 0.0
        self._last_gps_lat: float | None = None
        self._last_gps_lon: float | None = None
        self._gps_course_min_speed_mps = gps_course_min_speed_mps

    def _meters_per_deg_lon(self) -> float:
        assert self._ref_lat is not None
        return EARTH_M_PER_DEG_LAT * math.cos(math.radians(self._ref_lat))

    def _latlon_to_local(self, lat: float, lon: float) -> tuple[float, float]:
        assert self._ref_lat is not None and self._ref_lon is not None
        x = (lon - self._ref_lon) * self._meters_per_deg_lon()
        y = (lat - self._ref_lat) * EARTH_M_PER_DEG_LAT
        return x, y

    def _local_to_latlon(self) -> tuple[float, float]:
        assert self._ref_lat is not None and self._ref_lon is not None
        lat = self._ref_lat + self._y_m / EARTH_M_PER_DEG_LAT
        lon = self._ref_lon + self._x_m / self._meters_per_deg_lon()
        return lat, lon

    def update(
        self,
        dt: float,
        gz_dps: float,
        gps_lat: float | None,
        gps_lon: float | None,
        gps_speed_mps: float | None,
        gps_course_deg: float | None,
    ) -> FusedState | None:
        is_fresh_fix = (
            gps_lat is not None
            and gps_lon is not None
            and (gps_lat != self._last_gps_lat or gps_lon != self._last_gps_lon)
        )

        if is_fresh_fix:
            self._last_gps_lat = gps_lat
            self._last_gps_lon = gps_lon

        if self._ref_lat is None:
            if not is_fresh_fix:
                return None  # 還沒拿到第一個 GPS fix，無法建立本地座標系
            self._ref_lat, self._ref_lon = gps_lat, gps_lon
            self._x_m = self._y_m = 0.0

        # 陀螺儀 yaw rate 積分（dps -> rad/s）
        self._heading_rad += math.radians(gz_dps) * dt
        if gps_speed_mps is not None:
            self._speed_mps = gps_speed_mps

        self._x_m += self._speed_mps * math.sin(self._heading_rad) * dt
        self._y_m += self._speed_mps * math.cos(self._heading_rad) * dt

        source = "dr"
        if is_fresh_fix:
            # 新 GPS fix：硬校正回真實位置，蓋掉累積漂移
            self._x_m, self._y_m = self._latlon_to_local(gps_lat, gps_lon)
            if (
                gps_course_deg is not None
                and self._speed_mps >= self._gps_course_min_speed_mps
            ):
                self._heading_rad = math.radians(gps_course_deg)
            source = "gps"

        lat, lon = self._local_to_latlon()
        return FusedState(
            lat=lat,
            lon=lon,
            heading_deg=math.degrees(self._heading_rad) % 360.0,
            speed_mps=self._speed_mps,
            source=source,
        )

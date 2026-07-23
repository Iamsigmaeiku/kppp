"""用 GPS 軌跡穿越虛擬起跑線切圈（純函式、無 IO）。

不依賴 decoder 通過時間戳——那套切法會因 GPS 取樣偏移讓圈首尾歪。
幾何一律在本地公尺座標運算（見 track_coords.latlng_to_local_m），
穿越時刻用線段相交參數做時間內插，10Hz GPS 也能拿到 ~ms 級圈速。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.decoder_ingest.influx_reader import TrackPoint
from services.webapp.track_coords import latlng_to_local_m, local_m_to_latlng

# 本場地最快約 47s；25s 內重複穿越視為 GPS 在 gate 附近抖動。
MIN_LAP_TIME_SEC = 25.0
# 超過視為進 pit / 失鎖中斷，該段標 is_complete=False。
MAX_LAP_TIME_SEC = 300.0
# 相鄰兩點時間差超過此值視為 GPS 斷訊，跨越該缺口的相交不算穿越。
MAX_GAP_SEC = 5.0
# 線段相交端點容差（公尺空間的參數容差，略放寬避免掠過端點漏切）。
_EPS = 1e-9


@dataclass(frozen=True)
class GateCrossing:
    crossed_at: datetime  # 內插後的精確穿越時間
    point_index: int  # 穿越發生在 points[i] → points[i+1]，記 i
    s: float  # P1→P2 上的內插參數 ∈ [0, 1]
    x_m: float
    y_m: float


@dataclass(frozen=True)
class GpsLap:
    lap_number: int  # 1-based，依時間先後編（含未閉合段）
    lap_time: float  # 秒；complete 為 crossing-to-crossing
    started_at: datetime
    ended_at: datetime
    points: list[TrackPoint]  # 含首尾各一個內插到起跑線上的合成點（閉合圈）
    is_complete: bool  # out-lap / in-lap / 超時中斷為 False


def _cross2(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _segment_intersection(
    p1: tuple[float, float],
    p2: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float | None:
    """回傳 P1→P2 上的參數 s ∈ [0,1]，若與 gate A→B 相交；否則 None。"""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s_vec = (b[0] - a[0], b[1] - a[1])
    denom = _cross2(r[0], r[1], s_vec[0], s_vec[1])
    if abs(denom) < _EPS:
        return None  # 平行或共線：不把共線滑過當穿越（避免直線上抖動）
    qp = (a[0] - p1[0], a[1] - p1[1])
    t = _cross2(qp[0], qp[1], s_vec[0], s_vec[1]) / denom
    u = _cross2(qp[0], qp[1], r[0], r[1]) / denom
    if -_EPS <= t <= 1.0 + _EPS and -_EPS <= u <= 1.0 + _EPS:
        return max(0.0, min(1.0, t))
    return None


def _forward_normal(bearing_deg: float) -> tuple[float, float]:
    """方位角 → 單位法向量（本地 m：0°=+y 北、90°=+x 東）。"""
    rad = math.radians(bearing_deg)
    return (math.sin(rad), math.cos(rad))


def _lerp_speed(s1: float | None, s2: float | None, s: float) -> float | None:
    if s1 is None and s2 is None:
        return None
    a = 0.0 if s1 is None else s1
    b = 0.0 if s2 is None else s2
    if s1 is None:
        return s2
    if s2 is None:
        return s1
    return a + (b - a) * s


def _interp_track_point(
    p1: TrackPoint,
    p2: TrackPoint,
    s: float,
    x_m: float,
    y_m: float,
    crossed_at: datetime,
) -> TrackPoint:
    lat, lon = local_m_to_latlng(x_m, y_m)
    return TrackPoint(
        lat=lat,
        lon=lon,
        recorded_at=crossed_at,
        speed_mps=_lerp_speed(p1.speed_mps, p2.speed_mps, s),
    )


def _find_crossings(
    points: list[TrackPoint],
    locals_m: list[tuple[float, float]],
    gate_a: tuple[float, float],
    gate_b: tuple[float, float],
    forward_n: tuple[float, float],
) -> list[GateCrossing]:
    raw: list[GateCrossing] = []
    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i + 1]
        dt = (p2.recorded_at - p1.recorded_at).total_seconds()
        if dt < 0:
            continue
        # GPS 斷訊缺口：跨越不算穿越
        if dt > MAX_GAP_SEC:
            continue
        m1, m2 = locals_m[i], locals_m[i + 1]
        move = (m2[0] - m1[0], m2[1] - m1[1])
        # 方向過濾：只接受往前進方向穿過 gate
        if move[0] * forward_n[0] + move[1] * forward_n[1] <= 0:
            continue
        s = _segment_intersection(m1, m2, gate_a, gate_b)
        if s is None:
            continue
        crossed_at = p1.recorded_at + timedelta(seconds=dt * s)
        x_m = m1[0] + (m2[0] - m1[0]) * s
        y_m = m1[1] + (m2[1] - m1[1]) * s
        raw.append(
            GateCrossing(
                crossed_at=crossed_at,
                point_index=i,
                s=s,
                x_m=x_m,
                y_m=y_m,
            )
        )

    # 防抖：與上一接受穿越間隔 < MIN_LAP_TIME 的丟掉
    accepted: list[GateCrossing] = []
    for c in raw:
        if accepted:
            gap = (c.crossed_at - accepted[-1].crossed_at).total_seconds()
            if gap < MIN_LAP_TIME_SEC:
                continue
        accepted.append(c)
    return accepted


def split_laps_by_gate(
    points: list[TrackPoint],
    gate_a_m: tuple[float, float],
    gate_b_m: tuple[float, float],
    forward_bearing_deg: float,
) -> list[GpsLap]:
    """依虛擬起跑線穿越切圈。

    - 空輸入 → []
    - 從未穿越 → 單筆 is_complete=False（整段軌跡）
    - 有穿越：out-lap / complete / in-lap；lap_number 1-based 連續
    """
    if not points:
        return []

    locals_m = [latlng_to_local_m(p.lat, p.lon) for p in points]
    forward_n = _forward_normal(forward_bearing_deg)
    crossings = _find_crossings(points, locals_m, gate_a_m, gate_b_m, forward_n)

    if not crossings:
        return [
            GpsLap(
                lap_number=1,
                lap_time=(points[-1].recorded_at - points[0].recorded_at).total_seconds(),
                started_at=points[0].recorded_at,
                ended_at=points[-1].recorded_at,
                points=list(points),
                is_complete=False,
            )
        ]

    def synth_at(c: GateCrossing) -> TrackPoint:
        i = c.point_index
        return _interp_track_point(
            points[i], points[i + 1], c.s, c.x_m, c.y_m, c.crossed_at
        )

    laps: list[GpsLap] = []
    lap_number = 1

    # out-lap：軌跡起點 → 第一個穿越（合成點收尾，圈未閉合）
    first = crossings[0]
    first_synth = synth_at(first)
    out_pts = list(points[: first.point_index + 1]) + [first_synth]
    laps.append(
        GpsLap(
            lap_number=lap_number,
            lap_time=(first.crossed_at - points[0].recorded_at).total_seconds(),
            started_at=points[0].recorded_at,
            ended_at=first.crossed_at,
            points=out_pts,
            is_complete=False,
        )
    )
    lap_number += 1

    # crossing-to-crossing：首尾各插合成點，畫出來的圈才閉合在起跑線上
    for i in range(len(crossings) - 1):
        c0, c1 = crossings[i], crossings[i + 1]
        dt = (c1.crossed_at - c0.crossed_at).total_seconds()
        # 低於 MIN 已被防抖丟掉；這裡只把超時段標未閉合
        complete = dt <= MAX_LAP_TIME_SEC
        s0, s1 = synth_at(c0), synth_at(c1)
        mid = points[c0.point_index + 1 : c1.point_index + 1]
        seg = [s0, *mid, s1]
        laps.append(
            GpsLap(
                lap_number=lap_number,
                lap_time=dt,
                started_at=c0.crossed_at,
                ended_at=c1.crossed_at,
                points=seg,
                is_complete=complete,
            )
        )
        lap_number += 1

    # in-lap：最後穿越 → 軌跡結尾
    last = crossings[-1]
    last_synth = synth_at(last)
    in_pts = [last_synth, *points[last.point_index + 1 :]]
    laps.append(
        GpsLap(
            lap_number=lap_number,
            lap_time=(in_pts[-1].recorded_at - last.crossed_at).total_seconds(),
            started_at=last.crossed_at,
            ended_at=in_pts[-1].recorded_at,
            points=in_pts,
            is_complete=False,
        )
    )

    return laps

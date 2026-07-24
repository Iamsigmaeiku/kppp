"""用 GPS 軌跡穿越虛擬起跑線切圈（純函式、無 IO）。

不依賴 decoder 通過時間戳——那套切法會因 GPS 取樣偏移讓圈首尾歪。
幾何一律在本地公尺座標運算（見 track_coords.latlng_to_local_m），
穿越用無限線 signed-distance 零交越 + 橫向半寬，時間內插到 ~ms。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.decoder_ingest.influx_reader import TrackPoint
from services.webapp.track_coords import (
    GATE_HALF_WIDTH_M,
    latlng_to_local_m,
    local_m_to_latlng,
)

# 本場地最快約 47s；25s 內重複穿越視為 GPS 在 gate 附近抖動。
MIN_LAP_TIME_SEC = 25.0
# 超過視為進 pit / 失鎖中斷，該段標 is_complete=False。
MAX_LAP_TIME_SEC = 300.0
# 相鄰兩點時間差超過此值視為 GPS 斷訊，跨越該缺口的相交不算穿越。
MAX_GAP_SEC = 15.0
# signed-distance 需至少離線此距離再跨到另一側，避免貼線抖動誤切。
# 真實過線通常從數公尺外接近；過線弦本身的 |d1| 可能很小（取樣已貼線）。
_CROSS_MIN_ABS_M = 0.3
# 過線前 lookback：路徑上需曾到達 across ≤ -ARM，才接受穿越（抗貼線抖動）。
_ARM_DEPTH_M = 4.0
_ARM_LOOKBACK_POINTS = 40  # ~4s @10Hz；涵蓋接近直線
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
    is_complete: bool  # out-lap / in-lap / 超時中斷 / 圈內大缺口為 False


def _forward_normal(bearing_deg: float) -> tuple[float, float]:
    """方位角 → 單位法向量（本地 m：0°=+y 北、90°=+x 東）。"""
    rad = math.radians(bearing_deg)
    return (math.sin(rad), math.cos(rad))


def _gate_frame(
    gate_a: tuple[float, float],
    gate_b: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], float]:
    """回傳 (center, along_unit, across_unit, half_len)。

    along = A→B 單位向量；across = 逆時針法向（左手系 2D）。
    half_len 至少 GATE_HALF_WIDTH_M，即使 A/B 畫線較短也能吃橫漂。
    """
    ax, ay = gate_a
    bx, by = gate_b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < _EPS:
        raise ValueError("gate A/B coincident")
    along = (dx / length, dy / length)
    across = (-along[1], along[0])  # 逆時針 90°
    center = ((ax + bx) * 0.5, (ay + by) * 0.5)
    half_len = max(length * 0.5, GATE_HALF_WIDTH_M)
    return center, along, across, half_len


def _signed_across(
    p: tuple[float, float],
    center: tuple[float, float],
    across: tuple[float, float],
) -> float:
    return (p[0] - center[0]) * across[0] + (p[1] - center[1]) * across[1]


def _along_offset(
    p: tuple[float, float],
    center: tuple[float, float],
    along: tuple[float, float],
) -> float:
    return (p[0] - center[0]) * along[0] + (p[1] - center[1]) * along[1]


def _zero_cross_s(d1: float, d2: float) -> float | None:
    """signed-distance 異號（或一端為 0）時回傳 P1→P2 參數 s；否則 None。"""
    if abs(d1) < _EPS and abs(d2) < _EPS:
        return None  # 整段貼線：不當穿越
    if d1 * d2 > 0:
        return None
    denom = d1 - d2
    if abs(denom) < _EPS:
        return None
    s = d1 / denom
    if s < -_EPS or s > 1.0 + _EPS:
        return None
    return max(0.0, min(1.0, s))


def _lerp_speed(s1: float | None, s2: float | None, s: float) -> float | None:
    if s1 is None and s2 is None:
        return None
    if s1 is None:
        return s2
    if s2 is None:
        return s1
    return s1 + (s2 - s1) * s


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


def _segment_has_large_gap(points: list[TrackPoint], i0: int, i1: int) -> bool:
    """points[i0]…points[i1]（含）之間是否有相鄰 dt > MAX_GAP_SEC。"""
    lo = max(0, i0)
    hi = min(len(points) - 1, i1)
    for i in range(lo, hi):
        dt = (points[i + 1].recorded_at - points[i].recorded_at).total_seconds()
        if dt > MAX_GAP_SEC:
            return True
    return False


def _find_crossings(
    points: list[TrackPoint],
    locals_m: list[tuple[float, float]],
    gate_a: tuple[float, float],
    gate_b: tuple[float, float],
    forward_n: tuple[float, float],
) -> list[GateCrossing]:
    center, along, across, half_len = _gate_frame(gate_a, gate_b)
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

        d1 = _signed_across(m1, center, across)
        d2 = _signed_across(m2, center, across)
        # 至少一側離線夠遠，避免貼線抖動
        if max(abs(d1), abs(d2)) < _CROSS_MIN_ABS_M:
            continue
        s = _zero_cross_s(d1, d2)
        if s is None:
            continue

        x_m = m1[0] + (m2[0] - m1[0]) * s
        y_m = m1[1] + (m2[1] - m1[1]) * s
        # 交點須落在 gate 中心 ± half_len（半寬至少 GATE_HALF_WIDTH_M）
        if abs(_along_offset((x_m, y_m), center, along)) > half_len + _EPS:
            continue

        # lookback：近期路徑須曾到南側夠深，過濾貼線抖動誤切
        armed = False
        lo = max(0, i - _ARM_LOOKBACK_POINTS)
        for k in range(lo, i + 1):
            if _signed_across(locals_m[k], center, across) <= -_ARM_DEPTH_M:
                if abs(_along_offset(locals_m[k], center, along)) <= half_len + 5.0:
                    armed = True
                    break
        if not armed:
            continue

        crossed_at = p1.recorded_at + timedelta(seconds=dt * s)
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


def find_gate_crossings(
    points: list[TrackPoint],
    gate_a_m: tuple[float, float],
    gate_b_m: tuple[float, float],
    forward_bearing_deg: float,
) -> list[GateCrossing]:
    """公開 API：回傳通過防抖後的 gate 穿越（供時基校準用）。"""
    if len(points) < 2:
        return []
    locals_m = [latlng_to_local_m(p.lat, p.lon) for p in points]
    forward_n = _forward_normal(forward_bearing_deg)
    return _find_crossings(points, locals_m, gate_a_m, gate_b_m, forward_n)


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
    - 圈內相鄰點 dt > MAX_GAP_SEC → 該圈 is_complete=False
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
        # 低於 MIN 已被防抖丟掉；超時或圈內相鄰點大缺口 → 未閉合
        mid_lo = c0.point_index
        mid_hi = c1.point_index + 1
        has_gap = _segment_has_large_gap(points, mid_lo, mid_hi)
        complete = dt <= MAX_LAP_TIME_SEC and not has_gap
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

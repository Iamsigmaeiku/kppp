"""gps_lap_splitter：虛擬起跑線切圈純函式測試。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.gps_lap_splitter import (
    MIN_LAP_TIME_SEC,
    split_laps_by_gate,
)
from services.decoder_ingest.influx_reader import TrackPoint
from services.webapp.track_coords import local_m_to_latlng

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

# 水平起跑線：x=0..20、y=0；前進方向 +y（北，bearing=0）
GATE_A = (0.0, 0.0)
GATE_B = (20.0, 0.0)
BEARING_N = 0.0


def _pt(x_m: float, y_m: float, t_sec: float, speed: float | None = 10.0) -> TrackPoint:
    lat, lon = local_m_to_latlng(x_m, y_m)
    return TrackPoint(
        lat=lat,
        lon=lon,
        recorded_at=T0 + timedelta(seconds=t_sec),
        speed_mps=speed,
    )


def _north_cross_pair(t_cross: float, half_dt: float = 0.05) -> list[TrackPoint]:
    """在 t_cross 精確穿越 gate（南北對稱，s=0.5）。"""
    return [
        _pt(10.0, -1.0, t_cross - half_dt),
        _pt(10.0, 1.0, t_cross + half_dt),
    ]


def _loop_body(t0: float, duration: float = 49.0, n: int = 49) -> list[TrackPoint]:
    """穿越後繞一大圈回到 gate 南側，不正向穿越起跑線。"""
    # 路徑：(10,1) → (40,1) → (40,-20) → (-20,-20) → (10,-1)
    waypoints = [
        (10.0, 1.0),
        (40.0, 1.0),
        (40.0, -20.0),
        (-20.0, -20.0),
        (10.0, -1.0),
    ]
    pts: list[TrackPoint] = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 0.0
        # 沿 polyline 均勻取樣
        seg_f = frac * (len(waypoints) - 1)
        si = min(int(seg_f), len(waypoints) - 2)
        local = seg_f - si
        x1, y1 = waypoints[si]
        x2, y2 = waypoints[si + 1]
        pts.append(_pt(x1 + (x2 - x1) * local, y1 + (y2 - y1) * local, t0 + duration * frac))
    return pts


def _three_lap_track() -> list[TrackPoint]:
    """out + 3 complete（各 50s）+ in。穿越時刻 10, 60, 110, 160。"""
    crossings = [10.0, 60.0, 110.0, 160.0]
    pts: list[TrackPoint] = []
    # out-lap 接近
    pts.append(_pt(10.0, -5.0, 0.0))
    pts.append(_pt(10.0, -2.0, 5.0))
    for i, tc in enumerate(crossings):
        pts.extend(_north_cross_pair(tc))
        if i < len(crossings) - 1:
            # 下一穿越前的迴路本體（避開交叉點附近重複）
            body = _loop_body(tc + 0.1, duration=crossings[i + 1] - tc - 0.2)
            pts.extend(body[1:])  # 跳過起點避免與 cross 後點重疊
    # in-lap
    pts.append(_pt(10.0, 3.0, 165.0))
    pts.append(_pt(15.0, 8.0, 170.0))
    pts.sort(key=lambda p: p.recorded_at)
    return pts


def test_empty_input():
    assert split_laps_by_gate([], GATE_A, GATE_B, BEARING_N) == []


def test_never_crossed_returns_single_incomplete():
    pts = [_pt(0.0, -10.0, 0.0), _pt(5.0, -10.0, 1.0), _pt(10.0, -10.0, 2.0)]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False
    assert laps[0].lap_number == 1
    assert laps[0].lap_time == pytest.approx(2.0)


def test_three_laps_plus_out_in():
    pts = _three_lap_track()
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    complete = [lap for lap in laps if lap.is_complete]
    incomplete = [lap for lap in laps if not lap.is_complete]
    assert len(complete) == 3
    assert len(incomplete) == 2  # out + in
    assert laps[0].is_complete is False
    assert laps[-1].is_complete is False
    for lap in complete:
        assert lap.lap_time == pytest.approx(50.0, abs=0.1)
        # 閉合：首尾都在起跑線附近（y≈0）
        assert abs(lap.points[0].lat - local_m_to_latlng(10.0, 0.0)[0]) < 1e-6


def test_time_interpolation_sub_sample():
    """已知穿越時刻：P1/P2 不在線上，lap_time 誤差 < 1ms。"""
    # 穿越 1：t=10.000；穿越 2：t=60.123 → lap = 50.123
    pts = [
        _pt(10.0, -5.0, 0.0),
        *_north_cross_pair(10.0, half_dt=0.1),
        *_loop_body(10.2, duration=49.7, n=40)[1:],
        # 第二穿越：刻意不對稱內插
        _pt(10.0, -2.0, 60.0),
        _pt(10.0, 2.0, 60.246),  # s=0.5 → 60.123
        _pt(10.0, 5.0, 65.0),
    ]
    pts.sort(key=lambda p: p.recorded_at)
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    complete = [lap for lap in laps if lap.is_complete]
    assert len(complete) == 1
    assert complete[0].lap_time == pytest.approx(50.123, abs=1e-3)


def test_reverse_crossing_ignored():
    """由北往南穿過 gate（反向）不計。"""
    pts = [
        _pt(10.0, 5.0, 0.0),
        _pt(10.0, 1.0, 1.0),
        _pt(10.0, -1.0, 2.0),  # 反向穿越
        _pt(10.0, -5.0, 3.0),
    ]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False


def test_min_lap_time_debounce():
    """gate 附近來回抖動：正向短間隔穿越被丟棄。"""
    pts = [
        _pt(10.0, -5.0, 0.0),
        *_north_cross_pair(10.0),
        # 立刻又從南側再穿一次（模擬抖動）：間隔 << MIN
        _pt(10.0, -1.0, 10.5),
        _pt(10.0, 1.0, 10.6),
        # 合法第二穿越
        *_loop_body(11.0, duration=40.0, n=30)[1:],
        *_north_cross_pair(10.0 + MIN_LAP_TIME_SEC + 5.0),
        _pt(10.0, 5.0, 50.0),
    ]
    pts.sort(key=lambda p: p.recorded_at)
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    complete = [lap for lap in laps if lap.is_complete]
    assert len(complete) == 1
    assert complete[0].lap_time == pytest.approx(MIN_LAP_TIME_SEC + 5.0, abs=0.15)


def test_gps_gap_skips_crossing():
    """相鄰點時間差 > 5s，即使空間上跨過 gate 也不算穿越。"""
    pts = [
        _pt(10.0, -5.0, 0.0),
        _pt(10.0, -1.0, 1.0),
        # 斷訊 6 秒後出現在北側——幾何上跨過，但缺口太大
        _pt(10.0, 1.0, 7.5),
        _pt(10.0, 5.0, 8.5),
    ]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False

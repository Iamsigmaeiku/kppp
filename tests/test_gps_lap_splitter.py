"""gps_lap_splitter：虛擬起跑線切圈純函式測試。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.gps_lap_splitter import (
    MAX_GAP_SEC,
    MIN_LAP_TIME_SEC,
    split_laps_by_gate,
)
from services.decoder_ingest.influx_reader import TrackPoint
from services.webapp.track_coords import local_m_to_latlng

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

# 水平起跑線：x=0..20、y=0；前進方向 +y（北，bearing=0）
# half_len = max(10, GATE_HALF_WIDTH_M=22) → 中心±22m
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


def _north_cross_pair(
    t_cross: float, half_dt: float = 0.05, x_m: float = 10.0
) -> list[TrackPoint]:
    """在 t_cross 精確穿越 gate（南北對稱，s=0.5）；含南側 approach 供 arm。"""
    return [
        _pt(x_m, -5.0, t_cross - half_dt - 0.2),
        _pt(x_m, -1.0, t_cross - half_dt),
        _pt(x_m, 1.0, t_cross + half_dt),
    ]


def _loop_body(t0: float, duration: float = 49.0, n: int = 49) -> list[TrackPoint]:
    """穿越後繞一大圈回到 gate 南側，不正向穿越起跑線。"""
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
    pts.append(_pt(10.0, -5.0, 0.0))
    pts.append(_pt(10.0, -2.0, 5.0))
    for i, tc in enumerate(crossings):
        pts.extend(_north_cross_pair(tc))
        if i < len(crossings) - 1:
            body = _loop_body(tc + 0.1, duration=crossings[i + 1] - tc - 0.2)
            pts.extend(body[1:])
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
        assert abs(lap.points[0].lat - local_m_to_latlng(10.0, 0.0)[0]) < 1e-6


def test_time_interpolation_sub_sample():
    """已知穿越時刻：P1/P2 不在線上，lap_time 誤差 < 1ms。"""
    pts = [
        _pt(10.0, -5.0, 0.0),
        *_north_cross_pair(10.0, half_dt=0.1),
        *_loop_body(10.2, duration=49.7, n=40)[1:],
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
        _pt(10.0, -1.0, 2.0),
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
        _pt(10.0, -1.0, 10.5),
        _pt(10.0, 1.0, 10.6),
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
    """相鄰點時間差 > MAX_GAP_SEC，即使空間上跨過 gate 也不算穿越。"""
    gap = MAX_GAP_SEC + 1.0
    pts = [
        _pt(10.0, -5.0, 0.0),
        _pt(10.0, -1.0, 1.0),
        _pt(10.0, 1.0, 1.0 + gap),
        _pt(10.0, 5.0, 2.0 + gap),
    ]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False


def test_moderate_gap_still_crosses():
    """dt=8s（舊門檻 5s 會漏、新門檻 15s 仍切到）。"""
    pts = [
        _pt(10.0, -5.0, 0.0),
        _pt(10.0, -5.0, 0.5),  # arm
        _pt(10.0, -1.0, 1.0),
        _pt(10.0, 1.0, 9.0),  # dt=8s across gate
        _pt(10.0, 5.0, 10.0),
        *_loop_body(10.5, duration=49.0, n=40)[1:],
        *_north_cross_pair(60.0),
        _pt(10.0, 5.0, 65.0),
    ]
    pts.sort(key=lambda p: p.recorded_at)
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    complete = [lap for lap in laps if lap.is_complete]
    assert len(complete) == 1
    assert complete[0].lap_time == pytest.approx(55.0, abs=0.3)


def test_lateral_offset_within_half_width():
    """x 超出有限 gate 線段 (0..20) 但仍在中心±22m → 仍切到。"""
    # 中心 x=10；x=28 超出舊線段末端 20，但 |28-10|=18 < 22
    pts = [
        _pt(28.0, -5.0, 0.0),
        *_north_cross_pair(10.0, x_m=28.0),
        *_loop_body(10.5, duration=49.0, n=40)[1:],
        *_north_cross_pair(60.0, x_m=28.0),
        _pt(28.0, 5.0, 65.0),
    ]
    pts.sort(key=lambda p: p.recorded_at)
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    complete = [lap for lap in laps if lap.is_complete]
    assert len(complete) == 1
    assert complete[0].lap_time == pytest.approx(50.0, abs=0.15)


def test_shallow_jitter_ignored_without_arm():
    """貼線南北抖動（從未到 across≤-4）不切圈。"""
    pts = [
        _pt(10.0, -0.5, 0.0),
        _pt(10.0, 0.5, 0.1),
        _pt(10.0, -0.5, 0.2),
        _pt(10.0, 0.5, 0.3),
        _pt(10.0, 2.0, 1.0),
    ]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False


def test_lateral_offset_beyond_half_width_missed():
    """橫向超出半寬 → 不算穿越。"""
    # |45-10|=35 > 22
    pts = [
        _pt(45.0, -5.0, 0.0),
        _pt(45.0, -1.0, 1.0),
        _pt(45.0, 1.0, 2.0),
        _pt(45.0, 5.0, 3.0),
    ]
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    assert len(laps) == 1
    assert laps[0].is_complete is False


def test_gap_inside_lap_marks_incomplete():
    """兩次穿越之間中段有 >MAX_GAP 缺口 → 該圈 is_complete=False。"""
    pts = [
        _pt(10.0, -5.0, 0.0),
        *_north_cross_pair(10.0),
        _pt(40.0, 1.0, 15.0),
        # 中段斷訊 20s
        _pt(40.0, -20.0, 15.0 + MAX_GAP_SEC + 5.0),
        _pt(10.0, -1.0, 50.0),
        *_north_cross_pair(60.0),
        _pt(10.0, 5.0, 65.0),
    ]
    pts.sort(key=lambda p: p.recorded_at)
    laps = split_laps_by_gate(pts, GATE_A, GATE_B, BEARING_N)
    between = [lap for lap in laps if lap.lap_number == 2]
    assert len(between) == 1
    assert between[0].is_complete is False
    assert between[0].lap_time == pytest.approx(50.0, abs=0.15)

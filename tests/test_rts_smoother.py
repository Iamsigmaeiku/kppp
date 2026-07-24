"""RTS smoother 純演算法測試。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from services.postprocess.rts_smoother import (
    SmoothInput,
    hall_scale_m_per_rev,
    smooth_track,
    theil_sen_slope,
)

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _circle_truth(n: int = 200, radius: float = 40.0, period: float = 40.0):
    """合成圓形賽道真值（等角速度）。"""
    ts = []
    xs = []
    ys = []
    for i in range(n):
        t = i * (period / n)
        ang = 2 * math.pi * t / period
        ts.append(T0 + timedelta(seconds=t))
        xs.append(radius * math.cos(ang))
        ys.append(radius * math.sin(ang))
    return ts, np.array(xs), np.array(ys)


def test_theil_sen_slope_through_origin():
    xs = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    ys = 0.85 * xs + np.array([0.01, -0.02, 0.0, 0.01, -0.01])
    m = theil_sen_slope(xs, ys)
    assert m == pytest.approx(0.85, abs=0.02)


def test_hall_scale_m_per_rev():
    rng = np.random.default_rng(0)
    true_m = 0.85
    v = rng.uniform(5.0, 20.0, size=80)
    hall = v / true_m + rng.normal(0, 0.05, size=80)
    m, r2 = hall_scale_m_per_rev(hall, v)
    assert m is not None
    assert abs(m - true_m) / true_m < 0.02
    assert r2 >= 0.9


def test_smoothed_rmse_half_of_raw():
    rng = np.random.default_rng(1)
    ts, x_true, y_true = _circle_truth(n=250, radius=45.0, period=50.0)
    noise = 2.5
    samples = []
    for i, t in enumerate(ts):
        samples.append(
            SmoothInput(
                t=t,
                x_m=float(x_true[i] + rng.normal(0, noise)),
                y_m=float(y_true[i] + rng.normal(0, noise)),
                hdop=1.0,
                speed_mps=None,
            )
        )
    out = smooth_track(samples, use_speed=False)
    raw_err = np.sqrt(
        (np.array([s.x_m for s in samples]) - x_true) ** 2
        + (np.array([s.y_m for s in samples]) - y_true) ** 2
    )
    sm_err = np.sqrt(
        (np.array([o.x_m for o in out]) - x_true) ** 2
        + (np.array([o.y_m for o in out]) - y_true) ** 2
    )
    assert float(np.mean(sm_err)) <= float(np.mean(raw_err)) * 0.5


def test_gap_bridge_and_flag():
    rng = np.random.default_rng(2)
    ts, x_true, y_true = _circle_truth(n=200, radius=40.0, period=40.0)
    # 挖掉中間 ~11s（> GAP_MARK_SEC=10）
    dt = (ts[1] - ts[0]).total_seconds()
    gap_n = int(11.0 / dt)
    gap_start = 60
    samples = []
    for i, t in enumerate(ts):
        if gap_start <= i < gap_start + gap_n:
            samples.append(
                SmoothInput(
                    t=t,
                    x_m=float(x_true[i]),
                    y_m=float(y_true[i]),
                    hdop=1.0,
                    has_position=False,
                )
            )
        else:
            samples.append(
                SmoothInput(
                    t=t,
                    x_m=float(x_true[i] + rng.normal(0, 1.5)),
                    y_m=float(y_true[i] + rng.normal(0, 1.5)),
                    hdop=1.0,
                )
            )
    out = smooth_track(samples, use_speed=False)
    gap_slice = slice(gap_start, gap_start + gap_n)
    assert any(o.gap for o in out[gap_slice])
    err = np.sqrt(
        (np.array([o.x_m for o in out[gap_slice]]) - x_true[gap_slice]) ** 2
        + (np.array([o.y_m for o in out[gap_slice]]) - y_true[gap_slice]) ** 2
    )
    assert float(np.mean(err)) < 5.0


def test_outlier_gating():
    rng = np.random.default_rng(3)
    ts, x_true, y_true = _circle_truth(n=180, radius=35.0, period=45.0)
    samples = []
    jump_idx = {20, 50, 80, 110, 140}
    for i, t in enumerate(ts):
        x = float(x_true[i] + rng.normal(0, 1.0))
        y = float(y_true[i] + rng.normal(0, 1.0))
        if i in jump_idx:
            x += 30.0
            y += 30.0
        samples.append(SmoothInput(t=t, x_m=x, y_m=y, hdop=1.0))
    out = smooth_track(samples, use_speed=False)
    # 跳點附近平滑結果應接近真值，不被拉走
    for i in jump_idx:
        d = math.hypot(out[i].x_m - x_true[i], out[i].y_m - y_true[i])
        assert d < 8.0


def test_variable_dt_stable():
    rng = np.random.default_rng(4)
    # 混合 10–50Hz 間隔
    t = 0.0
    ts = []
    xs = []
    ys = []
    while t < 30.0:
        ts.append(T0 + timedelta(seconds=t))
        xs.append(t * 2.0)  # 直線 2 m/s
        ys.append(0.0)
        t += float(rng.uniform(0.02, 0.1))
    samples = [
        SmoothInput(
            t=ts[i],
            x_m=float(xs[i] + rng.normal(0, 1.0)),
            y_m=float(ys[i] + rng.normal(0, 1.0)),
            hdop=1.2,
            speed_mps=2.0,
        )
        for i in range(len(ts))
    ]
    out = smooth_track(samples, use_speed=True)
    assert len(out) == len(samples)
    assert all(math.isfinite(o.x_m) and math.isfinite(o.speed_mps) for o in out)
    # 末端應大致在直線上
    assert abs(out[-1].y_m) < 5.0


def test_fixed_lag_commits_only_after_lag():
    from services.postprocess.rts_smoother import FixedLagState, fixed_lag_commit

    state = FixedLagState(samples=[])
    # 餵 0..5s，每 0.2s 一點
    batch1 = [
        SmoothInput(t=T0 + timedelta(seconds=i * 0.2), x_m=float(i), y_m=0.0, hdop=1.0)
        for i in range(15)  # 0..2.8s
    ]
    out1 = fixed_lag_commit(state, batch1, lag_sec=3.0, use_speed=False)
    # t_max=2.8 < lag → 尚無 commit
    assert out1 == []

    batch2 = [
        SmoothInput(t=T0 + timedelta(seconds=3.0 + i * 0.2), x_m=float(15 + i), y_m=0.0, hdop=1.0)
        for i in range(20)  # 到 ~6.8s
    ]
    out2 = fixed_lag_commit(state, batch2, lag_sec=3.0, use_speed=False)
    assert len(out2) > 0
    # 最晚 commit ≤ t_max - 3
    assert out2[-1].t <= batch2[-1].t - timedelta(seconds=3.0 - 1e-6)
    # 再餵重複不應重複吐
    out3 = fixed_lag_commit(state, batch2, lag_sec=3.0, use_speed=False)
    assert out3 == []

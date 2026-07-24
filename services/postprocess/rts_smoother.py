"""離線 2D CV Kalman + RTS smoother（純函式、numpy only）。

賽後可看未來，用 forward KF + backward RTS 把 raw GPS 平滑、橋接短暫失鎖。
狀態在本地公尺座標；不碰 Influx / HTTP。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import numpy as np

from services.webapp.track_coords import local_m_to_latlng

# GPS 位置量測 χ² gating（2 dof, p=0.95）
CHI2_GATE_2DOF = 5.99
# 無量測跨度超過此值 → 輸出標 gap
GAP_MARK_SEC = 10.0
# hall 過原點回歸 R² 低於此 → 放棄 hall
HALL_MIN_R2 = 0.9
# 速度量測最小 |v|，避免除零線性化
_SPEED_EPS = 0.3
SEGMENT_GAP_SEC = 2.0


@dataclass(frozen=True)
class SmoothInput:
    t: datetime
    x_m: float
    y_m: float
    hdop: float | None = None
    speed_mps: float | None = None
    hall_hz: float | None = None
    course_deg: float | None = None
    h_acc_m: float | None = None
    pps_age_ms: float | None = None
    # 若 False：此點只有時間（純 predict 步，用於補密度）；目前未強制使用
    has_position: bool = True


@dataclass(frozen=True)
class SmoothOutput:
    t: datetime
    lat: float
    lon: float
    x_m: float
    y_m: float
    speed_mps: float
    sigma_m: float
    gap: bool
    vx_mps: float = 0.0
    vy_mps: float = 0.0
    pps_age_ms: float | None = None


def theil_sen_slope(xs: np.ndarray, ys: np.ndarray) -> float:
    """過原點 Theil–Sen：median(yi/xi) for xi≠0；無有效點回 0。"""
    xs = np.asarray(xs, dtype=float).ravel()
    ys = np.asarray(ys, dtype=float).ravel()
    mask = np.abs(xs) > 1e-12
    if not np.any(mask):
        return 0.0
    ratios = ys[mask] / xs[mask]
    return float(np.median(ratios))


def theil_sen_intercept_slope(
    xs: np.ndarray, ys: np.ndarray
) -> tuple[float, float]:
    """一般 Theil–Sen：斜率 = median pairwise slopes；截距 = median(y - m x)。"""
    xs = np.asarray(xs, dtype=float).ravel()
    ys = np.asarray(ys, dtype=float).ravel()
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if abs(dx) < 1e-15:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return 0.0, float(np.median(ys))
    m = float(np.median(slopes))
    b = float(np.median(ys - m * xs))
    return m, b


def hall_scale_m_per_rev(
    hall_hz: np.ndarray, gps_speed: np.ndarray
) -> tuple[float | None, float]:
    """hall_hz * m_per_rev ≈ gps_speed；回 (m_per_rev|None, r2)。"""
    h = np.asarray(hall_hz, dtype=float).ravel()
    v = np.asarray(gps_speed, dtype=float).ravel()
    mask = np.isfinite(h) & np.isfinite(v) & (h > 0.5) & (v > 0.5)
    if np.count_nonzero(mask) < 8:
        return None, 0.0
    h, v = h[mask], v[mask]
    m = theil_sen_slope(h, v)
    if m <= 0:
        return None, 0.0
    pred = m * h
    ss_res = float(np.sum((v - pred) ** 2))
    ss_tot = float(np.sum((v - np.mean(v)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    if r2 < HALL_MIN_R2:
        return None, r2
    return m, r2


def _gps_sigma(hdop: float | None, *, hdop_scale: float = 2.5, sigma_floor: float = 1.5) -> float:
    if hdop is None or not math.isfinite(hdop) or hdop <= 0:
        return max(sigma_floor, 3.0)
    return max(sigma_floor, float(hdop) * hdop_scale)


def _F_Q(dt: float, q_accel: float) -> tuple[np.ndarray, np.ndarray]:
    """等速模型：加速度白噪連續時間離散化。"""
    dt = max(float(dt), 1e-3)
    F = np.array(
        [
            [1.0, 0.0, dt, 0.0],
            [0.0, 1.0, 0.0, dt],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=float,
    )
    # Van Loan / 常見 CV 離散：q * [[dt^3/3, dt^2/2], [dt^2/2, dt]] per axis
    q = q_accel**2
    dt2 = dt * dt
    dt3 = dt2 * dt
    q11 = q * dt3 / 3.0
    q13 = q * dt2 / 2.0
    q33 = q * dt
    Q = np.array(
        [
            [q11, 0.0, q13, 0.0],
            [0.0, q11, 0.0, q13],
            [q13, 0.0, q33, 0.0],
            [0.0, q13, 0.0, q33],
        ],
        dtype=float,
    )
    return F, Q


@dataclass
class FixedLagState:
    """即時 fixed-lag RTS 狀態（保留短窗 raw，延遲 lag_sec 後 commit）。"""

    samples: list[SmoothInput]
    last_commit_t: datetime | None = None
    seen_ts: set[datetime] | None = None  # 去重；None → 懶初始化


def fixed_lag_commit(
    state: FixedLagState,
    new_samples: list[SmoothInput],
    *,
    lag_sec: float = 3.0,
    keep_sec: float = 60.0,
    q_accel: float = 3.0,
    use_speed: bool = True,
    hdop_scale: float = 2.5,
    sigma_floor: float = 1.5,
    chi2_gate: float = CHI2_GATE_2DOF,
    gap_mark_sec: float = GAP_MARK_SEC,
    model: str = "cv",
) -> list[SmoothOutput]:
    """餵入新 GPS → 對視窗跑 batch RTS → 只吐出已過 lag 且尚未 commit 的點。

    視窗長度 ~keep_sec（hall 尺度 + 橋接夠用）；CPU ≈ O(n_window) 每次，
    5Hz×60s ≈ 300 點，Orin/Pi 都可忽略。
    """
    if state.seen_ts is None:
        state.seen_ts = {s.t for s in state.samples}

    added = False
    for s in new_samples:
        if s.t in state.seen_ts:
            continue
        state.seen_ts.add(s.t)
        state.samples.append(s)
        added = True
    if not state.samples:
        return []
    if added:
        state.samples.sort(key=lambda s: s.t)

    t_max = state.samples[-1].t
    # 裁掉過舊
    t_keep = t_max - timedelta(seconds=keep_sec)
    if state.samples[0].t < t_keep:
        state.samples = [s for s in state.samples if s.t >= t_keep]
        state.seen_ts = {s.t for s in state.samples}

    t_commit = t_max - timedelta(seconds=lag_sec)
    if state.last_commit_t is not None and t_commit <= state.last_commit_t:
        return []

    if model == "ctrv":
        smoothed = smooth_track_ctrv(
            state.samples,
            q_accel=q_accel,
            hdop_scale=hdop_scale,
            sigma_floor=sigma_floor,
            chi2_gate=chi2_gate,
            gap_mark_sec=gap_mark_sec,
        )
    elif model == "cv":
        smoothed = smooth_track(
            state.samples,
            q_accel=q_accel,
            use_speed=use_speed,
            hdop_scale=hdop_scale,
            sigma_floor=sigma_floor,
            chi2_gate=chi2_gate,
            gap_mark_sec=gap_mark_sec,
        )
    else:
        raise ValueError(f"unsupported smoother model: {model}")
    out: list[SmoothOutput] = []
    for o in smoothed:
        if o.t > t_commit:
            break
        if state.last_commit_t is not None and o.t <= state.last_commit_t:
            continue
        out.append(o)
    if out:
        state.last_commit_t = out[-1].t
    return out


def _wrap_angle(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _split_on_time_gaps(
    samples: list[SmoothInput], threshold_sec: float
) -> list[list[SmoothInput]]:
    if not samples:
        return []
    chunks: list[list[SmoothInput]] = [[samples[0]]]
    for sample in samples[1:]:
        dt = (sample.t - chunks[-1][-1].t).total_seconds()
        if dt > threshold_sec:
            chunks.append([sample])
        else:
            chunks[-1].append(sample)
    return chunks


def _ctrv_transition(x: np.ndarray, dt: float) -> np.ndarray:
    """State [east, north, speed, math-heading, yaw-rate]."""
    px, py, speed, heading, omega = (float(v) for v in x)
    dt = max(float(dt), 1e-3)
    if abs(omega) > 1e-5:
        next_heading = heading + omega * dt
        px += speed / omega * (math.sin(next_heading) - math.sin(heading))
        py += speed / omega * (-math.cos(next_heading) + math.cos(heading))
    else:
        px += speed * math.cos(heading) * dt
        py += speed * math.sin(heading) * dt
        next_heading = heading + omega * dt
    return np.array([px, py, speed, _wrap_angle(next_heading), omega], dtype=float)


def _numeric_jacobian(x: np.ndarray, dt: float) -> np.ndarray:
    base = _ctrv_transition(x, dt)
    out = np.zeros((5, 5), dtype=float)
    steps = (1e-4, 1e-4, 1e-4, 1e-6, 1e-6)
    for column, step in enumerate(steps):
        moved = x.copy()
        moved[column] += step
        diff = _ctrv_transition(moved, dt) - base
        diff[3] = _wrap_angle(float(diff[3]))
        out[:, column] = diff / step
    return out


def _ctrv_measurement_update(
    x: np.ndarray,
    P: np.ndarray,
    sample: SmoothInput,
    *,
    hdop_scale: float,
    sigma_floor: float,
    chi2_gate: float,
) -> tuple[np.ndarray, np.ndarray, bool]:
    sigma = (
        max(sigma_floor, sample.h_acc_m)
        if sample.h_acc_m is not None and sample.h_acc_m > 0
        else _gps_sigma(sample.hdop, hdop_scale=hdop_scale, sigma_floor=sigma_floor)
    )
    H = np.zeros((2, 5), dtype=float)
    H[0, 0], H[1, 1] = 1.0, 1.0
    innovation = np.array([sample.x_m - x[0], sample.y_m - x[1]], dtype=float)
    R = np.diag([sigma * sigma, sigma * sigma])
    S = H @ P @ H.T + R
    S_inv = np.linalg.pinv(S)
    if float(innovation.T @ S_inv @ innovation) > chi2_gate:
        return x, P, False
    K = P @ H.T @ S_inv
    x = x + K @ innovation
    I = np.eye(5)
    P = (I - K @ H) @ P @ (I - K @ H).T + K @ R @ K.T

    if sample.speed_mps is not None and sample.speed_mps >= 0:
        Hs = np.zeros((1, 5), dtype=float)
        Hs[0, 2] = 1.0
        residual = np.array([float(sample.speed_mps) - x[2]])
        Ss = Hs @ P @ Hs.T + np.array([[1.0]])
        Ks = P @ Hs.T @ np.linalg.pinv(Ss)
        x = x + Ks @ residual
        P = (I - Ks @ Hs) @ P @ (I - Ks @ Hs).T + Ks @ Ks.T
    if sample.course_deg is not None and x[2] > 1.0:
        measured_heading = math.radians(90.0 - float(sample.course_deg))
        Hh = np.zeros((1, 5), dtype=float)
        Hh[0, 3] = 1.0
        residual = np.array([_wrap_angle(measured_heading - float(x[3]))])
        heading_var = math.radians(12.0) ** 2
        Sh = Hh @ P @ Hh.T + np.array([[heading_var]])
        Kh = P @ Hh.T @ np.linalg.pinv(Sh)
        x = x + Kh @ residual
        x[3] = _wrap_angle(float(x[3]))
        P = (I - Kh @ Hh) @ P @ (I - Kh @ Hh).T + Kh * heading_var @ Kh.T
    P = (P + P.T) * 0.5
    return x, P, True


def smooth_track_ctrv(
    samples: list[SmoothInput],
    *,
    q_accel: float = 3.0,
    q_yaw_accel: float = 0.8,
    hdop_scale: float = 2.5,
    sigma_floor: float = 1.5,
    chi2_gate: float = CHI2_GATE_2DOF,
    gap_mark_sec: float = GAP_MARK_SEC,
    segment_gap_sec: float = SEGMENT_GAP_SEC,
) -> list[SmoothOutput]:
    """Coordinated-turn EKF + nonlinear RTS smoother.

    This is separately selectable from the legacy CV model.  It remains
    unconstrained and does not turn a prediction through a long gap into a
    measurement.
    """
    if not samples:
        return []
    chunks = _split_on_time_gaps(samples, segment_gap_sec)
    if len(chunks) > 1:
        joined: list[SmoothOutput] = []
        for index, chunk in enumerate(chunks):
            part = smooth_track_ctrv(
                chunk,
                q_accel=q_accel,
                q_yaw_accel=q_yaw_accel,
                hdop_scale=hdop_scale,
                sigma_floor=sigma_floor,
                chi2_gate=chi2_gate,
                gap_mark_sec=gap_mark_sec,
                segment_gap_sec=float("inf"),
            )
            if part:
                if index > 0:
                    part[0] = replace(part[0], gap=True)
                if index < len(chunks) - 1:
                    part[-1] = replace(part[-1], gap=True)
            joined.extend(part)
        return joined
    n = len(samples)
    if samples[0].course_deg is not None:
        heading0 = math.radians(90.0 - float(samples[0].course_deg))
    elif n >= 2:
        heading0 = math.atan2(
            samples[1].y_m - samples[0].y_m,
            samples[1].x_m - samples[0].x_m,
        )
    else:
        heading0 = 0.0
    speed0 = float(samples[0].speed_mps or 0.0)
    x = np.array([samples[0].x_m, samples[0].y_m, speed0, heading0, 0.0])
    sigma0 = _gps_sigma(
        samples[0].hdop, hdop_scale=hdop_scale, sigma_floor=sigma_floor
    )
    P = np.diag([sigma0**2, sigma0**2, 25.0, math.pi**2, 1.0])
    filtered = np.zeros((n, 5))
    covariances = np.zeros((n, 5, 5))
    predicted = np.zeros((n, 5))
    predicted_cov = np.zeros((n, 5, 5))
    transitions = np.repeat(np.eye(5)[None, :, :], n, axis=0)
    no_position = np.zeros(n, dtype=bool)

    if samples[0].has_position:
        x, P, _ = _ctrv_measurement_update(
            x,
            P,
            samples[0],
            hdop_scale=hdop_scale,
            sigma_floor=sigma_floor,
            chi2_gate=chi2_gate,
        )
    else:
        no_position[0] = True
    filtered[0], covariances[0] = x, P
    predicted[0], predicted_cov[0] = x, P

    for i in range(1, n):
        dt = max((samples[i].t - samples[i - 1].t).total_seconds(), 1e-3)
        F = _numeric_jacobian(x, dt)
        Q = np.diag(
            [
                0.25 * q_accel**2 * dt**4,
                0.25 * q_accel**2 * dt**4,
                q_accel**2 * dt**2,
                0.25 * q_yaw_accel**2 * dt**4,
                q_yaw_accel**2 * dt**2,
            ]
        )
        x = _ctrv_transition(x, dt)
        P = F @ P @ F.T + Q
        transitions[i] = F
        predicted[i], predicted_cov[i] = x, P
        if samples[i].has_position:
            x, P, _ = _ctrv_measurement_update(
                x,
                P,
                samples[i],
                hdop_scale=hdop_scale,
                sigma_floor=sigma_floor,
                chi2_gate=chi2_gate,
            )
        else:
            no_position[i] = True
        filtered[i], covariances[i] = x, P

    smoothed = filtered.copy()
    smooth_cov = covariances.copy()
    for i in range(n - 2, -1, -1):
        gain = (
            covariances[i]
            @ transitions[i + 1].T
            @ np.linalg.pinv(predicted_cov[i + 1])
        )
        residual = smoothed[i + 1] - predicted[i + 1]
        residual[3] = _wrap_angle(float(residual[3]))
        smoothed[i] = filtered[i] + gain @ residual
        smoothed[i, 3] = _wrap_angle(float(smoothed[i, 3]))
        smooth_cov[i] = covariances[i] + gain @ (
            smooth_cov[i + 1] - predicted_cov[i + 1]
        ) @ gain.T
        smooth_cov[i] = (smooth_cov[i] + smooth_cov[i].T) * 0.5

    gap_flags = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if not no_position[i]:
            i += 1
            continue
        j = i
        while j < n and no_position[j]:
            j += 1
        end = samples[j].t if j < n else samples[j - 1].t
        if (end - samples[i].t).total_seconds() >= gap_mark_sec:
            gap_flags[i:j] = True
        i = j

    outputs: list[SmoothOutput] = []
    for i, state in enumerate(smoothed):
        speed = max(0.0, float(state[2]))
        vx = speed * math.cos(float(state[3]))
        vy = speed * math.sin(float(state[3]))
        lat, lon = local_m_to_latlng(float(state[0]), float(state[1]))
        sigma = math.sqrt(
            max(0.0, float(smooth_cov[i, 0, 0] + smooth_cov[i, 1, 1]))
        )
        outputs.append(
            SmoothOutput(
                t=samples[i].t,
                lat=lat,
                lon=lon,
                x_m=float(state[0]),
                y_m=float(state[1]),
                speed_mps=speed,
                sigma_m=sigma,
                gap=bool(gap_flags[i]),
                vx_mps=vx,
                vy_mps=vy,
                pps_age_ms=samples[i].pps_age_ms,
            )
        )
    return outputs


def smooth_track(
    samples: list[SmoothInput],
    *,
    q_accel: float = 3.0,
    use_speed: bool = True,
    hdop_scale: float = 2.5,
    sigma_floor: float = 1.5,
    chi2_gate: float = CHI2_GATE_2DOF,
    gap_mark_sec: float = GAP_MARK_SEC,
    segment_gap_sec: float = SEGMENT_GAP_SEC,
) -> list[SmoothOutput]:
    """Forward KF + RTS backward；回傳與 samples 等長的平滑點。"""
    if not samples:
        return []
    chunks = _split_on_time_gaps(samples, segment_gap_sec)
    if len(chunks) > 1:
        joined: list[SmoothOutput] = []
        for index, chunk in enumerate(chunks):
            part = smooth_track(
                chunk,
                q_accel=q_accel,
                use_speed=use_speed,
                hdop_scale=hdop_scale,
                sigma_floor=sigma_floor,
                chi2_gate=chi2_gate,
                gap_mark_sec=gap_mark_sec,
                segment_gap_sec=float("inf"),
            )
            if part:
                if index > 0:
                    part[0] = replace(part[0], gap=True)
                if index < len(chunks) - 1:
                    part[-1] = replace(part[-1], gap=True)
            joined.extend(part)
        return joined

    n = len(samples)
    # hall 尺度（整段一次）
    m_per_rev: float | None = None
    if use_speed:
        halls = np.array(
            [s.hall_hz if s.hall_hz is not None else np.nan for s in samples],
            dtype=float,
        )
        speeds = np.array(
            [s.speed_mps if s.speed_mps is not None else np.nan for s in samples],
            dtype=float,
        )
        m_per_rev, _ = hall_scale_m_per_rev(halls, speeds)

    # --- forward ---
    # 速度從 0 起步、P 放大：兩點差分在 GPS 噪聲下會給出假高速讓濾波飛掉
    x = np.array([samples[0].x_m, samples[0].y_m, 0.0, 0.0], dtype=float)
    sig0 = _gps_sigma(samples[0].hdop, hdop_scale=hdop_scale, sigma_floor=sigma_floor)
    P = np.diag([sig0**2, sig0**2, 100.0, 100.0]).astype(float)

    xs = np.zeros((n, 4), dtype=float)
    Ps = np.zeros((n, 4, 4), dtype=float)
    Fs = np.zeros((n, 4, 4), dtype=float)  # F used to go TO this index from prev
    meas_ok = np.zeros(n, dtype=bool)
    no_position = np.zeros(n, dtype=bool)  # 真·失鎖（非 gating 拒點）

    Fs[0] = np.eye(4)
    if samples[0].has_position:
        x, P, ok = _update_position(x, P, samples[0], hdop_scale, sigma_floor, chi2_gate)
        if ok:
            meas_ok[0] = True
        if use_speed:
            x, P = _update_speed(x, P, samples[0], m_per_rev)
    else:
        no_position[0] = True
    xs[0] = x
    Ps[0] = P

    for i in range(1, n):
        dt = (samples[i].t - samples[i - 1].t).total_seconds()
        F, Q = _F_Q(dt, q_accel)
        Fs[i] = F
        x = F @ x
        P = F @ P @ F.T + Q
        if samples[i].has_position:
            x, P, ok = _update_position(
                x, P, samples[i], hdop_scale, sigma_floor, chi2_gate
            )
            if ok:
                meas_ok[i] = True
            if use_speed:
                x, P = _update_speed(x, P, samples[i], m_per_rev)
        else:
            no_position[i] = True
        xs[i] = x
        Ps[i] = P

    # --- RTS backward ---
    x_s = xs.copy()
    P_s = Ps.copy()
    for i in range(n - 2, -1, -1):
        dt = (samples[i + 1].t - samples[i].t).total_seconds()
        F, Q = _F_Q(dt, q_accel)
        x_pred = F @ xs[i]
        P_pred = F @ Ps[i] @ F.T + Q
        try:
            P_pred_inv = np.linalg.inv(P_pred)
        except np.linalg.LinAlgError:
            P_pred_inv = np.linalg.pinv(P_pred)
        C = Ps[i] @ F.T @ P_pred_inv
        x_s[i] = xs[i] + C @ (x_s[i + 1] - x_pred)
        P_s[i] = Ps[i] + C @ (P_s[i + 1] - P_pred) @ C.T

    # gap：連續無位置量測（失鎖），牆鐘跨度 >= gap_mark_sec
    gap_flags = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if not no_position[i]:
            i += 1
            continue
        j = i
        while j < n and no_position[j]:
            j += 1
        t0 = samples[i].t
        t1 = samples[min(j, n) - 1].t
        if j < n:
            t1 = samples[j].t
        if (t1 - t0).total_seconds() >= gap_mark_sec - 1e-9:
            gap_flags[i:j] = True
        i = j

    out: list[SmoothOutput] = []
    for i in range(n):
        xi = x_s[i]
        Pi = P_s[i]
        lat, lon = local_m_to_latlng(float(xi[0]), float(xi[1]))
        speed = math.hypot(float(xi[2]), float(xi[3]))
        sigma = math.sqrt(max(0.0, float(Pi[0, 0] + Pi[1, 1])))
        out.append(
            SmoothOutput(
                t=samples[i].t,
                lat=lat,
                lon=lon,
                x_m=float(xi[0]),
                y_m=float(xi[1]),
                speed_mps=speed,
                sigma_m=sigma,
                gap=bool(gap_flags[i]),
                vx_mps=float(xi[2]),
                vy_mps=float(xi[3]),
                pps_age_ms=samples[i].pps_age_ms,
            )
        )
    return out


def _update_position(
    x: np.ndarray,
    P: np.ndarray,
    s: SmoothInput,
    hdop_scale: float,
    sigma_floor: float,
    chi2_gate: float,
) -> tuple[np.ndarray, np.ndarray, bool]:
    H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=float)
    z = np.array([s.x_m, s.y_m], dtype=float)
    sig = _gps_sigma(s.hdop, hdop_scale=hdop_scale, sigma_floor=sigma_floor)
    R = np.diag([sig * sig, sig * sig])
    y = z - H @ x
    S = H @ P @ H.T + R
    try:
        S_inv = np.linalg.inv(S)
    except np.linalg.LinAlgError:
        S_inv = np.linalg.pinv(S)
    innov2 = float(y.T @ S_inv @ y)
    if innov2 > chi2_gate:
        return x, P, False
    K = P @ H.T @ S_inv
    x = x + K @ y
    I = np.eye(4)
    P = (I - K @ H) @ P @ (I - K @ H).T + K @ R @ K.T
    return x, P, True


def _update_speed(
    x: np.ndarray,
    P: np.ndarray,
    s: SmoothInput,
    m_per_rev: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """|v| 量測；優先 hall*m_per_rev，否則 gps_speed。"""
    z_speed: float | None = None
    r_var = 1.0
    if m_per_rev is not None and s.hall_hz is not None and s.hall_hz > 0.5:
        z_speed = float(s.hall_hz) * m_per_rev
        r_var = 0.5**2
    elif s.speed_mps is not None and s.speed_mps >= 0:
        z_speed = float(s.speed_mps)
        r_var = 1.0**2
    if z_speed is None:
        return x, P

    vx, vy = float(x[2]), float(x[3])
    speed = math.hypot(vx, vy)
    if speed < _SPEED_EPS:
        return x, P
    # h = |v|, H = [0,0, vx/|v|, vy/|v|]
    H = np.array([[0.0, 0.0, vx / speed, vy / speed]], dtype=float)
    y = np.array([z_speed - speed], dtype=float)
    S = H @ P @ H.T + np.array([[r_var]], dtype=float)
    # 1D gating 稍寬：χ² 1dof ~3.84，這裡用 9 避免過度拒
    try:
        S_inv = 1.0 / float(S[0, 0])
    except ZeroDivisionError:
        return x, P
    if float(y[0] * S_inv * y[0]) > 9.0:
        return x, P
    K = (P @ H.T) * S_inv
    x = x + (K @ y).ravel()
    I = np.eye(4)
    R = np.array([[r_var]], dtype=float)
    P = (I - K @ H) @ P @ (I - K @ H).T + K @ R @ K.T
    return x, P

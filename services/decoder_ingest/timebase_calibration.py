"""Decoder ↔ GPS 時基校準（純函式、無 IO）。

配對不靠絕對時間（偏移未知），改用圈速序列 sliding alignment，
再估常數 offset + Theil–Sen 線性漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from services.postprocess.rts_smoother import theil_sen_intercept_slope


@dataclass(frozen=True)
class TimebaseResult:
    offset_sec: float  # decoder_time - gps_time（正 = decoder 鐘快）
    drift_sec_per_hour: float
    matched_pairs: int
    residual_std_sec: float
    quality: str  # good | marginal | failed


def _diffs(times: list[datetime]) -> np.ndarray:
    if len(times) < 2:
        return np.array([], dtype=float)
    return np.array(
        [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)],
        dtype=float,
    )


def _median_abs_err(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return float("inf")
    return float(np.median(np.abs(a[:n] - b[:n])))


def _best_lag_align(
    dec_laps: np.ndarray, gps_laps: np.ndarray, *, max_lag: int = 3
) -> tuple[int, int, int]:
    """回傳 (dec_start, gps_start, length) 使圈速序列 MAE 最小。

    lag>0：decoder 序列相對 GPS 往後錯（丟掉 decoder 前 lag 圈）。
    同時掃兩邊起點偏移以容忍單邊漏圈。
    """
    best = (0, 0, 0)
    best_mae = float("inf")
    if len(dec_laps) == 0 or len(gps_laps) == 0:
        return best

    for dec_off in range(0, max_lag + 1):
        for gps_off in range(0, max_lag + 1):
            d = dec_laps[dec_off:]
            g = gps_laps[gps_off:]
            n = min(len(d), len(g))
            if n < 1:
                continue
            mae = _median_abs_err(d[:n], g[:n])
            # 偏好更長對齊；MAE 相近時取較長
            score = mae - 0.001 * n
            if score < best_mae - 1e-12 or (
                abs(score - best_mae) < 1e-12 and n > best[2]
            ):
                best_mae = score
                best = (dec_off, gps_off, n)
    return best


def calibrate(
    decoder_passings: list[datetime],
    gps_crossings: list[datetime],
) -> TimebaseResult:
    """用圈速序列對齊後估 offset / drift。"""
    # residual 必須 finite（JSON / SQLite 都不能吃 inf）
    failed = TimebaseResult(
        offset_sec=0.0,
        drift_sec_per_hour=0.0,
        matched_pairs=0,
        residual_std_sec=0.0,
        quality="failed",
    )
    if len(decoder_passings) < 2 or len(gps_crossings) < 2:
        return failed

    dec_laps = _diffs(decoder_passings)
    gps_laps = _diffs(gps_crossings)
    # 粗檢：中位圈速差太大 → 無關序列
    if len(dec_laps) >= 1 and len(gps_laps) >= 1:
        med_ratio = float(np.median(dec_laps)) / max(float(np.median(gps_laps)), 1e-6)
        if med_ratio < 0.4 or med_ratio > 2.5:
            # 仍嘗試對齊，但後面 quality 會反映
            pass

    dec_off, gps_off, n = _best_lag_align(dec_laps, gps_laps, max_lag=3)
    if n < 1:
        return failed

    # 對齊後的事件：用圈速差分的「結束事件」配對
    # decoder_passings[i+1] 對應 lap ending at that passing
    # 配對：dec_passings[dec_off+1 : dec_off+1+n] ↔ gps_crossings[gps_off+1 : gps_off+1+n]
    dec_ev = decoder_passings[dec_off + 1 : dec_off + 1 + n]
    gps_ev = gps_crossings[gps_off + 1 : gps_off + 1 + n]
    if len(dec_ev) != len(gps_ev) or not dec_ev:
        return failed

    # 對齊品質：對齊後圈速 MAE
    d_seg = dec_laps[dec_off : dec_off + n]
    g_seg = gps_laps[gps_off : gps_off + n]
    lap_mae = float(np.median(np.abs(d_seg - g_seg)))
    # 圈速對不上（例如 >8s MAE）視為無關
    if lap_mae > 8.0:
        return failed

    deltas = np.array(
        [(d - g).total_seconds() for d, g in zip(dec_ev, gps_ev)],
        dtype=float,
    )
    offset = float(np.median(deltas))

    # Theil–Sen：delta ≈ offset0 + drift_per_sec * gps_unix
    gps_t0 = gps_ev[0].timestamp()
    xs = np.array([(g.timestamp() - gps_t0) for g in gps_ev], dtype=float)
    ys = deltas
    slope_per_sec, _intercept = theil_sen_intercept_slope(xs, ys)
    drift_per_hour = float(slope_per_sec) * 3600.0

    # 殘差：扣掉 offset+drift 後
    pred = offset + slope_per_sec * xs
    # 用 median offset 當常數項更穩：重新 fit residual around median offset + drift
    resid = deltas - (offset + slope_per_sec * xs)
    resid_std = float(np.std(resid, ddof=1)) if len(resid) >= 2 else float(abs(resid[0]))
    if not np.isfinite(resid_std):
        resid_std = 0.0
    if not np.isfinite(offset):
        offset = 0.0
    if not np.isfinite(drift_per_hour):
        drift_per_hour = 0.0

    pairs = len(dec_ev)
    if pairs >= 5 and resid_std <= 0.15:
        quality = "good"
    elif pairs >= 3 and resid_std <= 0.5:
        quality = "marginal"
    else:
        quality = "failed"

    if quality == "failed":
        return TimebaseResult(
            offset_sec=offset,
            drift_sec_per_hour=drift_per_hour,
            matched_pairs=pairs,
            residual_std_sec=resid_std,
            quality="failed",
        )

    return TimebaseResult(
        offset_sec=offset,
        drift_sec_per_hour=drift_per_hour,
        matched_pairs=pairs,
        residual_std_sec=resid_std,
        quality=quality,
    )


def apply_timebase(
    gps_time: datetime,
    *,
    offset_sec: float,
    drift_sec_per_hour: float,
    t0: datetime,
) -> datetime:
    """把 GPS 時間映射到 decoder 時基：decoder ≈ gps + offset + drift*(gps-t0)。"""
    from datetime import timedelta

    hours = (gps_time - t0).total_seconds() / 3600.0
    return gps_time + timedelta(seconds=offset_sec + drift_sec_per_hour * hours)


def gps_to_decoder_delta(
    decoder_time: datetime,
    gps_time: datetime,
    *,
    offset_sec: float,
    drift_sec_per_hour: float,
    t0: datetime,
) -> float:
    """decoder_time - mapped_gps（秒）。"""
    mapped = apply_timebase(
        gps_time,
        offset_sec=offset_sec,
        drift_sec_per_hour=drift_sec_per_hour,
        t0=t0,
    )
    return (decoder_time - mapped).total_seconds()

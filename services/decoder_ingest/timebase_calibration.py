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


def align_lap_sequences(
    dec_laps: np.ndarray, gps_laps: np.ndarray
) -> list[tuple[int, int]]:
    """Smith-Waterman alignment of lap intervals.

    Unlike the old fixed ±3 offset scan, gaps may occur anywhere.  This
    tolerates a missing decoder passing, missing GNSS crossing, duplicate
    crossing, and a session that starts mid-sequence.  Returned indices refer
    to lap intervals; event ``i + 1`` is that interval's ending crossing.
    """
    nd, ng = len(dec_laps), len(gps_laps)
    if nd == 0 or ng == 0:
        return []
    score = np.zeros((nd + 1, ng + 1), dtype=float)
    trace = np.zeros((nd + 1, ng + 1), dtype=np.int8)  # 1 match, 2 up, 3 left
    best = (0.0, 0, 0)
    gap_penalty = 1.5
    for i in range(1, nd + 1):
        for j in range(1, ng + 1):
            err = abs(float(dec_laps[i - 1]) - float(gps_laps[j - 1]))
            match_reward = 3.0 - 0.5 * min(err, 20.0)
            choices = (
                0.0,
                score[i - 1, j - 1] + match_reward,
                score[i - 1, j] - gap_penalty,
                score[i, j - 1] - gap_penalty,
            )
            k = int(np.argmax(choices))
            score[i, j] = choices[k]
            trace[i, j] = k
            if score[i, j] > best[0]:
                best = (float(score[i, j]), i, j)
    _, i, j = best
    pairs: list[tuple[int, int]] = []
    while i > 0 and j > 0 and score[i, j] > 0:
        k = int(trace[i, j])
        if k == 1:
            err = abs(float(dec_laps[i - 1]) - float(gps_laps[j - 1]))
            if err <= 8.0:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif k == 2:
            i -= 1
        elif k == 3:
            j -= 1
        else:
            break
    pairs.reverse()
    return pairs


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

    aligned = align_lap_sequences(dec_laps, gps_laps)
    if not aligned:
        return failed

    dec_ev = [decoder_passings[i + 1] for i, _ in aligned]
    gps_ev = [gps_crossings[j + 1] for _, j in aligned]
    if len(dec_ev) != len(gps_ev) or not dec_ev:
        return failed

    # 對齊品質：對齊後圈速 MAE
    d_seg = np.array([dec_laps[i] for i, _ in aligned], dtype=float)
    g_seg = np.array([gps_laps[j] for _, j in aligned], dtype=float)
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

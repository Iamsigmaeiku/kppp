"""Sequence-aligned independent-vs-decoder lap error statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from services.decoder_ingest.timebase_calibration import align_lap_sequences


@dataclass(frozen=True)
class LapComparison:
    matched_pairs: int
    decoder_unmatched: int
    independent_unmatched: int
    signed_errors_sec: tuple[float, ...]
    bias_ms: float | None
    mae_ms: float | None
    rmse_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_abs_ms: float | None


def compare_lap_sequences(
    decoder_laps_sec: list[float],
    independent_laps_sec: list[float],
) -> LapComparison:
    decoder = np.asarray(decoder_laps_sec, dtype=float)
    independent = np.asarray(independent_laps_sec, dtype=float)
    pairs = align_lap_sequences(decoder, independent)
    errors = np.asarray(
        [independent[j] - decoder[i] for i, j in pairs], dtype=float
    )
    if not len(errors):
        return LapComparison(
            matched_pairs=0,
            decoder_unmatched=len(decoder),
            independent_unmatched=len(independent),
            signed_errors_sec=(),
            bias_ms=None,
            mae_ms=None,
            rmse_ms=None,
            p95_ms=None,
            p99_ms=None,
            max_abs_ms=None,
        )
    abs_errors = np.abs(errors)
    return LapComparison(
        matched_pairs=len(pairs),
        decoder_unmatched=len(decoder) - len({i for i, _ in pairs}),
        independent_unmatched=len(independent) - len({j for _, j in pairs}),
        signed_errors_sec=tuple(float(v) for v in errors),
        bias_ms=float(np.mean(errors) * 1000.0),
        mae_ms=float(np.mean(abs_errors) * 1000.0),
        rmse_ms=float(math.sqrt(float(np.mean(errors**2))) * 1000.0),
        p95_ms=float(np.percentile(abs_errors, 95) * 1000.0),
        p99_ms=float(np.percentile(abs_errors, 99) * 1000.0),
        max_abs_ms=float(np.max(abs_errors) * 1000.0),
    )

from __future__ import annotations

import pytest

from services.timing.lap_compare import compare_lap_sequences


def test_compare_laps_tolerates_missing_and_duplicate() -> None:
    decoder = [50.0, 51.0, 49.5, 50.5, 50.2]
    independent = [50.02, 51.01, 12.0, 49.48, 50.53, 50.19]
    result = compare_lap_sequences(decoder, independent)
    assert result.matched_pairs == 5
    assert result.independent_unmatched == 1
    assert result.mae_ms == pytest.approx(18.0, abs=0.01)
    assert result.max_abs_ms == pytest.approx(30.0, abs=0.01)


def test_compare_laps_reports_no_fake_metrics_without_pairs() -> None:
    result = compare_lap_sequences([], [50.0])
    assert result.matched_pairs == 0
    assert result.mae_ms is None

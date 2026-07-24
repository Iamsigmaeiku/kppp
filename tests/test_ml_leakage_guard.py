from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "smartkart-lstm"))
from data.build_dataset import assert_no_group_leakage  # noqa: E402


def test_leakage_guard_checks_session_day_and_kart() -> None:
    meta = pd.DataFrame(
        {
            "session_id": ["s1", "s1", "s2", "s2"],
            "day": ["d1", "d1", "d2", "d2"],
            "kart_id": ["k1", "k1", "k2", "k2"],
        }
    )
    assert_no_group_leakage(meta, np.array([0, 1]), np.array([2, 3]))


def test_leakage_guard_rejects_same_day_across_sessions() -> None:
    meta = pd.DataFrame(
        {
            "session_id": ["s1", "s2"],
            "day": ["d1", "d1"],
            "kart_id": ["k1", "k2"],
        }
    )
    with pytest.raises(ValueError, match="day"):
        assert_no_group_leakage(meta, np.array([0]), np.array([1]))

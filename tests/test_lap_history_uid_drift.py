"""get_lap_history must resolve 77/78 UID drift."""

from __future__ import annotations

from services.decoder_ingest.lap_tracker import normalize_transponder_id


def test_normalize_matches_drift_to_77():
    assert normalize_transponder_id("140215494F78") == "140215494F77"
    assert normalize_transponder_id("140215494F68") == "140215494F77"
    assert normalize_transponder_id("140215494F6D") == "140215494F77"

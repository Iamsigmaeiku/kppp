"""list_sessions 起訖時間：session_id 解析與舊資料 fallback。"""

from __future__ import annotations

from datetime import datetime, timezone

from services.decoder_ingest.influx_reader import started_at_from_session_id


def test_started_at_from_session_id():
    assert started_at_from_session_id("sess-20260710-082947") == datetime(
        2026, 7, 10, 8, 29, 47, tzinfo=timezone.utc
    )


def test_started_at_from_session_id_rejects_garbage():
    assert started_at_from_session_id("not-a-session") is None
    assert started_at_from_session_id("sess-bogus") is None
    assert started_at_from_session_id("") is None

"""SessionManager.archive_and_reset：驗證「先歸檔、後清空」的順序、
session_id 每次 reset 後都會更換，以及 idle_seconds() 的閒置計算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.decoder_ingest.lap_tracker import LapTracker
from services.decoder_ingest.packet_parser import PassingEvent
from services.decoder_ingest.session_manager import SessionManager

TID = "AABBCCDDEEFF"


class _RecordingWriter:
    """假 writer：只記錄呼叫當下 lap_tracker 的狀態，藉此驗證
    archive_and_reset 一定「先歸檔、後清空」，不會兩者順序顛倒。
    """

    def __init__(self, lap_tracker: LapTracker) -> None:
        self.lap_tracker = lap_tracker
        self.written_points: list = []
        self.states_at_write: list[dict] | None = None

    async def write_points_now(self, points) -> None:
        self.states_at_write = self.lap_tracker.all_states()
        self.written_points.extend(points)


def _make_tracker_with_data() -> LapTracker:
    tracker = LapTracker(noise_threshold_sec=1.0, car_number_map={TID: "42"})
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tracker.record_passing(
        PassingEvent(transponder_id=TID, raw_payload=TID + "0000", received_at=t0)
    )
    tracker.record_passing(
        PassingEvent(
            transponder_id=TID,
            raw_payload=TID + "0001",
            received_at=t0 + timedelta(seconds=45.0),
        )
    )
    return tracker


async def test_archive_and_reset_archives_before_clearing():
    tracker = _make_tracker_with_data()
    writer = _RecordingWriter(tracker)
    manager = SessionManager.start_new(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    old_session_id = manager.current_session_id

    new_session_id = await manager.archive_and_reset(tracker, writer, trigger="manual")

    assert writer.states_at_write, "archive should have seen non-empty state before clear"
    assert writer.states_at_write[0]["transponder_id"] == TID
    assert writer.states_at_write[0]["lap_count"] == 1

    assert tracker.all_states() == []
    assert new_session_id != old_session_id
    assert manager.current_session_id == new_session_id


async def test_archive_and_reset_skips_write_when_no_states():
    tracker = LapTracker(noise_threshold_sec=1.0)
    writer = _RecordingWriter(tracker)
    manager = SessionManager.start_new()

    await manager.archive_and_reset(tracker, writer, trigger="manual")

    assert writer.written_points == []
    assert writer.states_at_write is None


def test_idle_seconds():
    manager = SessionManager.start_new(at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
    later = datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    assert manager.idle_seconds(at=later) == pytest.approx(300.0)

    manager.note_activity(at=later)
    even_later = later + timedelta(seconds=10)
    assert manager.idle_seconds(at=even_later) == pytest.approx(10.0)

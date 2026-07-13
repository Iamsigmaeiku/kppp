"""snapshot 必須綁 session_id，否則上一節圈速會灌進下一節。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.decoder_ingest.lap_tracker import LapTracker
from services.decoder_ingest.session_manager import SessionManager
from services.decoder_ingest.session_snapshot import (
    build_snapshot_dict,
    load_snapshot,
    write_snapshot,
)


def test_orphan_snapshot_without_session_id_is_discarded(tmp_path: Path):
    path = tmp_path / "session_snapshot.json"
    path.write_text(
        json.dumps(
            {
                "states": {
                    "AABBCCDDEEFF": {
                        "lap_count": 14,
                        "best_lap_time": 50.949,
                        "last_lap_time": 51.247,
                        "lap_history": [50.949],
                        "last_passing_at": None,
                        "last_raw_payload": None,
                        "last_passing_tick": None,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    tracker = LapTracker()
    restored = load_snapshot(tracker, path)
    assert restored is None
    assert tracker.all_states() == []


def test_snapshot_roundtrip_restores_same_session_id(tmp_path: Path):
    path = tmp_path / "session_snapshot.json"
    tracker = LapTracker()
    started = datetime(2026, 7, 12, 4, 52, 44, tzinfo=timezone.utc)
    manager = SessionManager.resume(
        session_id="sess-20260712-045244",
        started_at=started,
        last_activity_at=started,
    )
    # 手動塞一筆狀態（不走 record_passing，只測序列化）
    tracker.load_snapshot(
        {
            "states": {
                "140211084277": {
                    "lap_count": 2,
                    "best_lap_time": 56.673,
                    "last_lap_time": 58.201,
                    "lap_history": [56.673, 58.201],
                    "last_passing_at": started.isoformat(),
                    "last_raw_payload": None,
                    "last_passing_tick": None,
                }
            }
        }
    )
    write_snapshot(tracker, manager, path)

    tracker2 = LapTracker()
    restored = load_snapshot(tracker2, path)
    assert restored is not None
    assert restored.session_manager.current_session_id == "sess-20260712-045244"
    assert restored.session_manager.session_started_at == started
    assert restored.state_count == 1
    assert tracker2.all_states()[0]["best_lap_time"] == 56.673


def test_write_snapshot_after_reset_has_new_session_and_empty_states(tmp_path: Path):
    path = tmp_path / "session_snapshot.json"
    tracker = LapTracker()
    tracker.load_snapshot(
        {
            "states": {
                "AABBCCDDEEFF": {
                    "lap_count": 5,
                    "best_lap_time": 50.0,
                    "last_lap_time": 51.0,
                    "lap_history": [50.0],
                    "last_passing_at": None,
                    "last_raw_payload": None,
                    "last_passing_tick": None,
                }
            }
        }
    )
    old = SessionManager.start_new(
        at=datetime(2026, 7, 12, 4, 5, 31, tzinfo=timezone.utc)
    )
    write_snapshot(tracker, old, path)

    tracker.reset_session()
    new = SessionManager.start_new(
        at=datetime(2026, 7, 12, 4, 52, 44, tzinfo=timezone.utc)
    )
    write_snapshot(tracker, new, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == new.current_session_id
    assert data["session_id"] != old.current_session_id
    assert data["states"] == {}

    # 重啟復原：必須是空的新場次，不能把 50.0 灌回來
    tracker3 = LapTracker()
    restored = load_snapshot(tracker3, path)
    assert restored is not None
    assert restored.session_manager.current_session_id == new.current_session_id
    assert tracker3.all_states() == []


def test_build_snapshot_dict_includes_session_fields():
    tracker = LapTracker()
    manager = SessionManager.resume(
        session_id="sess-20260712-040531",
        started_at=datetime(2026, 7, 12, 4, 5, 31, tzinfo=timezone.utc),
    )
    data = build_snapshot_dict(tracker, manager)
    assert data["session_id"] == "sess-20260712-040531"
    assert "session_started_at" in data
    assert "last_activity_at" in data
    assert data["states"] == {}

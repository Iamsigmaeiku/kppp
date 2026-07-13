"""本地 session snapshot：崩潰復原用，必須與 session_id 綁定。

歷史 bug：snapshot 只存 lap 狀態、啟動時卻 SessionManager.start_new() 發新
session_id → 上一節的圈速被灌進下一節，之後 auto_idle/manual reset 再歸檔
就會出現「第 9 節長得跟第 5 節一模一樣」。

規則：
- 寫入時一定帶 session_id / session_started_at / last_activity_at
- 讀取時只有 session_id 齊全才復原 lap 狀態；缺 session_id 的舊格式
  orphan snapshot 直接丟棄，绝不灌進新場次
- 寫入用 threading.Lock，避免 snapshot_loop 與 reset 交錯把舊狀態蓋回去
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .lap_tracker import LapTracker
from .session_manager import SessionManager

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class SnapshotRestore:
    session_manager: SessionManager
    state_count: int


def build_snapshot_dict(
    lap_tracker: LapTracker, session_manager: SessionManager
) -> dict:
    return {
        "session_id": session_manager.current_session_id,
        "session_started_at": session_manager.session_started_at.isoformat(),
        "last_activity_at": session_manager.last_activity_at.isoformat(),
        "states": lap_tracker.to_snapshot_dict()["states"],
    }


def write_snapshot(
    lap_tracker: LapTracker, session_manager: SessionManager, path: Path
) -> None:
    """原子寫入；與 reset 共用 lock，避免舊狀態覆寫清空後的 snapshot。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with _write_lock:
        data = build_snapshot_dict(lap_tracker, session_manager)
        tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_snapshot(
    lap_tracker: LapTracker, path: Path
) -> SnapshotRestore | None:
    """啟動時復原。回傳 SnapshotRestore 代表 lap + session 一起復原成功；
    回傳 None 代表沒有可安全復原的 snapshot（呼叫端應 start_new()）。

    缺 session_id 的舊 snapshot：即使有 states 也丟棄，避免跨場次污染。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("failed to read snapshot %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("snapshot %s is not an object; ignoring", path)
        return None

    session_id = data.get("session_id")
    states = data.get("states") or {}
    if not isinstance(session_id, str) or not session_id.startswith("sess-"):
        if states:
            logger.warning(
                "discarding orphan snapshot %s (%d transponder states without "
                "session_id) — refusing to load into a new session",
                path,
                len(states) if isinstance(states, dict) else 0,
            )
        return None

    started_at = _parse_iso(data.get("session_started_at"))
    last_activity_at = _parse_iso(data.get("last_activity_at"))
    if started_at is None:
        # 至少能從 session_id 還原開始時間；活動時間退回 started_at
        from .influx_reader import started_at_from_session_id

        started_at = started_at_from_session_id(session_id)
    if started_at is None:
        logger.warning(
            "snapshot %s has session_id=%s but no parseable start time; ignoring",
            path,
            session_id,
        )
        return None
    if last_activity_at is None:
        last_activity_at = started_at

    lap_tracker.load_snapshot({"states": states if isinstance(states, dict) else {}})
    manager = SessionManager.resume(
        session_id=session_id,
        started_at=started_at,
        last_activity_at=last_activity_at,
    )
    restored = len(states) if isinstance(states, dict) else 0
    logger.info(
        "restored %d transponder(s) into session_id=%s from snapshot %s",
        restored,
        session_id,
        path,
    )
    return SnapshotRestore(session_manager=manager, state_count=restored)

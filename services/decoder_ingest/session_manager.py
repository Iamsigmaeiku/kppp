"""場次 (session/heat) 邊界管理：reset 永遠先把目前狀態歸檔進 InfluxDB
再清空 lap_tracker，確保任何一次 reset（手動或閒置安全網）都不會遺失
資料。

自動偵測「新的一節開始」本身刻意不做：現場常見的中停/紅旗會被誤判成
場次結束，而 Influx 裡一旦寫錯 session_id 就無法乾淨地合併回去，風險
不可逆。因此權威訊號只有手動 reset；auto_idle 只是防止忘記按重置的
安全網，門檻應設得夠長（見 AUTO_ARCHIVE_IDLE_SEC），不是真正的場次
邊界判斷。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from influxdb_client import Point

from .influx_writer import InfluxWriter
from .lap_tracker import LapTracker

logger = logging.getLogger(__name__)

ResetTrigger = Literal["manual", "auto_idle"]


def _new_session_id(at: datetime | None = None) -> str:
    ts = (at or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"sess-{ts}"


@dataclass(slots=True)
class SessionManager:
    current_session_id: str
    session_started_at: datetime
    last_activity_at: datetime

    @classmethod
    def start_new(cls, *, at: datetime | None = None) -> "SessionManager":
        now = at or datetime.now(timezone.utc)
        return cls(
            current_session_id=_new_session_id(now),
            session_started_at=now,
            last_activity_at=now,
        )

    def note_activity(self, *, at: datetime | None = None) -> None:
        self.last_activity_at = at or datetime.now(timezone.utc)

    def idle_seconds(self, *, at: datetime | None = None) -> float:
        now = at or datetime.now(timezone.utc)
        return (now - self.last_activity_at).total_seconds()

    async def archive_and_reset(
        self,
        lap_tracker: LapTracker,
        writer: InfluxWriter,
        *,
        trigger: ResetTrigger,
        at: datetime | None = None,
    ) -> str:
        """把目前場次每支 transponder 的狀態寫成一筆 session_archive
        point，force 寫入（含 fallback）完成後才清空 lap_tracker、換發新
        session_id。回傳新的 session_id。
        """
        now = at or datetime.now(timezone.utc)
        # 場次真的結束了：把每台車還在跑、從沒被 record_passing() 算成
        # 正式一圈的「本圈」補記成這一節的最後一圈，歸檔資料才完整
        # （見 LapTracker.finalize_in_progress_laps 說明）。
        lap_tracker.finalize_in_progress_laps(at=now)
        points = self._build_archive_points(lap_tracker, at=now, trigger=trigger)
        if points:
            await writer.write_points_now(points)

        archived_session_id = self.current_session_id
        lap_tracker.reset_session()
        self.current_session_id = _new_session_id(now)
        self.session_started_at = now
        self.last_activity_at = now

        logger.info(
            "session archived and reset: trigger=%s archived_session_id=%s "
            "new_session_id=%s archived_transponders=%d",
            trigger,
            archived_session_id,
            self.current_session_id,
            len(points),
        )
        return self.current_session_id

    def _build_archive_points(
        self, lap_tracker: LapTracker, *, at: datetime, trigger: ResetTrigger
    ) -> list[Point]:
        points: list[Point] = []
        for state in lap_tracker.all_states():
            # all_states() 已是 broadcast dict 形式（見
            # LapTracker._to_broadcast_dict），欄位命名沿用同一份定義。
            point = (
                Point("session_archive")
                .tag("session_id", self.current_session_id)
                .tag("transponder_id", state["transponder_id"])
                .tag("car_number", state["car_number"])
                .field("registered", bool(state["registered"]))
                .field("lap_count", int(state["lap_count"]))
                .field("best_lap_time", float(state["best_lap_time"] or 0.0))
                .field("last_lap_time", float(state["last_lap_time"] or 0.0))
                .field("lap_history_json", json.dumps(state["lap_history"]))
                .field("reset_trigger", trigger)
                .time(at)
            )
            points.append(point)
        return points

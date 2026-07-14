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
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from influxdb_client import Point

from .influx_writer import InfluxWriter
from .lap_tracker import LapTracker

logger = logging.getLogger(__name__)

ResetTrigger = Literal["manual", "auto_idle", "day_rollover"]


def local_date_for(at: datetime, tz_name: str) -> date:
    dt = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).date()


def _new_session_id(at: datetime | None = None) -> str:
    ts = (at or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"sess-{ts}"


@dataclass(slots=True)
class SessionManager:
    current_session_id: str
    session_started_at: datetime
    last_activity_at: datetime
    # 是否已拿到「今天第 N 節」編號。重啟/空 reset 先不編號，等第一筆
    # 真實過線再編，避免空殼場次把第 6~9 節燒掉、下一場實賽變成第 10 節。
    numbered: bool = False

    @classmethod
    def start_new(cls, *, at: datetime | None = None) -> "SessionManager":
        now = at or datetime.now(timezone.utc)
        return cls(
            current_session_id=_new_session_id(now),
            session_started_at=now,
            last_activity_at=now,
            numbered=False,
        )

    @classmethod
    def resume(
        cls,
        *,
        session_id: str,
        started_at: datetime,
        last_activity_at: datetime | None = None,
        numbered: bool = True,
    ) -> "SessionManager":
        """從 snapshot 復原同一個 session_id（崩潰復原），不要發新號。"""
        return cls(
            current_session_id=session_id,
            session_started_at=started_at,
            last_activity_at=last_activity_at or started_at,
            # 復原中的場次多半已在 SQLite 編過號；若沒有，第一筆過線會補編。
            numbered=numbered,
        )

    def note_activity(self, *, at: datetime | None = None) -> None:
        self.last_activity_at = at or datetime.now(timezone.utc)

    def idle_seconds(self, *, at: datetime | None = None) -> float:
        now = at or datetime.now(timezone.utc)
        return (now - self.last_activity_at).total_seconds()

    def is_from_previous_local_day(
        self, tz_name: str, *, at: datetime | None = None
    ) -> bool:
        """場次開始日是否早於『現在』的本地日——跨日就該強制收掉再開新節。"""
        now = at or datetime.now(timezone.utc)
        return local_date_for(self.session_started_at, tz_name) < local_date_for(
            now, tz_name
        )

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
        self.numbered = False

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
            # 沒完成任何一圈的噪音過線不要寫進 archive，否則排行榜會出現
            # 「有場次、但 best_lap_time 全 0」的空殼節次。
            lap_count = int(state["lap_count"] or 0)
            best = float(state["best_lap_time"] or 0.0)
            if lap_count <= 0 and best <= 0.0:
                continue
            # all_states() 已是 broadcast dict 形式（見
            # LapTracker._to_broadcast_dict），欄位命名沿用同一份定義。
            point = (
                Point("session_archive")
                .tag("session_id", self.current_session_id)
                .tag("transponder_id", state["transponder_id"])
                .tag("car_number", state["car_number"])
                .field("registered", bool(state["registered"]))
                .field("lap_count", lap_count)
                .field("best_lap_time", best)
                .field("last_lap_time", float(state["last_lap_time"] or 0.0))
                .field("lap_history_json", json.dumps(state["lap_history"]))
                .field("reset_trigger", trigger)
                # 歸檔 _time 是結束時間；開始時間另外存，否則 list_sessions
                # 用 first()/last() 會得到同一個時間戳。
                .field("session_started_at", self.session_started_at.timestamp())
                .time(at)
            )
            points.append(point)
        return points

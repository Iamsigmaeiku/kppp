"""InfluxDB 讀取路徑：目前 decoder_ingest 只寫不讀（influx_writer.py），
這裡是第一個讀取路徑，供未來的 Web 層（場次瀏覽、leaderboard、AI 教練）
查詢歷史資料使用。查詢用的量測沿用 influx_writer.py 已經在寫的
decoder_raw_events（逐次通過）與 session_manager.py 新增的 session_archive
（每場次每支 transponder 一筆歸檔摘要）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from .config import InfluxConfig


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    started_at: datetime
    ended_at: datetime


@dataclass(slots=True)
class TransponderSessionResult:
    transponder_id: str
    car_number: str
    registered: bool
    lap_count: int
    best_lap_time: float
    last_lap_time: float
    lap_history: list[float] = field(default_factory=list)


@dataclass(slots=True)
class LapRecord:
    lap_number: int
    lap_time: float
    recorded_at: datetime


@dataclass(slots=True)
class AllTimeBestEntry:
    transponder_id: str
    car_number: str
    best_lap_time: float
    session_id: str


class InfluxReader:
    def __init__(
        self,
        config: InfluxConfig,
        *,
        measurement: str = "decoder_raw_events",
        archive_measurement: str = "session_archive",
    ) -> None:
        self._config = config
        self._measurement = measurement
        self._archive_measurement = archive_measurement
        # InfluxDBClientAsync 內部用到的 aiohttp session 要求建構當下有
        # running event loop，但這個物件本身常常在還沒有 loop 的地方就被
        # 建出來（例如 app.py 的 configure_app() 是同步函式）。延後到第一次
        # 真的查詢時才建立，之後同一個 process 內重複使用。
        self._client: InfluxDBClientAsync | None = None

    async def _get_client(self) -> InfluxDBClientAsync:
        if self._client is None:
            self._client = InfluxDBClientAsync(
                url=self._config.url, token=self._config.token, org=self._config.org
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _query(self, flux: str):
        client = await self._get_client()
        return await client.query_api().query(flux, org=self._config.org)

    async def list_sessions(self, *, range_start: str = "-30d") -> list[SessionSummary]:
        """依 session_archive 的第一/最後一筆時間推得每個場次的起訖時間，
        依開始時間新到舊排序。
        """
        base = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: {range_start}) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r._field == "lap_count") '
            f'|> group(columns: ["session_id"])'
        )
        first_tables = await self._query(base + " |> first()")
        last_tables = await self._query(base + " |> last()")

        started: dict[str, datetime] = {}
        for table in first_tables:
            for record in table.records:
                sid = record.values.get("session_id")
                if sid:
                    started[sid] = record.get_time()

        ended: dict[str, datetime] = {}
        for table in last_tables:
            for record in table.records:
                sid = record.values.get("session_id")
                if sid:
                    ended[sid] = record.get_time()

        sessions = [
            SessionSummary(
                session_id=sid,
                started_at=started_at,
                ended_at=ended.get(sid, started_at),
            )
            for sid, started_at in started.items()
        ]
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return sessions

    async def get_session_summary(self, session_id: str) -> list[TransponderSessionResult]:
        """單一場次、每支 transponder 一筆的歸檔摘要（來自 session_archive），
        依最佳圈速由小到大排序（尚無最佳圈速的排最後）。
        """
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: 0) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r.session_id == "{session_id}") '
            f'|> pivot(rowKey: ["_time", "transponder_id", "car_number"], '
            f'columnKey: ["_field"], valueColumn: "_value")'
        )
        tables = await self._query(query)

        results: list[TransponderSessionResult] = []
        for table in tables:
            for record in table.records:
                values = record.values
                try:
                    lap_history = json.loads(values.get("lap_history_json") or "[]")
                except (TypeError, ValueError):
                    lap_history = []
                results.append(
                    TransponderSessionResult(
                        transponder_id=values.get("transponder_id", ""),
                        car_number=values.get("car_number", ""),
                        registered=bool(values.get("registered", False)),
                        lap_count=int(values.get("lap_count", 0)),
                        best_lap_time=float(values.get("best_lap_time") or 0.0),
                        last_lap_time=float(values.get("last_lap_time") or 0.0),
                        lap_history=lap_history,
                    )
                )

        results.sort(key=lambda r: r.best_lap_time or float("inf"))
        return results

    async def get_lap_history(
        self, session_id: str, transponder_id: str
    ) -> list[LapRecord]:
        """單一場次、單一 transponder 的每一圈完整歷史（來自
        decoder_raw_events 的逐次 passing 紀錄，不受 LapTracker 記憶體內
        LAP_HISTORY_MAX 上限影響——歸檔後想看幾圈都看得到）。

        注意：transponder_id/car_number 在 decoder_raw_events 目前是
        field 而非 tag（見 influx_writer._event_to_point），所以要先
        pivot 成同一列再依欄位篩選，不能在 filter() 階段直接當 tag 用。
        """
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: 0) "
            f'|> filter(fn: (r) => r._measurement == "{self._measurement}" '
            f'and r.event_type == "passing" and r.session_id == "{session_id}") '
            f'|> filter(fn: (r) => r._field == "last_lap_time" '
            f'or r._field == "transponder_id") '
            f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
            f'|> filter(fn: (r) => r.transponder_id == "{transponder_id}") '
            f'|> sort(columns: ["_time"])'
        )
        tables = await self._query(query)

        records: list[LapRecord] = []
        for table in tables:
            for row in table.records:
                lap_time = row.values.get("last_lap_time")
                if lap_time is None:
                    continue
                # 第一次過線寫 last_lap_time=0.0，不算真實圈。
                if float(lap_time) <= 0.0:
                    continue
                records.append(
                    LapRecord(
                        lap_number=len(records) + 1,
                        lap_time=float(lap_time),
                        recorded_at=row.get_time(),
                    )
                )
        return records

    async def get_alltime_best(
        self, *, limit: int = 20, range_start: str = "-3650d"
    ) -> list[AllTimeBestEntry]:
        """依 transponder 分組取每人歷史最佳圈速（跨所有已歸檔場次），
        由快到慢排序、取前 N 筆——全站排行榜的資料來源。
        """
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: {range_start}) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r._field == "best_lap_time" and r._value > 0.0) '
            f'|> group(columns: ["transponder_id", "car_number"]) '
            f'|> min(column: "_value") '
            f'|> group() '
            f'|> sort(columns: ["_value"]) '
            f"|> limit(n: {limit})"
        )
        tables = await self._query(query)

        entries: list[AllTimeBestEntry] = []
        for table in tables:
            for record in table.records:
                entries.append(
                    AllTimeBestEntry(
                        transponder_id=record.values.get("transponder_id", ""),
                        car_number=record.values.get("car_number", ""),
                        best_lap_time=float(record.get_value()),
                        session_id=record.values.get("session_id", ""),
                    )
                )
        return entries

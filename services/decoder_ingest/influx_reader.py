"""InfluxDB 讀取路徑：目前 decoder_ingest 只寫不讀（influx_writer.py），
這裡是第一個讀取路徑，供未來的 Web 層（場次瀏覽、leaderboard、AI 教練）
查詢歷史資料使用。查詢用的量測沿用 influx_writer.py 已經在寫的
decoder_raw_events（逐次通過）與 session_manager.py 新增的 session_archive
（每場次每支 transponder 一筆歸檔摘要）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from .config import InfluxConfig


def started_at_from_session_id(session_id: str) -> datetime | None:
    """從 sess-YYYYMMDD-HHMMSS 還原場次開始時間（UTC）。

    session_archive 的 _time 是歸檔／結束時間；舊資料沒有
    session_started_at field 時靠這個 fallback。
    """
    if not session_id.startswith("sess-"):
        return None
    try:
        return datetime.strptime(session_id[5:], "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


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
class TrackPoint:
    lat: float
    lon: float
    recorded_at: datetime
    speed_mps: float | None = None


@dataclass(slots=True)
class LapTrack:
    lap_number: int
    lap_time: float
    recorded_at: datetime
    source: str
    points: list[TrackPoint] = field(default_factory=list)


@dataclass(slots=True)
class LapTelemetrySummary:
    """每圈的速度/G力彙總，供 AI 教練引用。任何欄位在沒有遙測重疊時都是
    None——呼叫端不可把 None 當 0 用，必須明確判斷「這圈沒有遙測資料」。
    """

    lap_number: int
    avg_speed_mps: float | None
    max_speed_mps: float | None
    max_lat_g: float | None
    max_brake_g: float | None
    brake_event_count: int | None


@dataclass(slots=True)
class AllTimeBestEntry:
    transponder_id: str
    car_number: str
    best_lap_time: float
    session_id: str


class InfluxReader:
    # 雙板 ESKF 寫 dr_position；走線圖查這台 device_id。
    _TRACK_DEVICE_ID = "esp32-kart-01"

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
        """結束時間＝session_archive 寫入時間；開始時間從 session_id 解析
        （sess-YYYYMMDD-HHMMSS）。同一場歸檔點時間相同，不能用 first/last 當起訖。
        """
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: {range_start}) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r._field == "lap_count") '
            f'|> group(columns: ["session_id"]) '
            f"|> first()"
        )
        tables = await self._query(query)

        sessions: list[SessionSummary] = []
        for table in tables:
            for record in table.records:
                sid = record.values.get("session_id")
                if not sid:
                    continue
                ended_at = record.get_time()
                started_at = started_at_from_session_id(sid) or ended_at
                sessions.append(
                    SessionSummary(
                        session_id=sid,
                        started_at=started_at,
                        ended_at=ended_at,
                    )
                )
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

        # 同一 session_id+transponder 若被重複歸檔（舊 bug / 手動重跑），
        # 只保留最新一筆，避免排行榜出現重複車號。
        latest_by_tid: dict[str, tuple[datetime, TransponderSessionResult]] = {}
        for table in tables:
            for record in table.records:
                values = record.values
                try:
                    lap_history = json.loads(values.get("lap_history_json") or "[]")
                except (TypeError, ValueError):
                    lap_history = []
                tid = values.get("transponder_id", "")
                row = TransponderSessionResult(
                    transponder_id=tid,
                    car_number=values.get("car_number", ""),
                    registered=bool(values.get("registered", False)),
                    lap_count=int(values.get("lap_count", 0)),
                    best_lap_time=float(values.get("best_lap_time") or 0.0),
                    last_lap_time=float(values.get("last_lap_time") or 0.0),
                    lap_history=lap_history,
                )
                recorded_at = record.get_time() or datetime.min.replace(
                    tzinfo=timezone.utc
                )
                prev = latest_by_tid.get(tid)
                if prev is None or recorded_at >= prev[0]:
                    latest_by_tid[tid] = (recorded_at, row)

        results = [row for _, row in latest_by_tid.values()]
        results.sort(key=lambda r: r.best_lap_time or float("inf"))
        return results

    async def get_lap_history(
        self, session_id: str, transponder_id: str
    ) -> list[LapRecord]:
        """單一場次、單一 transponder 的每一圈完整歷史。

        優先讀 decoder_raw_events；若因 UID 尾碼漂移（77/78）對不到、或
        舊資料缺 raw，再 fallback 到 session_archive.lap_history_json。
        """
        from .lap_tracker import normalize_transponder_id

        tid = transponder_id.upper().strip()
        canon = normalize_transponder_id(tid)
        # 查 raw 時同時接受 canonical 與現場漂移尾碼，避免歸檔用 77、raw 用 78
        # 導致 AI 教練/展開圈速全滅。
        tid_candidates = {tid, canon}
        if len(canon) >= 12 and canon[11] == "7":
            tid_candidates.add(canon[:11] + "6")
            tid_candidates.add(canon[:11] + "8")
        tid_filter = " or ".join(
            f'r.transponder_id == "{t}"' for t in sorted(tid_candidates)
        )

        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: 0) "
            f'|> filter(fn: (r) => r._measurement == "{self._measurement}" '
            f'and r.event_type == "passing" and r.session_id == "{session_id}") '
            f'|> filter(fn: (r) => r._field == "last_lap_time" '
            f'or r._field == "transponder_id") '
            f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
            f"|> filter(fn: (r) => {tid_filter}) "
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
        if records:
            return records

        # Fallback：session_archive 裡的 lap_history_json（手動歸檔 / UID
        # 正規化後只剩 canonical tid 的情況）。
        return await self._lap_history_from_archive(session_id, tid_candidates)

    async def _lap_history_from_archive(
        self, session_id: str, tid_candidates: set[str]
    ) -> list[LapRecord]:
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: 0) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r.session_id == "{session_id}") '
            f'|> filter(fn: (r) => r._field == "lap_history_json") '
            f'|> pivot(rowKey: ["_time", "transponder_id", "car_number"], '
            f'columnKey: ["_field"], valueColumn: "_value")'
        )
        tables = await self._query(query)
        best_hist: list[float] = []
        best_time = None
        for table in tables:
            for record in table.records:
                tid = (record.values.get("transponder_id") or "").upper()
                if tid not in tid_candidates:
                    # 也比對 normalize 後
                    from .lap_tracker import normalize_transponder_id

                    if normalize_transponder_id(tid) not in {
                        normalize_transponder_id(t) for t in tid_candidates
                    }:
                        continue
                try:
                    hist = json.loads(record.values.get("lap_history_json") or "[]")
                except (TypeError, ValueError):
                    hist = []
                if not isinstance(hist, list) or not hist:
                    continue
                recorded_at = record.get_time()
                if best_time is None or (
                    recorded_at is not None and recorded_at >= best_time
                ):
                    best_time = recorded_at
                    best_hist = [float(x) for x in hist if float(x) > 0]
        if not best_hist:
            return []
        base = best_time or datetime.now(timezone.utc)
        return [
            LapRecord(lap_number=i + 1, lap_time=t, recorded_at=base)
            for i, t in enumerate(best_hist)
        ]

    async def get_alltime_best(
        self, *, limit: int = 20, range_start: str = "-3650d"
    ) -> list[AllTimeBestEntry]:
        """依 transponder 分組取每人歷史最佳圈速（跨所有已歸檔場次），
        由快到慢排序、取前 N 筆——全站排行榜的資料來源。
        """
        import os

        min_lap = float(os.getenv("LEADERBOARD_MIN_LAP_SEC", "35.0"))
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: {range_start}) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r._field == "best_lap_time" and r._value >= {min_lap:.3f}) '
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

    async def get_lap_tracks(
        self, session_id: str, transponder_id: str
    ) -> list[LapTrack]:
        laps = await self.get_lap_history(session_id, transponder_id)
        if not laps:
            return []

        session_start = laps[0].recorded_at - timedelta(seconds=laps[0].lap_time + 2)
        session_end = laps[-1].recorded_at + timedelta(seconds=2)

        points, source = await self._query_track_points(
            device_id=self._TRACK_DEVICE_ID,
            start=session_start,
            stop=session_end,
        )
        if not points:
            return []

        out: list[LapTrack] = []
        lap_start = session_start
        idx = 0
        for lap in laps:
            seg_points: list[TrackPoint] = []
            while idx < len(points):
                point = points[idx]
                if point.recorded_at < lap_start:
                    idx += 1
                    continue
                if point.recorded_at > lap.recorded_at:
                    break
                seg_points.append(point)
                idx += 1

            out.append(
                LapTrack(
                    lap_number=lap.lap_number,
                    lap_time=lap.lap_time,
                    recorded_at=lap.recorded_at,
                    source=source,
                    points=seg_points,
                )
            )
            lap_start = lap.recorded_at
        return out

    async def _session_time_bounds(
        self, session_id: str
    ) -> tuple[datetime, datetime] | None:
        """場次起訖：started 從 session_id 解析；ended 從 session_archive。

        不依賴 get_lap_history——decoder 沒資料時 GPS 分圈也要能動。
        無 archive 時 ended = started + 3h。
        """
        started = started_at_from_session_id(session_id)
        if started is None:
            return None

        query = (
            f'from(bucket: "{self._config.bucket}") '
            f"|> range(start: 0) "
            f'|> filter(fn: (r) => r._measurement == "{self._archive_measurement}" '
            f'and r.session_id == "{session_id}" and r._field == "lap_count") '
            f"|> group() "
            f"|> last()"
        )
        ended: datetime | None = None
        try:
            tables = await self._query(query)
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    if ts is not None and (ended is None or ts > ended):
                        ended = ts
        except Exception:
            ended = None

        if ended is None:
            ended = started + timedelta(hours=3)
        # 略放寬尾端，避免最後幾秒 GPS 被 range stop 切掉
        return started, ended + timedelta(seconds=2)

    async def get_gps_lap_tracks(self, session_id: str) -> tuple[list, str]:
        """用虛擬起跑線切 GPS 軌跡成圈。回傳 (laps, source)。

        source 與 _query_track_points 一致（gps_track / kart_telemetry / dr_position）。
        """
        from services.webapp.track_coords import (
            GATE_FORWARD_BEARING_DEG,
            START_GATE_A_M,
            START_GATE_B_M,
        )

        from .gps_lap_splitter import GpsLap, split_laps_by_gate

        bounds = await self._session_time_bounds(session_id)
        if bounds is None:
            return [], "none"
        session_start, session_end = bounds

        points, source = await self._query_track_points(
            device_id=self._TRACK_DEVICE_ID,
            start=session_start,
            stop=session_end,
        )
        if not points:
            return [], source

        laps: list[GpsLap] = split_laps_by_gate(
            points,
            START_GATE_A_M,
            START_GATE_B_M,
            GATE_FORWARD_BEARING_DEG,
        )
        return laps, source

    # 跟 Grafana 那邊（scripts/_gen_esp_grafana_dashboards.py）同一套門檻，
    # 兩邊的「這算不算煞車/這是不是誇張的 G」判斷要一致，不要各自兜一套。
    _G_CLAMP = 2.0
    _BRAKE_THRESHOLD_G = 0.25

    async def get_lap_telemetry_summary(
        self, session_id: str, transponder_id: str
    ) -> list[LapTelemetrySummary]:
        """每圈的速度/G力彙總（供 AI 教練併入 prompt）。跟 get_lap_tracks()
        用同一組圈次時間切點；沒有遙測重疊的圈，各欄位就是 None，不會用 0
        頂替，避免 AI 把「沒資料」誤讀成「這圈很平穩」。
        """
        laps = await self.get_lap_history(session_id, transponder_id)
        if not laps:
            return []

        session_start = laps[0].recorded_at - timedelta(seconds=laps[0].lap_time + 2)
        session_end = laps[-1].recorded_at + timedelta(seconds=2)

        samples = await self._fetch_motion_samples(
            device_id=self._TRACK_DEVICE_ID, start=session_start, stop=session_end
        )

        out: list[LapTelemetrySummary] = []
        lap_start = session_start
        idx = 0
        for lap in laps:
            seg: list[dict] = []
            while idx < len(samples):
                sample = samples[idx]
                if sample["_time"] < lap_start:
                    idx += 1
                    continue
                if sample["_time"] > lap.recorded_at:
                    break
                seg.append(sample)
                idx += 1

            speeds = [s["speed"] for s in seg if s["speed"] is not None]
            lat_gs = [s["lat_g"] for s in seg if s["lat_g"] is not None]
            brake_gs = [s["brake_g"] for s in seg if s["brake_g"] is not None]

            out.append(
                LapTelemetrySummary(
                    lap_number=lap.lap_number,
                    avg_speed_mps=sum(speeds) / len(speeds) if speeds else None,
                    max_speed_mps=max(speeds) if speeds else None,
                    max_lat_g=max(lat_gs) if lat_gs else None,
                    max_brake_g=max(brake_gs) if brake_gs else None,
                    brake_event_count=(
                        sum(1 for g in brake_gs if g >= self._BRAKE_THRESHOLD_G)
                        if brake_gs
                        else None
                    ),
                )
            )
            lap_start = lap.recorded_at
        return out

    async def _fetch_motion_samples(
        self, *, device_id: str, start: datetime, stop: datetime
    ) -> list[dict]:
        """跟 flux_motion_derived()（Grafana 那邊）同一套 a_lat/a_lon
        fallback 邏輯：優先用 ESKF 算好的 a_lat/a_lon，沒有就退回原始
        ax/az、mpu_ax/mpu_az、gy85_ax/gy85_az。"""
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        stop_iso = stop.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f'|> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}")) '
            f'|> filter(fn: (r) => r._measurement == "kart_telemetry" and r.device_id == "{device_id}") '
            f'|> filter(fn: (r) => r._field == "gps_speed_mps" or r._field == "a_lat" or r._field == "a_lon"'
            f' or r._field == "ax" or r._field == "az"'
            f' or r._field == "mpu_ax" or r._field == "mpu_az"'
            f' or r._field == "gy85_ax" or r._field == "gy85_az") '
            f'|> aggregateWindow(every: 1s, fn: last, createEmpty: false) '
            f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
            f'|> sort(columns: ["_time"])'
        )
        tables = await self._query(query)

        samples: list[dict] = []
        for table in tables:
            for record in table.records:
                v = record.values
                ts = record.get_time()
                if ts is None:
                    continue

                a_lat = v.get("a_lat")
                if a_lat is None:
                    a_lat = v.get("az") if v.get("az") is not None else v.get("mpu_az")
                    if a_lat is None:
                        a_lat = v.get("gy85_az")
                a_lon = v.get("a_lon")
                if a_lon is None:
                    a_lon = v.get("ax") if v.get("ax") is not None else v.get("mpu_ax")
                    if a_lon is None:
                        a_lon = v.get("gy85_ax")

                lat_g = None
                brake_g = None
                if a_lat is not None and a_lon is not None:
                    a_lat = max(-self._G_CLAMP, min(self._G_CLAMP, float(a_lat)))
                    a_lon = max(-self._G_CLAMP, min(self._G_CLAMP, float(a_lon)))
                    lat_g = abs(a_lat)
                    brake_g = max(0.0, -a_lon)

                speed = v.get("gps_speed_mps")
                samples.append(
                    {
                        "_time": ts,
                        "speed": float(speed) if speed is not None else None,
                        "lat_g": lat_g,
                        "brake_g": brake_g,
                    }
                )
        return samples

    async def _resolve_car_number(self, session_id: str, transponder_id: str) -> str | None:
        from .lap_tracker import normalize_transponder_id

        want = normalize_transponder_id(transponder_id.upper().strip())
        for row in await self.get_session_summary(session_id):
            tid = normalize_transponder_id((row.transponder_id or "").upper().strip())
            if tid == want:
                car_number = (row.car_number or "").strip()
                return car_number or None
        return None

    async def _query_track_points(
        self,
        *,
        device_id: str,
        start: datetime,
        stop: datetime,
    ) -> tuple[list[TrackPoint], str]:
        # 雙 GPS 不混合：先 m10180c，無資料再 neo6m
        gps_track = await self._fetch_track_points(
            measurement="gps_track",
            field_map={"lat": "lat", "lon": "lon"},
            speed_field="speed",
            device_id=device_id,
            start=start,
            stop=stop,
            extra_tag_filter=' and r.device == "m10180c"',
        )
        if not gps_track:
            gps_track = await self._fetch_track_points(
                measurement="gps_track",
                field_map={"lat": "lat", "lon": "lon"},
                speed_field="speed",
                device_id=device_id,
                start=start,
                stop=stop,
                extra_tag_filter=' and r.device == "neo6m"',
            )
        if gps_track:
            return gps_track, "gps_track"

        # 原始 GPS 優先於 DR：實地比對（Grafana geomap）原始 GPS 走線緊貼
        # 賽道，而 dr_position 是 ESKF 航位推算，飄移時整段軌跡會偏出賽道，
        # 對「賽後看走線」來說原始 GPS 才是可信基準。只取 gps_fix=1 的樣本，
        # 避免失鎖期間殘留座標混進來。
        raw_gps = await self._fetch_track_points(
            measurement="kart_telemetry",
            field_map={"gps_lat": "lat", "gps_lon": "lon"},
            speed_field="gps_speed_mps",
            device_id=device_id,
            start=start,
            stop=stop,
            extra_tag_filter=' and r.gps_fix == "1"',
        )
        if raw_gps:
            return raw_gps, "kart_telemetry"

        dr_track = await self._fetch_track_points(
            measurement="dr_position",
            field_map={"lat_dr": "lat", "lon_dr": "lon"},
            speed_field="speed_mps",
            device_id=device_id,
            start=start,
            stop=stop,
        )
        return dr_track, "dr_position"

    async def _fetch_track_points(
        self,
        *,
        measurement: str,
        field_map: dict[str, str],
        device_id: str,
        start: datetime,
        stop: datetime,
        speed_field: str | None = None,
        extra_tag_filter: str = "",
    ) -> list[TrackPoint]:
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        stop_iso = stop.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        fields = list(field_map.keys())
        if speed_field:
            fields.append(speed_field)
        field_filters = " or ".join(f'r._field == "{field}"' for field in fields)
        keep_columns = ", ".join(f'"{field}"' for field in fields)
        # group() 是關鍵：同一量測會因 tag 組合不同（car_id 有時有有時沒有、
        # gps_fix 有無）被拆成多個 series，pivot/sort 只在各自 series 內排序，
        # Python 端逐 table 收點就會把不同時間段交錯接在一起，畫出橫跨全圖
        # 的鋸齒直線。先 group() 打平再全域排序。
        query = (
            f'from(bucket: "{self._config.bucket}") '
            f'|> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}")) '
            f'|> filter(fn: (r) => r._measurement == "{measurement}" and r.device_id == "{device_id}"{extra_tag_filter}) '
            f"|> filter(fn: (r) => {field_filters}) "
            f'|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
            f'|> group() '
            f'|> keep(columns: ["_time", {keep_columns}]) '
            f'|> sort(columns: ["_time"])'
        )
        tables = await self._query(query)

        points: list[TrackPoint] = []
        for table in tables:
            for record in table.records:
                values = record.values
                lat_key, lon_key = list(field_map.keys())
                lat = values.get(lat_key)
                lon = values.get(lon_key)
                ts = record.get_time()
                if lat is None or lon is None or ts is None:
                    continue
                speed_mps: float | None = None
                if speed_field:
                    raw_speed = values.get(speed_field)
                    if raw_speed is not None:
                        speed_mps = float(raw_speed)
                points.append(
                    TrackPoint(
                        lat=float(lat),
                        lon=float(lon),
                        recorded_at=ts,
                        speed_mps=speed_mps,
                    )
                )
        # 保險再排一次：無論 Flux 端 table 怎麼拆，回到 Python 一律以時間排序。
        points.sort(key=lambda p: p.recorded_at)
        return points

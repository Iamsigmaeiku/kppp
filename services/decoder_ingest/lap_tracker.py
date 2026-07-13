"""Per-transponder 圈速狀態追蹤（記憶體，非持久化）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .packet_parser import PassingEvent

logger = logging.getLogger(__name__)

NOISE_THRESHOLD_SEC = 10.0
TIMER_TIMEOUT_SEC = 120.0
MAX_LAP_TIME_SEC = 600.0
LAP_HISTORY_MAX = 20
DEFAULT_CAR_NUMBER_MAP: dict[str, str] = {
    # 2026-07-12 現場晶片（UID 尾碼會在 6/7/8 漂移，見 normalize_transponder_id）
    "14021124C877": "11",
    "140215359577": "12",
    "140210B98377": "13",
    "148210E3C477": "14",
    "140210E3C477": "14",
    "140201B81B77": "15",
    "140210D7E877": "16",
    "140211084277": "17",
    "140211241C77": "18",
    "140215494F77": "19",
    "140210998E77": "20",
}


def normalize_transponder_id(transponder_id: str) -> str:
    """同一實體晶片最後一個 hex nibble 會在 6/7/8 間漂（現場已見 76/77、77/78
    成對出現）。只把這三個不穩定尾碼收成 canonical ``7``，其餘尾碼不動。
    """
    tid = transponder_id.upper().strip()
    if len(tid) >= 12 and all(c in "0123456789ABCDEF" for c in tid[:12]):
        if tid[11] in "678":
            return tid[:11] + "7"
        return tid[:12]
    return tid


@dataclass
class TransponderState:
    lap_count: int = 0
    last_passing_at: datetime | None = None
    last_lap_time: float | None = None
    best_lap_time: float | None = None
    lap_history: list[float] = field(default_factory=list)
    last_raw_payload: str | None = None
    frozen_elapsed: float | None = None
    last_passing_tick: int | None = None

    def clear_timer_freeze(self) -> None:
        self.frozen_elapsed = None


class LapTracker:
    def __init__(
        self,
        *,
        noise_threshold_sec: float = NOISE_THRESHOLD_SEC,
        timer_timeout_sec: float = TIMER_TIMEOUT_SEC,
        max_lap_time_sec: float = MAX_LAP_TIME_SEC,
        history_max: int = LAP_HISTORY_MAX,
        car_number_map: dict[str, str] | None = None,
        decoder_ids: Sequence[str] = (),
        decoder_tick_hz: float | None = None,
    ) -> None:
        self._noise_threshold = noise_threshold_sec
        self._timer_timeout = timer_timeout_sec
        self._max_lap_time = max_lap_time_sec
        self._history_max = history_max
        # None（預設）代表功能關閉，圈速仍完全依 received_at 計算，行為與
        # 加入 decoder tick 支援前完全一致。設定後才會改用 decoder 自帶的
        # tick 欄位計算圈速（見 record_passing）。
        self._decoder_tick_hz = decoder_tick_hz
        self._car_number_map = {
            normalize_transponder_id(tid): car
            for tid, car in (car_number_map or DEFAULT_CAR_NUMBER_MAP).items()
        }
        self._states: dict[str, TransponderState] = {}
        # 多台 decoder 同時服務同一計時點（硬體備援）時，只要還有至少一台
        # 連線就不該凍結計時；只有全部斷線才凍結。_known_decoder_ids 用來
        # 回報「共幾台」，即使一開始都還沒連上也能顯示 0/N 而非未知。
        self._known_decoder_ids: set[str] = set(decoder_ids)
        self._connected_decoder_ids: set[str] = set()

    @property
    def decoder_connected(self) -> bool:
        return len(self._connected_decoder_ids) > 0

    def is_registered(self, transponder_id: str) -> bool:
        return normalize_transponder_id(transponder_id) in self._car_number_map

    def car_number_for(self, transponder_id: str) -> str:
        return self._car_number_map[normalize_transponder_id(transponder_id)]

    def transponder_id_for_car(self, car_number: str) -> str | None:
        """反查車號對應的 transponder_id（供使用者用車號綁定，而非直接貼
        UID）。只找已在 car_number_map 註冊的車號；車號不存在時回傳 None。
        """
        for tid, car in self._car_number_map.items():
            if car == car_number:
                return tid
        return None

    def set_decoder_connected(self, decoder_id: str, connected: bool) -> None:
        self._known_decoder_ids.add(decoder_id)
        was_connected = self.decoder_connected
        if connected:
            self._connected_decoder_ids.add(decoder_id)
        else:
            self._connected_decoder_ids.discard(decoder_id)
        is_connected = self.decoder_connected

        if was_connected == is_connected:
            # 聚合連線狀態沒有改變（例如備援中的其中一台斷線，但其他台還
            # 連著）：不要凍結/解凍計時器，避免備援 decoder 抖動造成計時器
            # 一直凍結又解凍。
            return

        if not is_connected:
            self._freeze_all_timers()
        else:
            # 聚合狀態剛從「全部斷線」變成「至少一台連線」：解除因斷線造成
            # 的凍結，讓計時從 last_passing_at 恢復即時累計。真的閒置太久
            # 的車輛會被 _timer_snapshot() 的 timer_timeout 邏輯重新凍結。
            self._unfreeze_all_timers()

    def all_timers_inactive(self) -> bool:
        """所有已過線車輛的本圈計時都已凍結（逾時或 decoder 斷線）。
        空場次回 False。供 auto-archive：全車暫停代表這一節實質結束。
        """
        if not self._states:
            return False
        now = datetime.now(timezone.utc)
        for state in self._states.values():
            if state.last_passing_at is None:
                continue
            _, timer_active = self._timer_snapshot(state, at=now)
            if timer_active:
                return False
        # 至少要有一台車真的過過線，否則「全停」沒意義
        return any(s.last_passing_at is not None for s in self._states.values())

    def has_archivable_results(self) -> bool:
        """是否有值得寫進 session_archive 的圈速（與 archive 過濾條件對齊）。"""
        for state in self._states.values():
            if state.lap_count > 0 or (
                state.best_lap_time is not None and state.best_lap_time > 0
            ):
                return True
        return False

    def _freeze_all_timers(self, *, at: datetime | None = None) -> None:
        now = at or datetime.now(timezone.utc)
        for state in self._states.values():
            if state.last_passing_at is None or state.frozen_elapsed is not None:
                continue
            state.frozen_elapsed = (
                now - state.last_passing_at
            ).total_seconds()

    def _unfreeze_all_timers(self) -> None:
        for state in self._states.values():
            state.clear_timer_freeze()

    def reset_session(self) -> None:
        """清空所有 transponder 的賽事資料（lap_count/best_lap_time/
        lap_history/last_passing_at/frozen_elapsed），供開賽前手動重置。
        不影響 decoder 連線狀態，也不影響 car_number_map 註冊設定。
        """
        self._states.clear()

    def finalize_in_progress_laps(self, *, at: datetime | None = None) -> None:
        """場次結束（archive_and_reset）前呼叫：每台車最後一次過線之後，
        因為沒有「下一次過線」讓 record_passing() 把它算成正式一圈，
        本圈計時器只會一直跑到 timer_timeout_sec 才凍結，凍結後的數字
        從來不會被計入 lap_count/lap_history——這一段直接視為這一節
        真正的最後一圈，補計入 lap_count/last_lap_time/best_lap_time/
        lap_history，讓歸檔到 InfluxDB 的資料完整反映車手實際跑的圈數。

        沿用 record_passing() 既有的合理性門檻：短於 noise_threshold_sec
        代表車手才剛觸發、根本還沒真的跑完一圈，不該算數；長於
        max_lap_time_sec 代表車手早就離場、這段時間本身沒有意義，
        兩種狀況都跳過、保留現有狀態不變。
        """
        now = at or datetime.now(timezone.utc)
        for state in self._states.values():
            if state.last_passing_at is None:
                continue
            elapsed, _ = self._timer_snapshot(state, at=now)
            if elapsed is None:
                continue
            if elapsed < self._noise_threshold or elapsed > self._max_lap_time:
                continue

            state.lap_count += 1
            state.last_lap_time = elapsed
            if state.best_lap_time is None or elapsed < state.best_lap_time:
                state.best_lap_time = elapsed
            state.lap_history.append(elapsed)
            if len(state.lap_history) > self._history_max:
                state.lap_history = state.lap_history[-self._history_max :]
            state.frozen_elapsed = elapsed

    def _timer_snapshot(
        self, state: TransponderState, *, at: datetime | None = None
    ) -> tuple[float | None, bool]:
        if state.last_passing_at is None:
            return None, False

        if state.frozen_elapsed is not None:
            return state.frozen_elapsed, False

        if not self.decoder_connected:
            return state.frozen_elapsed, False

        now = at or datetime.now(timezone.utc)
        elapsed = (now - state.last_passing_at).total_seconds()
        if elapsed > self._timer_timeout:
            state.frozen_elapsed = elapsed
            return elapsed, False

        return elapsed, True

    @staticmethod
    def _numeric_sort_key(value: str) -> tuple[int, int, str]:
        """數字車號/ID 依數值排序（"11" 排在 "2" 之後），非數字內容 fallback
        為字串排序並排在所有數字之後。car_number/transponder_id 在同一個
        tracker 內皆為唯一值，因此這個 tuple 本身就是穩定、確定性的最終
        tie-break key，不需要再額外附加第四層 key。
        """
        if value.isdigit():
            return (0, int(value), "")
        return (1, 0, value)

    def _sort_key(
        self, transponder_id: str, state: TransponderState
    ) -> tuple[int, float, tuple[int, int, str]]:
        """排行榜排序鍵：

        - group 0：已註冊 + 有 best_lap_time -> 依 best_lap_time 由小到大。
        - group 1：已註冊 + 尚無 best_lap_time -> 依 car_number 排序（數值優先）。
        - group 2：未註冊 + 有 best_lap_time -> 依 best_lap_time 由小到大。
        - group 3：未註冊 + 尚無 best_lap_time -> 依 transponder_id 排序。

        未註冊一律排在已註冊之後（group 2/3 > group 0/1）。
        """
        registered = self.is_registered(transponder_id)
        has_best = state.best_lap_time is not None
        if registered and has_best:
            group = 0
        elif registered:
            group = 1
        elif has_best:
            group = 2
        else:
            group = 3
        secondary = state.best_lap_time if has_best else 0.0
        tertiary_raw = (
            self.car_number_for(transponder_id) if registered else transponder_id
        )
        tertiary = self._numeric_sort_key(tertiary_raw)
        return (group, secondary, tertiary)

    def _sorted_transponder_ids(self) -> list[str]:
        return sorted(
            self._states.keys(),
            key=lambda tid: self._sort_key(tid, self._states[tid]),
        )

    def _rank_for(self, transponder_id: str) -> int | None:
        if transponder_id not in self._states:
            return None
        return self._sorted_transponder_ids().index(transponder_id) + 1

    def _to_broadcast_dict(
        self,
        transponder_id: str,
        state: TransponderState,
        *,
        raw_payload: str | None = None,
        at: datetime | None = None,
        rank: int | None = None,
    ) -> dict:
        registered = self.is_registered(transponder_id)
        car_number = (
            self.car_number_for(transponder_id)
            if registered
            else f"?{transponder_id}"
        )
        current_lap_elapsed, timer_active = self._timer_snapshot(state, at=at)
        return {
            "type": "lap",
            "transponder_id": transponder_id,
            "car_number": car_number,
            "registered": registered,
            "decoder_connected": self.decoder_connected,
            "rank": rank if rank is not None else self._rank_for(transponder_id),
            "lap_count": state.lap_count,
            "last_passing_at": (
                state.last_passing_at.isoformat() if state.last_passing_at else None
            ),
            "last_lap_time": state.last_lap_time,
            "best_lap_time": state.best_lap_time,
            "lap_history": list(state.lap_history),
            "raw_payload": raw_payload or state.last_raw_payload,
            "current_lap_elapsed": current_lap_elapsed,
            "timer_active": timer_active,
        }

    def record_passing(self, event: PassingEvent) -> dict:
        # 一律用 canonical UID 當 state key，否則同一台車 77/78 會變成兩筆。
        transponder_id = normalize_transponder_id(event.transponder_id)
        state = self._states.setdefault(transponder_id, TransponderState())
        state.last_raw_payload = event.raw_payload
        state.clear_timer_freeze()

        if state.last_passing_at is not None:
            lap_time = self._compute_lap_time(transponder_id, event, state)
            if lap_time < self._noise_threshold:
                # 同一次通過的雙觸發雜訊；同時也是多 decoder 涵蓋同一計時點時
                # 天然的跨 decoder 去重機制（見多 decoder 架構設計）。
                return self._to_broadcast_dict(
                    transponder_id,
                    state,
                    raw_payload=event.raw_payload,
                    at=event.received_at,
                )

            if lap_time > self._max_lap_time:
                # 圈速過大（例如 decoder 斷線數小時後才收到下一次通過），
                # 這段間隔不具參考價值：不算圈、不更新 best/history，
                # 但這次通過本身是真實事件，仍把它當成下一圈的新起點。
                logger.warning(
                    "lap_time %.1fs exceeds max_lap_time_sec=%.1fs, "
                    "resetting baseline for transponder=%s without counting a lap",
                    lap_time,
                    self._max_lap_time,
                    transponder_id,
                )
                state.last_passing_at = event.received_at
                state.last_passing_tick = event.decoder_tick
                return self._to_broadcast_dict(
                    transponder_id,
                    state,
                    raw_payload=event.raw_payload,
                    at=event.received_at,
                )

            state.lap_count += 1
            state.last_lap_time = lap_time
            if state.best_lap_time is None or lap_time < state.best_lap_time:
                state.best_lap_time = lap_time
            state.lap_history.append(lap_time)
            if len(state.lap_history) > self._history_max:
                state.lap_history = state.lap_history[-self._history_max :]
        else:
            # 第一次偵測到這支 transponder：只確立計時起點，尚未算出任何一圈。
            state.lap_count = 0

        state.last_passing_at = event.received_at
        state.last_passing_tick = event.decoder_tick
        return self._to_broadcast_dict(
            transponder_id,
            state,
            raw_payload=event.raw_payload,
            at=event.received_at,
        )

    def _compute_lap_time(
        self, transponder_id: str, event: PassingEvent, state: TransponderState
    ) -> float:
        """優先使用 decoder 自帶的 tick 欄位計算圈速（需
        DECODER_TICK_HZ 已設定、且這次和上次通過都有解出 tick 值）；
        否則 fallback 為現行的 received_at 到達時間差。兩者都可用時，
        算出的圈速若相差超過 0.5 秒會記警告，方便在正式啟用前發現
        byte offset/長度猜錯的狀況。
        """
        fallback_lap_time = (
            event.received_at - state.last_passing_at
        ).total_seconds()

        if (
            self._decoder_tick_hz is None
            or event.decoder_tick is None
            or state.last_passing_tick is None
            or event.tick_byte_len is None
        ):
            return fallback_lap_time

        modulus = 1 << (8 * event.tick_byte_len)
        tick_delta = (event.decoder_tick - state.last_passing_tick) % modulus
        tick_lap_time = tick_delta / self._decoder_tick_hz

        if abs(tick_lap_time - fallback_lap_time) > 0.5:
            implied_hz = (
                tick_delta / fallback_lap_time if fallback_lap_time > 0 else None
            )
            logger.warning(
                "lap_time mismatch for %s: tick-based=%.3fs received_at-based=%.3fs "
                "tick_delta=%s decoder_tick_hz=%s implied_hz_if_wallclock=%.3f "
                "(using tick-based; ticks are ASCII hex / 256000 — do NOT set "
                "DECODER_TICK_HZ≈14250 from decimal misread of Wireshark)",
                transponder_id,
                tick_lap_time,
                fallback_lap_time,
                tick_delta,
                self._decoder_tick_hz,
                implied_hz if implied_hz is not None else float("nan"),
            )

        return tick_lap_time

    def all_states(self) -> list[dict]:
        ordered_ids = self._sorted_transponder_ids()
        return [
            self._to_broadcast_dict(tid, self._states[tid], rank=idx + 1)
            for idx, tid in enumerate(ordered_ids)
        ]

    def decoder_status_message(self) -> dict:
        return {
            "type": "decoder_status",
            "connected": self.decoder_connected,
            "connected_count": len(self._connected_decoder_ids),
            "total_count": len(self._known_decoder_ids),
            "decoders": {
                did: (did in self._connected_decoder_ids)
                for did in sorted(self._known_decoder_ids)
            },
        }

    def to_snapshot_dict(self) -> dict:
        """序列化目前所有 transponder 狀態，供崩潰復原用的本地快照。
        不含 decoder 連線狀態（連線狀態應由當下實際連線情形決定，
        而非沿用快照當時的舊值）。
        """
        return {
            "states": {
                tid: {
                    "lap_count": state.lap_count,
                    "last_passing_at": (
                        state.last_passing_at.isoformat()
                        if state.last_passing_at
                        else None
                    ),
                    "last_lap_time": state.last_lap_time,
                    "best_lap_time": state.best_lap_time,
                    "lap_history": list(state.lap_history),
                    "last_raw_payload": state.last_raw_payload,
                    "last_passing_tick": state.last_passing_tick,
                }
                for tid, state in self._states.items()
            }
        }

    def load_snapshot(self, data: dict) -> None:
        """從 to_snapshot_dict() 產生的資料復原狀態；任何解析失敗的單筆
        紀錄會被跳過而不中斷整體載入。UID 會正規化，77/78 漂移合併。
        """
        states = data.get("states", {})
        for tid, raw in states.items():
            try:
                last_passing_at = (
                    datetime.fromisoformat(raw["last_passing_at"])
                    if raw.get("last_passing_at")
                    else None
                )
                canon = normalize_transponder_id(tid)
                incoming = TransponderState(
                    lap_count=raw.get("lap_count", 0),
                    last_passing_at=last_passing_at,
                    last_lap_time=raw.get("last_lap_time"),
                    best_lap_time=raw.get("best_lap_time"),
                    lap_history=list(raw.get("lap_history", [])),
                    last_raw_payload=raw.get("last_raw_payload"),
                    last_passing_tick=raw.get("last_passing_tick"),
                )
                existing = self._states.get(canon)
                if existing is None:
                    self._states[canon] = incoming
                    continue
                # 同一 canonical UID 若 snapshot 裡同時有 77/78 兩筆，合併成
                # 圈數較多 / 最佳圈較快的那份，避免復原後資料被較差的蓋掉。
                if incoming.lap_count > existing.lap_count or (
                    incoming.best_lap_time is not None
                    and (
                        existing.best_lap_time is None
                        or incoming.best_lap_time < existing.best_lap_time
                    )
                ):
                    self._states[canon] = incoming
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed snapshot entry for %s", tid)

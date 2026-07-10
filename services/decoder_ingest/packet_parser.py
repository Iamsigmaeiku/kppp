"""TCP stream framing + 可擴充 parser rules；heartbeat 過濾、未知封包收集。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

TRANSPONDER_ID_HEX_LEN = 12


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """已辨識、可寫 Influx 的事件（格式確認後由 rule 填充 fields）。"""

    timestamp: datetime
    event_type: str
    raw: bytes
    fields: dict[str, str | int | float | bool]


@dataclass(frozen=True, slots=True)
class UnknownPacket:
    timestamp: datetime
    raw: bytes


@dataclass(frozen=True, slots=True)
class DecodedTail:
    """passing payload 去掉 transponder id 後剩餘 hex 的解碼結果。

    hit_counter 是第一個 byte（依樣本分析像是每次通過遞增的計數器，
    見 lap_tracker 對照分析）；tick_raw 是依可設定的 byte_offset/byte_len
    取出的候選高精度時間戳/tick 值，尚未換算成秒（真正的 tick 頻率需靠
    診斷校正流程確認，見 DECODER_TICK_HZ）。任何一個欄位都可能是 None，
    表示剩餘 hex 不夠長、無法取出該欄位。
    """

    hit_counter: int | None
    tick_raw: int | None


def decode_trailing_hex(
    trailing_hex: str,
    *,
    tick_byte_offset: int,
    tick_byte_len: int,
) -> DecodedTail | None:
    """解碼 transponder id 之後剩餘的 hex payload。不確定/長度不足時回傳
    None 或部分欄位為 None，絕不拋例外——這段資料的實際格式在校正完成前
    仍是猜測，任何解碼失敗都不該讓 passing 事件整個處理失敗。
    """
    if not trailing_hex:
        return None
    try:
        tail = bytes.fromhex(trailing_hex)
    except ValueError:
        return None
    if not tail:
        return None

    hit_counter = tail[0]
    tick_raw: int | None = None
    end = tick_byte_offset + tick_byte_len
    if tick_byte_offset >= 0 and len(tail) >= end:
        tick_raw = int.from_bytes(tail[tick_byte_offset:end], "big")

    return DecodedTail(hit_counter=hit_counter, tick_raw=tick_raw)


@dataclass(frozen=True, slots=True)
class PassingEvent:
    transponder_id: str
    raw_payload: str
    received_at: datetime
    decoder_tick: int | None = None
    tick_byte_len: int | None = None
    hit_counter: int | None = None


@dataclass(slots=True)
class FeedResult:
    events: list[ParsedEvent] = field(default_factory=list)
    passings: list[PassingEvent] = field(default_factory=list)
    unknowns: list[UnknownPacket] = field(default_factory=list)
    heartbeat_seen: bool = False


class ParserRule(ABC):
    """可擴充規則介面：未來加 lap passing rule 只需新增 subclass。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def matches(self, frame: bytes) -> bool: ...

    @abstractmethod
    def parse(self, frame: bytes, *, received_at: datetime) -> ParsedEvent | None: ...


class HeartbeatRule(ParserRule):
    """匹配 decoder keepalive：#[0-9]{3,4}[DE]?\\r\\n，不產生 ParsedEvent。

    現場 decoder 常見兩種格式：`#1440D\\r\\n` 與 `#1440\\r\\n`（無 D/E 尾碼）。
    後者若未過濾會被當 unknown 灌進即時封包面板，看起來像有資料但其實不是過線。
    """

    PATTERN = re.compile(rb"^#[0-9]{3,4}(?:[DE])?\r\n$")

    @property
    def name(self) -> str:
        return "heartbeat"

    def matches(self, frame: bytes) -> bool:
        return bool(self.PATTERN.match(frame))

    def parse(self, frame: bytes, *, received_at: datetime) -> ParsedEvent | None:
        return None


class PassingRule(ParserRule):
    """匹配 $[hex]\\r\\n passing event，產生 PassingEvent（不寫 Influx）。"""

    PASSING_PATTERN = re.compile(rb"\$([0-9A-Fa-f]+)\r\n")

    def __init__(
        self,
        *,
        transponder_id_len: int = TRANSPONDER_ID_HEX_LEN,
        tick_byte_offset: int = 1,
        tick_byte_len: int = 3,
    ) -> None:
        self._transponder_id_len = transponder_id_len
        self._tick_byte_offset = tick_byte_offset
        self._tick_byte_len = tick_byte_len

    @property
    def name(self) -> str:
        return "passing"

    def matches(self, frame: bytes) -> bool:
        m = self.PASSING_PATTERN.match(frame)
        if not m:
            return False
        return len(m.group(1)) >= self._transponder_id_len

    def parse(self, frame: bytes, *, received_at: datetime) -> ParsedEvent | None:
        return None

    def to_passing_event(self, frame: bytes, *, received_at: datetime) -> PassingEvent:
        m = self.PASSING_PATTERN.match(frame)
        assert m is not None
        raw_hex = m.group(1).decode("ascii").upper()
        trailing_hex = raw_hex[self._transponder_id_len :]
        tail = decode_trailing_hex(
            trailing_hex,
            tick_byte_offset=self._tick_byte_offset,
            tick_byte_len=self._tick_byte_len,
        )
        return PassingEvent(
            transponder_id=raw_hex[: self._transponder_id_len],
            raw_payload=raw_hex,
            received_at=received_at,
            decoder_tick=tail.tick_raw if tail else None,
            tick_byte_len=self._tick_byte_len if (tail and tail.tick_raw is not None) else None,
            hit_counter=tail.hit_counter if tail else None,
        )


class PacketParser:
    def __init__(self, rules: Sequence[ParserRule] | None = None) -> None:
        self._rules: list[ParserRule] = list(rules) if rules is not None else []
        self._heartbeat_rule = HeartbeatRule()
        self._buffer = bytearray()
        self._last_heartbeat_time: datetime | None = None

    @property
    def last_heartbeat_time(self) -> datetime | None:
        return self._last_heartbeat_time

    def feed(self, chunk: bytes, *, received_at: datetime | None = None) -> FeedResult:
        """累積 stream buffer，切出完整 frame 後分派給 rules。"""
        if not chunk:
            return FeedResult()

        self._buffer.extend(chunk)
        received = received_at or datetime.now(timezone.utc)
        result = FeedResult()

        for frame in self._extract_frames():
            frame_result = self._dispatch(frame, received)
            result.events.extend(frame_result.events)
            result.passings.extend(frame_result.passings)
            result.unknowns.extend(frame_result.unknowns)
            result.heartbeat_seen = result.heartbeat_seen or frame_result.heartbeat_seen

        return result

    def feed_frame(self, frame: bytes, *, received_at: datetime | None = None) -> FeedResult:
        """replay 模式直接餵單一 frame（跳過 stream buffer）。"""
        received = received_at or datetime.now(timezone.utc)
        return self._dispatch(frame, received)

    def _extract_frames(self) -> list[bytes]:
        frames: list[bytes] = []
        while True:
            sep = self._buffer.find(b"\r\n")
            if sep < 0:
                break
            frame = bytes(self._buffer[: sep + 2])
            del self._buffer[: sep + 2]
            frames.append(frame)
        return frames

    def _dispatch(self, frame: bytes, received_at: datetime) -> FeedResult:
        result = FeedResult()

        if self._heartbeat_rule.matches(frame):
            self._last_heartbeat_time = received_at
            result.heartbeat_seen = True
            return result

        for rule in self._rules:
            if rule.matches(frame):
                if isinstance(rule, PassingRule):
                    result.passings.append(
                        rule.to_passing_event(frame, received_at=received_at)
                    )
                else:
                    parsed = rule.parse(frame, received_at=received_at)
                    if parsed is not None:
                        result.events.append(parsed)
                return result

        result.unknowns.append(UnknownPacket(timestamp=received_at, raw=frame))
        return result


def bytes_to_printable_ascii(data: bytes) -> str:
    """可列印 ASCII 原樣輸出，其餘以 '.' 代替。"""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def format_raw_log_line(packet: UnknownPacket) -> str:
    """輸出: {iso} | {hex} | {ascii_if_printable}"""
    ts = packet.timestamp.astimezone(timezone.utc).isoformat()
    hex_str = packet.raw.hex()
    ascii_str = bytes_to_printable_ascii(packet.raw)
    return f"{ts} | {hex_str} | {ascii_str}"


def format_passing_calibration_line(passing: PassingEvent) -> str:
    """校正用途：不論是否已註冊，逐筆記錄 passing 的到達時間與完整 payload，
    供離線比對 tick 欄位與碼表/decoder 螢幕時間，找出真正的 tick 編碼。
    輸出: {iso} | {raw_payload} | tid={id} tick={tick} hit={hit_counter}
    """
    ts = passing.received_at.astimezone(timezone.utc).isoformat()
    return (
        f"{ts} | {passing.raw_payload} | "
        f"tid={passing.transponder_id} tick={passing.decoder_tick} "
        f"hit={passing.hit_counter}"
    )

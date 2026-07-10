"""TCP stream framing + 可擴充 parser rules；heartbeat 過濾、未知封包收集。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

TRANSPONDER_ID_HEX_LEN = 12
# AmbRC/MyLaps 固定欄位：$[tid 12][ticks 8][強度 4]\r\n
DECODER_TICK_HEX_LEN = 8
DECODER_STRENGTH_HEX_LEN = 4
DECODER_TICK_BYTE_LEN = 4  # 8 hex chars → 32-bit；wrap modulus = 2^32
TRAILING_HEX_MIN_LEN = DECODER_TICK_HEX_LEN + DECODER_STRENGTH_HEX_LEN


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

    固定 ASCII hex 欄位：前 8 碼 = decoder ticks（32-bit），後 4 碼 = 強度/命中。
    hit_counter 欄位名保留相容，語意為強度值。任一欄位為 None 表示 trailing
    長度不足或非合法 hex。
    """

    hit_counter: int | None
    tick_raw: int | None


def decode_trailing_hex(trailing_hex: str) -> DecodedTail | None:
    """解碼 transponder id 之後的固定欄位 trailing hex。

    格式：`[Decoder ticks 8碼][強度/命中 4碼]`。長度不足或非法 hex 時回傳
    None / 部分欄位 None，絕不拋例外——解碼失敗不該讓 passing 事件整筆失敗。
    """
    if not trailing_hex:
        return None
    if len(trailing_hex) < TRAILING_HEX_MIN_LEN:
        return DecodedTail(hit_counter=None, tick_raw=None)
    try:
        tick_raw = int(trailing_hex[:DECODER_TICK_HEX_LEN], 16)
        hit_counter = int(
            trailing_hex[
                DECODER_TICK_HEX_LEN : DECODER_TICK_HEX_LEN + DECODER_STRENGTH_HEX_LEN
            ],
            16,
        )
    except ValueError:
        return None
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
    """匹配 $[hex]\\r\\n passing event，產生 PassingEvent（不寫 Influx）。

    完整封包：`$[tid 12][ticks 8][強度 4]\\r\\n`（共 27 bytes）。
    """

    PASSING_PATTERN = re.compile(rb"\$([0-9A-Fa-f]+)\r\n")

    def __init__(
        self,
        *,
        transponder_id_len: int = TRANSPONDER_ID_HEX_LEN,
    ) -> None:
        self._transponder_id_len = transponder_id_len

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
        tail = decode_trailing_hex(trailing_hex)
        has_tick = tail is not None and tail.tick_raw is not None
        return PassingEvent(
            transponder_id=raw_hex[: self._transponder_id_len],
            raw_payload=raw_hex,
            received_at=received_at,
            decoder_tick=tail.tick_raw if has_tick else None,
            tick_byte_len=DECODER_TICK_BYTE_LEN if has_tick else None,
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

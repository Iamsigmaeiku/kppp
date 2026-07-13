"""packet_parser.py 固定欄位 tick 解碼測試：12+8+4 ASCII hex。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.decoder_ingest.packet_parser import (
    DECODER_TICK_BYTE_LEN,
    PassingRule,
    decode_trailing_hex,
)


def test_decode_trailing_hex_wireshark_sample():
    # 截圖圖1：$140201B81B68532978379100\r\n
    # 注意：53297837 是 hex digits，不是十進位整數字串
    tail = decode_trailing_hex("532978379100")
    assert tail is not None
    assert tail.tick_raw == 0x53297837
    assert tail.tick_raw != 53297837  # 若誤用 int(..., 10) 會變成這個
    assert tail.hit_counter == 0x9100


def test_decode_trailing_hex_wireshark_sample_2():
    # 截圖圖2：$140201B81B68543332438388\r\n → ticks 54333243, hit 8388
    tail = decode_trailing_hex("543332438388")
    assert tail is not None
    assert tail.tick_raw == 0x54333243
    assert tail.hit_counter == 0x8388


def test_decode_trailing_hex_pdf_lap2_ticks():
    # 截圖範例 Decoder_2_ticks = 543C8B3B（強度欄位用佔位）
    tail = decode_trailing_hex("543C8B3B428F")
    assert tail is not None
    assert tail.tick_raw == 0x543C8B3B
    assert tail.hit_counter == 0x428F


def test_wireshark_delta_is_not_decimal_14251_hz():
    """圖1 若把 ASCII 當十進位會得到 ~14251 Hz；正確是 hex / 256000。"""
    t1 = int("53329783", 16)
    t2 = int("54333243", 16)
    delta = (t2 - t1) % (1 << 32)
    # 錯誤路徑（十進位）才會接近 1003460
    assert int("54333243", 10) - int("53329783", 10) == 1003460
    assert delta != 1003460
    assert delta / 256000.0 == pytest.approx((0x54333243 - 0x53329783) / 256000.0)


def test_decode_trailing_hex_too_short():
    tail = decode_trailing_hex("53297837")
    assert tail is not None
    assert tail.tick_raw is None
    assert tail.hit_counter is None


def test_decode_trailing_hex_empty_returns_none():
    assert decode_trailing_hex("") is None


def test_decode_trailing_hex_invalid_hex_returns_none():
    assert decode_trailing_hex("ZZZZZZZZZZZZ") is None


def test_passing_rule_to_passing_event_decodes_tick():
    rule = PassingRule(transponder_id_len=12)
    frame = b"$140201B81B68532978379100\r\n"
    event = rule.to_passing_event(
        frame, received_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert event.transponder_id == "140201B81B68"
    assert event.raw_payload == "140201B81B68532978379100"
    assert event.decoder_tick == 0x53297837
    assert event.hit_counter == 0x9100
    assert event.tick_byte_len == DECODER_TICK_BYTE_LEN


def test_passing_rule_tid_only_no_tick():
    """沒有 trailing hex 時（payload 只有 transponder id）不該出錯。"""
    rule = PassingRule(transponder_id_len=12)
    transponder_hex = "140210E3C468"
    frame = f"${transponder_hex}\r\n".encode("ascii")
    event = rule.to_passing_event(
        frame, received_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert event.transponder_id == transponder_hex
    assert event.decoder_tick is None
    assert event.hit_counter is None
    assert event.tick_byte_len is None

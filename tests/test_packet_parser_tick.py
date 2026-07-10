"""packet_parser.py 的 trailing-byte tick 解碼測試：涵蓋基本解碼、長度
不足、無效 hex，以及 PassingRule.to_passing_event 整合行為。"""

from __future__ import annotations

from datetime import datetime, timezone

from services.decoder_ingest.packet_parser import PassingRule, decode_trailing_hex


def test_decode_trailing_hex_basic():
    # hit_counter=0x01；tick bytes（offset=1,len=3）= 0x0000FF -> 255；
    # 剩下的 "8C00" 不在設定範圍內，忽略。
    tail = decode_trailing_hex("010000FF8C00", tick_byte_offset=1, tick_byte_len=3)
    assert tail is not None
    assert tail.hit_counter == 1
    assert tail.tick_raw == 255


def test_decode_trailing_hex_too_short_for_tick_field():
    tail = decode_trailing_hex("01", tick_byte_offset=1, tick_byte_len=3)
    assert tail is not None
    assert tail.hit_counter == 1
    assert tail.tick_raw is None


def test_decode_trailing_hex_empty_returns_none():
    assert decode_trailing_hex("", tick_byte_offset=1, tick_byte_len=3) is None


def test_decode_trailing_hex_invalid_hex_returns_none():
    assert decode_trailing_hex("ZZ", tick_byte_offset=1, tick_byte_len=3) is None


def test_passing_rule_to_passing_event_decodes_tick():
    rule = PassingRule(transponder_id_len=12, tick_byte_offset=1, tick_byte_len=3)
    transponder_hex = "140210E3C468"
    trailing_hex = "010000FF8C00"
    frame = f"${transponder_hex}{trailing_hex}\r\n".encode("ascii")
    received_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event = rule.to_passing_event(frame, received_at=received_at)

    assert event.transponder_id == transponder_hex
    assert event.raw_payload == transponder_hex + trailing_hex
    assert event.hit_counter == 1
    assert event.decoder_tick == 255
    assert event.tick_byte_len == 3


def test_passing_rule_handles_missing_trailing_bytes_safely():
    """沒有 trailing hex 時（payload 只有 transponder id 本身）不該出錯，
    decoder_tick/hit_counter 皆為 None，等同功能關閉時的行為。"""
    rule = PassingRule(transponder_id_len=12, tick_byte_offset=1, tick_byte_len=3)
    transponder_hex = "140210E3C468"
    frame = f"${transponder_hex}\r\n".encode("ascii")
    received_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event = rule.to_passing_event(frame, received_at=received_at)

    assert event.decoder_tick is None
    assert event.hit_counter is None
    assert event.tick_byte_len is None

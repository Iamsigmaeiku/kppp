from __future__ import annotations

import struct
from datetime import datetime, timezone

from services.webapp.udp_telemetry import _nav_pvt_utc_ns, _parse_gps_raw


FMT_V2 = "<BBHBBBBBBBBBiIIIIIIiiiiiiiiQI"


def _v2_payload(*, nano: int = 123_456_789, seq: int = 42) -> bytes:
    return struct.pack(
        FMT_V2,
        2, 0x07, 2026, 7, 24, 12, 34, 56, 3, 18, 0x01, 0x20,
        nano, 432_100, 25, 1200, 2300, 150, 9000,
        227_423_000, 1_203_217_000, 15_000,
        12_000, -3_000, 100, 12_369, 9_000_000,
        12_345_678_901, seq,
    )


def test_v2_nav_pvt_all_fields_and_epoch():
    raw = _parse_gps_raw(_v2_payload())
    assert raw is not None
    assert raw.version == 2
    assert raw.packet_seq == 42
    assert raw.sensor_time_us == 12_345_678_901
    assert raw.t_acc == 25
    assert raw.head_acc == 9000
    assert raw.flags == 0x01
    assert raw.flags2 == 0x20
    expected = int(datetime(2026, 7, 24, 12, 34, 56, tzinfo=timezone.utc).timestamp()) * 1_000_000_000 + 123_456_789
    assert _nav_pvt_utc_ns(raw) == expected


def test_legacy_gps_payload_still_decodes():
    payload = struct.pack(
        "<IiiiiiiiiIIIBB",
        1000, 227_423_000, 1_203_217_000, 15_000,
        1, 2, 3, 4, 5, 1000, 2000, 300, 12, 3,
    )
    raw = _parse_gps_raw(payload)
    assert raw is not None
    assert raw.version == 1
    assert raw.itow == 1000
    assert raw.packet_seq is None
    assert _nav_pvt_utc_ns(raw) is None


def test_invalid_utc_flags_never_uses_calendar_epoch():
    payload = bytearray(_v2_payload())
    payload[1] = 0x01
    raw = _parse_gps_raw(bytes(payload))
    assert raw is not None
    assert _nav_pvt_utc_ns(raw) is None


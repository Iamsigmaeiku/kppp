from __future__ import annotations

from services.audit.session_audit import audit_rows


def _gps(i: int, *, itow: int | None = None, seq: int | None = None, accepted: int = 1):
    t = itow if itow is not None else i * 100
    return {
        "source": "kart_telemetry",
        "time_ns": 1_700_000_000_000_000_000 + t * 1_000_000,
        "gnss_time_ns": 1_700_000_000_000_000_000 + t * 1_000_000,
        "gps_itow_ms": t,
        "gps_packet_seq": i if seq is None else seq,
        "gps_lat": 22.7423 + i * 1e-6,
        "gps_lon": 120.3217,
        "gps_course_deg": 0.0,
        "gps_gate_accepted": accepted,
        "gps_gate_reason": 4 if not accepted else 0,
        "gps_fix_type": 3,
        "gps_satellites": 16,
        "gps_h_acc_mm": 1200,
        "sensor_time_us": 100_000 + i * 100_000,
        "receive_time_ns": 1_700_000_000_010_000_000 + t * 1_000_000,
    }


def test_audit_detects_itow_and_sequence_layers():
    rows = [_gps(0), _gps(1), _gps(3, itow=300, seq=4), _gps(4, itow=400, seq=5, accepted=0)]
    report = audit_rows(rows, session_id="synthetic")
    assert report["itow"]["missing_epochs"] == 1
    assert report["packet_sequence"]["missing"] == 2
    assert report["gps_sampling"]["rejected"] == 1
    assert any(g["class"] == "A" for g in report["gaps"])
    assert any(g["class"] == "E" for g in report["gaps"])
    assert any(g["class"] == "H" for g in report["gaps"])


def test_audit_uses_counter_to_disambiguate_ring_loss():
    rows = [_gps(0, seq=1), _gps(1, seq=3), {"source": "kart_telemetry", "ring_drops": 2}]
    report = audit_rows(rows, session_id="synthetic")
    assert any(g["class"] == "D" for g in report["gaps"])

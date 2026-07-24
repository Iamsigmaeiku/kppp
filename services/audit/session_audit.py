"""Evidence-based position/timing audit shared by the CLI and tests.

Rows are deliberately plain dictionaries so captures can be exported to JSON
and replayed without an InfluxDB connection.  A row has ``source``, ``time_ns``
and any number of telemetry fields.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

GPS_WEEK_MS = 604_800_000
GATE_REASONS = {
    0: "accepted",
    1: "fix_type",
    2: "num_sv",
    3: "h_acc",
    4: "jump_or_innovation",
    5: "timestamp",
    6: "reacquire_pending",
}
GAP_CLASSES = {
    "A": "GNSS did not produce NAV-PVT",
    "B": "ESP32 UART/parser loss",
    "C": "FreeRTOS queue full",
    "D": "ring buffer loss",
    "E": "UDP/network loss",
    "F": "server queue drop",
    "G": "Influx write failure",
    "H": "quality gate rejection",
    "I": "jump-filter reset/re-anchor discontinuity",
    "J": "query/fallback/render layer omission",
    "K": "session boundary or timestamp error",
}


def _number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    a = sorted(values)
    if len(a) == 1:
        return a[0]
    pos = (len(a) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    return a[lo] + (a[hi] - a[lo]) * (pos - lo)


def _summary(values: Iterable[Any]) -> dict[str, float | int | None]:
    a = [x for v in values if (x := _number(v)) is not None]
    return {
        "count": len(a),
        "min": min(a) if a else None,
        "p50": _percentile(a, 0.50),
        "p95": _percentile(a, 0.95),
        "p99": _percentile(a, 0.99),
        "max": max(a) if a else None,
    }


def _haversine_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lon1 = math.radians(float(a["gps_lat"])), math.radians(float(a["gps_lon"]))
    lat2, lon2 = math.radians(float(b["gps_lat"])), math.radians(float(b["gps_lon"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


def _time_ns(row: dict[str, Any]) -> int | None:
    for key in ("gnss_time_ns", "time_ns", "receive_time_ns"):
        v = row.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _counter_max(rows: list[dict[str, Any]], name: str) -> int:
    vals = [int(v) for r in rows if (v := _number(r.get(name))) is not None]
    return max(vals, default=0)


def audit_rows(
    rows: list[dict[str, Any]],
    *,
    session_id: str,
    expected_period_ms: float | None = None,
) -> dict[str, Any]:
    gps = [
        r
        for r in rows
        if r.get("source") == "kart_telemetry"
        and (r.get("gps_itow_ms") is not None or r.get("gps_lat") is not None)
    ]
    gps.sort(key=lambda r: (_time_ns(r) or 0, int(r.get("gps_packet_seq") or 0)))
    accepted = [
        r
        for r in gps
        if int(
            r.get(
                "gps_gate_accepted",
                1 if r.get("gps_fix") == "1" else r.get("gps_fresh", 0),
            )
            or 0
        )
        == 1
    ]
    rejected = [r for r in gps if r not in accepted]
    itow_gps = [r for r in gps if r.get("gps_itow_ms") is not None]

    intervals_ms: list[float] = []
    itow_diffs: list[int] = []
    duplicates = backwards = week_wraps = 0
    missing_epochs = 0
    for prev, cur in zip(gps, gps[1:]):
        a, b = _time_ns(prev), _time_ns(cur)
        if a is not None and b is not None and b >= a:
            intervals_ms.append((b - a) / 1e6)
    for prev, cur in zip(itow_gps, itow_gps[1:]):
        di = int(cur["gps_itow_ms"]) - int(prev["gps_itow_ms"])
        if di == 0:
            duplicates += 1
        elif di < 0:
            if int(prev["gps_itow_ms"]) > GPS_WEEK_MS - 10_000 and int(cur["gps_itow_ms"]) < 10_000:
                week_wraps += 1
                di += GPS_WEEK_MS
            else:
                backwards += 1
        if di > 0:
            itow_diffs.append(di)

    nominal = expected_period_ms
    if nominal is None and itow_diffs:
        nominal = float(median([d for d in itow_diffs if 20 <= d <= 2000] or itow_diffs))
    if nominal and nominal > 0:
        for di in itow_diffs:
            if di > nominal * 1.5:
                missing_epochs += max(0, round(di / nominal) - 1)

    seq_missing = seq_duplicate = seq_backward = 0
    seq_rows = [r for r in itow_gps if r.get("gps_packet_seq") is not None]
    for prev, cur in zip(seq_rows, seq_rows[1:]):
        d = (int(cur["gps_packet_seq"]) - int(prev["gps_packet_seq"])) & 0xFFFFFFFF
        if d == 0:
            seq_duplicate += 1
        elif 1 < d < 0x80000000:
            seq_missing += d - 1
        elif d >= 0x80000000:
            seq_backward += 1

    speeds: list[float] = []
    accels: list[float] = []
    yaw_rates: list[float] = []
    prev_speed: float | None = None
    for a, b in zip(gps, gps[1:]):
        ta, tb = _time_ns(a), _time_ns(b)
        if ta is None or tb is None or tb <= ta:
            continue
        dt = (tb - ta) / 1e9
        if all(k in a and a[k] is not None for k in ("gps_lat", "gps_lon")) and all(
            k in b and b[k] is not None for k in ("gps_lat", "gps_lon")
        ):
            speed = _haversine_m(a, b) / dt
            speeds.append(speed)
            if prev_speed is not None:
                accels.append((speed - prev_speed) / dt)
            prev_speed = speed
        ca, cb = _number(a.get("gps_course_deg")), _number(b.get("gps_course_deg"))
        if ca is not None and cb is not None:
            dyaw = (cb - ca + 180.0) % 360.0 - 180.0
            yaw_rates.append(dyaw / dt)

    reject_reasons = Counter(
        GATE_REASONS.get(int(r.get("gps_gate_reason") or 0), "unknown") for r in rejected
    )

    def compare_source(source: str, lat_key: str, lon_key: str) -> dict[str, Any]:
        other = [
            r
            for r in rows
            if r.get("source") == source
            and r.get(lat_key) is not None
            and r.get(lon_key) is not None
            and _time_ns(r) is not None
        ]
        other.sort(key=lambda r: _time_ns(r) or 0)
        ref = [r for r in accepted if _time_ns(r) is not None]
        ref.sort(key=lambda r: _time_ns(r) or 0)
        errors: list[float] = []
        j = 0
        for r in other:
            tr = _time_ns(r) or 0
            while j + 1 < len(ref) and abs((_time_ns(ref[j + 1]) or 0) - tr) <= abs((_time_ns(ref[j]) or 0) - tr):
                j += 1
            if ref and abs((_time_ns(ref[j]) or 0) - tr) <= 200_000_000:
                proxy = {
                    "gps_lat": r[lat_key],
                    "gps_lon": r[lon_key],
                }
                errors.append(_haversine_m(ref[j], proxy))
        return {
            "points": len(other),
            "paired_with_accepted_raw": len(errors),
            "separation_m": _summary(errors),
            "note": "Separation from raw GPS is consistency evidence, not absolute accuracy.",
        }
    counters = {
        name: _counter_max(rows, name)
        for name in (
            "crc_errors",
            "gps_queue_drops",
            "queue_drops",
            "ring_drops",
            "gps_ring_drops",
            "server_queue_drops",
            "influx_write_failures",
            "gps_uart_overflows",
            "gps_parser_errors",
        )
    }

    def imu_metrics(prefix: str) -> dict[str, Any]:
        axk, ayk, azk = (f"{prefix}{k}" for k in ("ax", "ay", "az"))
        samples = [
            r
            for r in rows
            if r.get("source") == "kart_telemetry"
            and r.get(axk) is not None
            and r.get(ayk) is not None
            and r.get(azk) is not None
        ]
        norms = [
            math.sqrt(float(r[axk]) ** 2 + float(r[ayk]) ** 2 + float(r[azk]) ** 2)
            for r in samples
        ]
        intervals = [
            ((_time_ns(b) or 0) - (_time_ns(a) or 0)) / 1e6
            for a, b in zip(samples, samples[1:])
            if _time_ns(a) is not None and _time_ns(b) is not None and (_time_ns(b) or 0) > (_time_ns(a) or 0)
        ]
        return {
            "points": len(samples),
            "interval_ms": _summary(intervals),
            "acceleration_g": {
                "x": _summary(r.get(axk) for r in samples),
                "y": _summary(r.get(ayk) for r in samples),
                "z": _summary(r.get(azk) for r in samples),
                "norm": _summary(norms),
            },
        }

    passing_rows = [
        r
        for r in rows
        if r.get("source") == "decoder_raw_events"
        and r.get("event_type") == "passing"
    ]
    by_tid: dict[str, list[dict[str, Any]]] = {}
    for r in passing_rows:
        raw = str(r.get("raw_hex") or "")
        tick = r.get("decoder_tick")
        if tick is None and len(raw) >= 20:
            try:
                tick = int(raw[12:20], 16)
            except ValueError:
                tick = None
        copy = dict(r)
        copy["_audit_tick"] = tick
        by_tid.setdefault(str(r.get("transponder_id") or raw[:12]), []).append(copy)
    decoder_by_tid: dict[str, Any] = {}
    for tid, events in by_tid.items():
        events.sort(key=lambda r: _time_ns(r) or 0)
        tick_deltas: list[int] = []
        implied_hz: list[float] = []
        wraps = 0
        for a, b in zip(events, events[1:]):
            ta, tb = a.get("_audit_tick"), b.get("_audit_tick")
            if ta is None or tb is None:
                continue
            if int(tb) < int(ta):
                wraps += 1
            delta = (int(tb) - int(ta)) & 0xFFFFFFFF
            tick_deltas.append(delta)
            wall_a, wall_b = _time_ns(a), _time_ns(b)
            if wall_a is not None and wall_b is not None and wall_b > wall_a:
                implied_hz.append(delta / ((wall_b - wall_a) / 1e9))
        decoder_by_tid[tid] = {
            "passings": len(events),
            "tick_intervals": len(tick_deltas),
            "tick_wraps": wraps,
            "implied_tick_hz_from_tcp_time": _summary(implied_hz),
        }

    gaps: list[dict[str, Any]] = []
    for prev, cur in zip(itow_gps, itow_gps[1:]):
        di = int(cur["gps_itow_ms"]) - int(prev["gps_itow_ms"])
        if di < 0 and int(prev["gps_itow_ms"]) > GPS_WEEK_MS - 10_000:
            di += GPS_WEEK_MS
        seq_d = None
        if prev.get("gps_packet_seq") is not None and cur.get("gps_packet_seq") is not None:
            seq_d = (int(cur["gps_packet_seq"]) - int(prev["gps_packet_seq"])) & 0xFFFFFFFF
        if nominal and di > nominal * 1.5:
            klass = "A"
            evidence = f"iTOW delta {di} ms spans missing measurement epochs"
            if counters["gps_uart_overflows"] or counters["gps_parser_errors"]:
                klass, evidence = "B", "UART overflow/parser error counter is non-zero"
            elif counters["gps_queue_drops"]:
                klass, evidence = "C", "GPS FreeRTOS queue drop counter is non-zero"
            gaps.append({"class": klass, "evidence": evidence, "from_itow": prev["gps_itow_ms"], "to_itow": cur["gps_itow_ms"]})
        if seq_d is not None and 1 < seq_d < 0x80000000:
            klass, evidence = "E", f"packet sequence skipped {seq_d - 1}"
            if counters["gps_ring_drops"] or counters["ring_drops"]:
                klass, evidence = "D", "GPS ring drop counter is non-zero"
            gaps.append({"class": klass, "evidence": evidence, "from_seq": prev["gps_packet_seq"], "to_seq": cur["gps_packet_seq"]})

    if rejected:
        gaps.append({"class": "H", "count": len(rejected), "evidence": dict(reject_reasons)})
    legacy_reanchor = any(int(r.get("jump_reanchor", 0) or 0) for r in rows)
    if legacy_reanchor:
        gaps.append({"class": "I", "evidence": "explicit jump_reanchor marker"})
    legacy_long_gaps = [x for x in intervals_ms if x > 2000.0]
    if legacy_long_gaps and not itow_gps:
        gaps.append(
            {
                "class": "UNRESOLVED",
                "candidates": ["A", "B", "C", "D", "E", "F", "G", "J", "K"],
                "count": len(legacy_long_gaps),
                "max_gap_sec": max(legacy_long_gaps) / 1000.0,
                "evidence": (
                    "receive/storage timestamp gap without iTOW, sequence or layer counters; "
                    "legacy evidence cannot select one A-K class"
                ),
            }
        )

    source_counts = Counter(str(r.get("source", "unknown")) for r in rows)
    report = {
        "schema_version": 2,
        "session_id": session_id,
        "data_evidence": {
            "row_count": len(rows),
            "source_counts": dict(source_counts),
            "v2_gps_points": sum(int(r.get("gps_packet_seq") is not None) for r in gps),
            "legacy_gps_points": sum(int(r.get("gps_packet_seq") is None) for r in gps),
        },
        "gps_sampling": {
            "points": len(gps),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "actual_rate_hz": (1000.0 / _percentile(intervals_ms, 0.5)) if intervals_ms and (_percentile(intervals_ms, 0.5) or 0) > 0 else None,
            "interval_ms": _summary(intervals_ms),
            "gaps_over_1s": sum(x > 1000.0 for x in intervals_ms),
            "gaps_over_2s": sum(x > 2000.0 for x in intervals_ms),
            "max_gap_sec": (max(intervals_ms) / 1000.0) if intervals_ms else None,
            "nominal_period_ms": nominal,
        },
        "itow": {
            "missing_epochs": missing_epochs,
            "duplicates": duplicates,
            "backwards": backwards,
            "week_wraps": week_wraps,
        },
        "packet_sequence": {
            "missing": seq_missing,
            "duplicates": seq_duplicate,
            "backwards": seq_backward,
        },
        "transport_counters": counters,
        "quality": {
            "fix_type": dict(Counter(str(r.get("gps_fix_type")) for r in gps)),
            "num_sv": _summary(r.get("gps_satellites") for r in gps),
            "h_acc_m": _summary((_number(r.get("gps_h_acc_mm")) or 0) / 1000.0 for r in gps if r.get("gps_h_acc_mm") is not None),
            "t_acc_ns": _summary(r.get("gps_t_acc_ns") for r in gps),
            "reject_reasons": dict(reject_reasons),
        },
        "imu": {
            "icm42688": imu_metrics(""),
            "mpu6050": imu_metrics("mpu_"),
            "note": "Influx ingest is downsampled; interval statistics are storage cadence, not sensor ODR.",
        },
        "decoder": {
            "passing_rows": len(passing_rows),
            "by_transponder": decoder_by_tid,
            "tick_hz_configured_assumption": 256000.0,
            "note": "TCP-derived frequency is diagnostic only; validate on long clean captures.",
        },
        "kinematics": {
            "implied_speed_mps": _summary(speeds),
            "implied_acceleration_mps2": _summary(accels),
            "implied_yaw_rate_dps": _summary(yaw_rates),
        },
        "position_comparison": {
            "esp32_fused_vs_raw": compare_source("dr_position", "lat_dr", "lon_dr"),
            "track_smoothed_vs_raw": compare_source("track_smoothed", "lat_s", "lon_s"),
        },
        "gaps": gaps,
        "gap_class_legend": GAP_CLASSES,
        "limitations": [],
    }
    missing_required = [
        key
        for key in ("gps_itow_ms", "gps_packet_seq", "sensor_time_us", "gnss_time_ns", "receive_time_ns")
        if not any(r.get(key) is not None for r in gps)
    ]
    if missing_required:
        report["limitations"].append(
            "Legacy capture lacks fields required for layer-exact attribution: "
            + ", ".join(missing_required)
        )
    if not rows:
        report["limitations"].append("No session rows were available; no accuracy claim is possible.")
    return report


def draw_overlay(rows: list[dict[str, Any]], output: Path) -> None:
    """Draw raw/accepted/rejected/fused/smoothed on the calibrated track PNG."""
    from PIL import Image, ImageDraw
    from services.webapp.track_coords import latlng_to_px

    root = Path(__file__).resolve().parents[2]
    bg = root / "services" / "webapp" / "static" / "tracks" / "tks_qiaotou_track.png"
    image = Image.open(bg).convert("RGBA") if bg.exists() else Image.new("RGBA", (1280, 1280), "white")
    draw = ImageDraw.Draw(image)

    def series(source: str, lat_key: str, lon_key: str, accepted: int | None = None):
        out = []
        for r in rows:
            if r.get("source") != source or r.get(lat_key) is None or r.get(lon_key) is None:
                continue
            if accepted is not None:
                gate = r.get(
                    "gps_gate_accepted",
                    1 if r.get("gps_fix") == "1" else r.get("gps_fresh", 0),
                )
                if int(gate or 0) != accepted:
                    continue
            out.append((_time_ns(r) or 0, latlng_to_px(float(r[lat_key]), float(r[lon_key]))))
        out.sort(key=lambda item: item[0])
        return out

    layers = [
        (series("kart_telemetry", "gps_lat", "gps_lon"), (120, 120, 120, 100), 1),
        (series("kart_telemetry", "gps_lat", "gps_lon", 0), (230, 30, 30, 220), 3),
        (series("kart_telemetry", "gps_lat", "gps_lon", 1), (30, 100, 255, 220), 2),
        (series("dr_position", "lat_dr", "lon_dr"), (255, 150, 20, 190), 2),
        (series("track_smoothed", "lat_s", "lon_s"), (30, 200, 80, 220), 2),
    ]
    for layer_index, (timed_pts, color, width) in enumerate(layers):
        if layer_index == 1:  # rejected raw fixes are points, never a made-up path
            for _, (x, y) in timed_pts:
                draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)
            continue
        segment: list[tuple[float, float]] = []
        prev_t: int | None = None
        for t, point in timed_pts:
            if prev_t is not None and t - prev_t > 500_000_000:
                if len(segment) >= 2:
                    draw.line(segment, fill=color, width=width)
                segment = []
            segment.append(point)
            prev_t = t
        if len(segment) >= 2:
            draw.line(segment, fill=color, width=width)
        elif segment:
            x, y = segment[0]
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)

"""iTOW 錨定器單元測試。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI

from services.webapp.udp_telemetry import GPS_WEEK_MS, UdpTelemetryServer


def _make_server() -> UdpTelemetryServer:
    app = FastAPI()
    app.state = SimpleNamespace(influx_reader=None, telemetry_by_device={})
    return UdpTelemetryServer(
        app, host="127.0.0.1", port=0, device_id="test-dev"
    )


def test_itow_normal_increment_tracks_offset():
    """正常遞增 — fix_time 與 server_time 偏移一致。"""
    srv = _make_server()
    t0 = 1_700_000_000.0
    with patch("services.webapp.udp_telemetry.time.time", return_value=t0):
        ts0 = srv._rebuild_fix_time(100_000)
    assert ts0 == int(t0 * 1000)
    assert srv._anchor_itow == 100_000

    with patch("services.webapp.udp_telemetry.time.time", return_value=t0 + 2.0):
        ts1 = srv._rebuild_fix_time(102_000)  # +2s in iTOW
    # 錨定不變：fix = anchor_server + (itow - anchor_itow)
    assert ts1 == int(t0 * 1000) + 2_000
    assert srv._anchor_itow == 100_000


def test_itow_week_wrap_reanchors():
    """週回捲 — itow 從接近週尾跳到開頭，觸發重新錨定。"""
    srv = _make_server()
    t0 = 1_700_000_000.0
    near_end = GPS_WEEK_MS - 1_000  # 604799000
    with patch("services.webapp.udp_telemetry.time.time", return_value=t0):
        srv._rebuild_fix_time(near_end)

    t1 = t0 + 2.0
    with patch("services.webapp.udp_telemetry.time.time", return_value=t1):
        ts = srv._rebuild_fix_time(1_000)  # 週回捲
    assert srv._anchor_itow == 1_000
    assert ts == int(t1 * 1000)


def test_itow_reboot_drift_reanchors():
    """重開機跳變 — |fix_time - now| > 5s，觸發重新錨定。"""
    srv = _make_server()
    t0 = 1_700_000_000.0
    with patch("services.webapp.udp_telemetry.time.time", return_value=t0):
        srv._rebuild_fix_time(50_000)

    # 伺服器時間前進 60s，但 iTOW 只前進 1s → 推算出的 fix 會偏離 now > 5s
    t1 = t0 + 60.0
    with patch("services.webapp.udp_telemetry.time.time", return_value=t1):
        ts = srv._rebuild_fix_time(51_000)
    assert srv._anchor_itow == 51_000
    assert ts == int(t1 * 1000)


def test_itow_out_of_order_still_writes(caplog):
    """亂序 frame — ts_ms 比上一筆早，log.debug 但仍寫入。"""
    srv = _make_server()
    t0 = 1_700_000_000.0
    with patch("services.webapp.udp_telemetry.time.time", return_value=t0):
        ts0 = srv._rebuild_fix_time(200_000)
    with patch("services.webapp.udp_telemetry.time.time", return_value=t0 + 1.0):
        ts1 = srv._rebuild_fix_time(201_000)

    with caplog.at_level(logging.DEBUG, logger="services.webapp.udp_telemetry"):
        with patch("services.webapp.udp_telemetry.time.time", return_value=t0 + 1.5):
            ts_oo = srv._rebuild_fix_time(200_500)  # 介於兩筆之間 → 比 last 早

    assert ts_oo < ts1
    assert srv._last_gps_ts_ms == ts_oo  # 仍更新
    assert any("out-of-order" in r.message for r in caplog.records)

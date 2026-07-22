"""Kiosk token 唯讀放行：leaderboard / sessions JSON，拒絕寫入與錯 token。"""

from __future__ import annotations


def test_kiosk_leaderboard_requires_token(webapp_client):
    r = webapp_client.get("/api/leaderboard")
    assert r.status_code == 401


def test_kiosk_leaderboard_rejects_bad_token(webapp_client):
    r = webapp_client.get(
        "/api/leaderboard",
        headers={"X-Kiosk-Token": "wrong"},
    )
    assert r.status_code == 401


def test_kiosk_leaderboard_ok_with_token(webapp_client):
    r = webapp_client.get(
        "/api/leaderboard",
        headers={"X-Kiosk-Token": "test-kiosk-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "alltime_entries" in body
    assert "session_entries" in body
    assert "influx_unavailable" in body


def test_kiosk_sessions_ok_with_token(webapp_client):
    r = webapp_client.get(
        "/api/sessions",
        headers={"X-Kiosk-Token": "test-kiosk-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body
    assert isinstance(body["sessions"], list)


def test_kiosk_token_does_not_allow_archive(webapp_client):
    r = webapp_client.post(
        "/api/session/archive",
        headers={"X-Kiosk-Token": "test-kiosk-token"},
    )
    assert r.status_code == 401


def test_kiosk_token_does_not_allow_bindings_write(webapp_client):
    r = webapp_client.post(
        "/api/bindings",
        headers={"X-Kiosk-Token": "test-kiosk-token"},
        json={"car_number": "11"},
    )
    assert r.status_code == 401

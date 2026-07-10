"""services/webapp 的 auth/bindings 路由整合測試，走 AUTH_DEV_BYPASS 流程
（見 tests/conftest.py），不需要真的打 Google OAuth。"""

from __future__ import annotations


def test_unauthenticated_html_redirects_to_login(webapp_client):
    r = webapp_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_telemetry_ingest_rejects_bad_token(webapp_client):
    r = webapp_client.post(
        "/api/telemetry/ingest",
        headers={"Authorization": "Bearer wrong"},
        json={
            "device_id": "esp32-test",
            "samples": [{"ax": 0.1, "ay": 0.0, "az": 1.0}],
        },
    )
    assert r.status_code == 401


def test_me_returns_none_when_not_logged_in(webapp_client):
    r = webapp_client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == {"user": None}


def test_dev_login_sets_session_and_me_reflects_it(webapp_client):
    r = webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/profile"

    r2 = webapp_client.get("/api/auth/me")
    assert r2.status_code == 200
    user = r2.json()["user"]
    assert user is not None
    assert user["email"] == "dev@example.local"


def test_logout_clears_session(webapp_client):
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    assert webapp_client.get("/api/auth/me").json()["user"] is not None

    r = webapp_client.post("/api/auth/logout")
    assert r.status_code == 200

    r2 = webapp_client.get("/api/auth/me")
    assert r2.json() == {"user": None}


def test_bindings_requires_login(webapp_client):
    r = webapp_client.post("/api/bindings", json={"car_number": "11"})
    assert r.status_code == 401


def test_bindings_fails_when_lap_tracker_not_initialized(webapp_client):
    # 這個測試 process 沒有跑 decoder_ingest 的 --with-dashboard 服務，
    # get_session_manager() 恆回傳 None，bind 應該明確地回 503 而不是
    # 假裝綁定成功。
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    r = webapp_client.post("/api/bindings", json={"car_number": "11"})
    assert r.status_code == 503


def test_bindings_rejects_unknown_car_number(webapp_client):
    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    lap_tracker = LapTracker(car_number_map={"AABBCCDDEEFF": "11"})
    set_lap_tracker(lap_tracker)
    set_session_manager(SessionManager.start_new(), None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r = webapp_client.post("/api/bindings", json={"car_number": "99"})
        assert r.status_code == 404
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)


def test_bindings_by_car_number_succeeds(webapp_client):
    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    lap_tracker = LapTracker(car_number_map={"AABBCCDDEEFF": "11"})
    set_lap_tracker(lap_tracker)
    set_session_manager(SessionManager.start_new(), None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r = webapp_client.post("/api/bindings", json={"car_number": "11"})
        assert r.status_code == 200
        body = r.json()
        assert body["car_number"] == "11"
        assert body["transponder_id"] == "AABBCCDDEEFF"
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)


async def _seed_race_session(webapp_app, *, session_id: str, session_number: int):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from services.webapp.models import RaceSession

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    today = datetime.now(tz).date()
    async with webapp_app.state.session_factory() as db:
        db.add(
            RaceSession(
                id=session_id,
                started_at=datetime.now(tz),
                session_date=today,
                session_number=session_number,
            )
        )
        await db.commit()
    return today


def test_bindings_by_session_number_resolves_correct_session(webapp_client, webapp_app):
    import asyncio

    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    # 目前正在進行的場次是 sess-live-xxx，但使用者輸入 #7 想綁定的是
    # 今天稍早已經結束的另一節場次（sess-earlier-xxx）——確認 session_number
    # 會解析成正確、不同於「目前這節」的 session_id。
    asyncio.run(
        _seed_race_session(webapp_app, session_id="sess-earlier-session", session_number=7)
    )

    lap_tracker = LapTracker(car_number_map={"AABBCCDDEEFF": "12"})
    set_lap_tracker(lap_tracker)
    set_session_manager(SessionManager.start_new(), None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r = webapp_client.post(
            "/api/bindings", json={"car_number": "12", "session_number": 7}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "sess-earlier-session"
        assert body["car_number"] == "12"
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)


def test_bindings_by_session_number_404_when_not_found(webapp_client):
    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    lap_tracker = LapTracker(car_number_map={"AABBCCDDEEFF": "13"})
    set_lap_tracker(lap_tracker)
    set_session_manager(SessionManager.start_new(), None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r = webapp_client.post(
            "/api/bindings", json={"car_number": "13", "session_number": 999}
        )
        assert r.status_code == 404
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)

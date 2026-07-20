"""services/webapp 的 auth/bindings 路由整合測試，走 AUTH_DEV_BYPASS 流程
（見 tests/conftest.py），不需要真的打 Google OAuth。"""

from __future__ import annotations

from services.webapp.auth import build_oauth
from services.webapp.config import GoogleOAuthConfig


def test_unauthenticated_html_redirects_to_login(webapp_client):
    r = webapp_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")

    r2 = webapp_client.get("/leaderboard", follow_redirects=False)
    assert r2.status_code == 302
    assert "/login" in r2.headers["location"]

    r3 = webapp_client.get("/telemetry", follow_redirects=False)
    assert r3.status_code == 302
    assert "/login" in r3.headers["location"]

    r4 = webapp_client.get("/grafana/d/kart-telemetry/", follow_redirects=False)
    assert r4.status_code == 302
    assert "/login" in r4.headers["location"]


def test_login_page_has_no_site_nav(webapp_client):
    r = webapp_client.get("/login")
    assert r.status_code == 200
    body = r.text
    assert "請先登入" in body
    assert 'href="/leaderboard"' not in body
    assert 'href="/telemetry"' not in body
    assert "使用 Google" in body or "開發模式登入" in body


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


def test_build_oauth_uses_static_google_endpoints():
    oauth = build_oauth(
        GoogleOAuthConfig(
            client_id="cid",
            client_secret="sec",
            redirect_uri="https://example.com/callback",
        )
    )
    client = oauth.create_client("google")
    assert client is not None
    assert client.client_kwargs["scope"] == "openid email profile"
    assert client.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
    assert client.access_token_url == "https://oauth2.googleapis.com/token"
    assert client.server_metadata["userinfo_endpoint"] == (
        "https://openidconnect.googleapis.com/v1/userinfo"
    )
    assert client.server_metadata["jwks_uri"] == "https://www.googleapis.com/oauth2/v3/certs"


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


def test_bindings_update_same_session_car_number(webapp_client):
    """同一節換車號：更新既有綁定，不開第二列。"""
    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    lap_tracker = LapTracker(
        car_number_map={"AAAAAAAAAAAA": "19", "BBBBBBBBBBBB": "17"}
    )
    set_lap_tracker(lap_tracker)
    set_session_manager(SessionManager.start_new(), None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r1 = webapp_client.post("/api/bindings", json={"car_number": "19"})
        assert r1.status_code == 200
        sid = r1.json()["session_id"]

        r2 = webapp_client.post("/api/bindings", json={"car_number": "17"})
        assert r2.status_code == 200
        assert r2.json()["car_number"] == "17"
        assert r2.json()["session_id"] == sid
        assert r2.json().get("updated") is True

        me = webapp_client.get("/api/bindings/me").json()
        current = [b for b in me["bindings"] if b["session_id"] == sid]
        assert len(current) == 1
        assert current[0]["car_number"] == "17"
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)


def test_bindings_current_false_when_only_previous_session(webapp_client, webapp_app):
    """上一節綁過 ≠ 本節已綁定（is_current_session /current 不認歷史）。"""
    import asyncio

    from services.decoder_ingest.dashboard import set_lap_tracker, set_session_manager
    from services.decoder_ingest.lap_tracker import LapTracker
    from services.decoder_ingest.session_manager import SessionManager

    asyncio.run(
        _seed_race_session(webapp_app, session_id="sess-prev-only", session_number=3)
    )

    lap_tracker = LapTracker(car_number_map={"AABBCCDDEEFF": "19"})
    sm = SessionManager.start_new()
    set_lap_tracker(lap_tracker)
    set_session_manager(sm, None)
    try:
        webapp_client.get("/api/auth/dev-login", follow_redirects=False)
        r = webapp_client.post(
            "/api/bindings", json={"car_number": "19", "session_number": 3}
        )
        assert r.status_code == 200
        assert r.json()["session_id"] == "sess-prev-only"

        me = webapp_client.get("/api/bindings/me").json()
        prev = [b for b in me["bindings"] if b["session_id"] == "sess-prev-only"]
        assert len(prev) == 1
        assert prev[0]["is_current_session"] is False

        cur = webapp_client.get("/api/bindings/current").json()
        assert cur["session_id"] == sm.current_session_id
        # 歷史節的綁定絕不能冒充本節
        if cur["bound"]:
            assert cur["binding"]["session_id"] == sm.current_session_id
            assert cur["binding"]["session_id"] != "sess-prev-only"
        else:
            assert cur["binding"] is None
    finally:
        set_lap_tracker(None)
        set_session_manager(None, None)


def test_nickname_update_and_me_reflects(webapp_client):
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    r = webapp_client.post("/api/profile/nickname", json={"nickname": "快車手"})
    assert r.status_code == 200
    assert r.json()["nickname"] == "快車手"
    assert r.json()["display_name"] == "快車手"

    me = webapp_client.get("/api/auth/me").json()["user"]
    assert me["nickname"] == "快車手"
    assert me["display_name"] == "快車手"


def test_leaderboard_alltime_section_before_session(webapp_client):
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    r = webapp_client.get("/leaderboard")
    assert r.status_code == 200
    body = r.text
    i_all = body.find("全站歷史最佳")
    i_sess = body.find("本節排行榜")
    assert i_all >= 0
    # 本節可能因無資料不渲染；有的話必須在全站之後
    if i_sess >= 0:
        assert i_all < i_sess


def test_profile_page_loads_with_bind_cta(webapp_client):
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    r = webapp_client.get("/profile")
    assert r.status_code == 200
    assert "綁定本節車號" in r.text
    assert "歷史綁定" in r.text
    assert "如何綁定節次與車號" in r.text
    assert "改暱稱" in r.text

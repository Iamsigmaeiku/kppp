"""services/webapp 的 auth/bindings 路由整合測試，走 AUTH_DEV_BYPASS 流程
（見 tests/conftest.py），不需要真的打 Google OAuth。"""

from __future__ import annotations


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
    r = webapp_client.post("/api/bindings", json={"transponder_id": "AABBCC"})
    assert r.status_code == 401


def test_bindings_fails_when_lap_tracker_not_initialized(webapp_client):
    # 這個測試 process 沒有跑 decoder_ingest 的 --with-dashboard 服務，
    # get_session_manager() 恆回傳 None，bind 應該明確地回 503 而不是
    # 假裝綁定成功。
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)
    r = webapp_client.post("/api/bindings", json={"transponder_id": "AABBCC"})
    assert r.status_code == 503

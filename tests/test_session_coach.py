"""session_coach.py：場次級 AI 教練報告不需要 CarBinding 就能觸發/查看
（跟 ai_coach.py 的個人綁定制端點對照）。測試環境沒有 AI_API_KEY，所以
驗證的是「有沒有被綁定門檻擋住」，不是真的呼叫 LLM。"""

from __future__ import annotations


def test_unauthenticated_post_requires_login(webapp_client):
    r = webapp_client.post(
        "/api/sessions/sess-does-not-exist/coach-reports/AABBCCDDEEFF"
    )
    assert r.status_code == 401


def test_unauthenticated_get_requires_login(webapp_client):
    r = webapp_client.get(
        "/api/sessions/sess-does-not-exist/coach-reports/AABBCCDDEEFF"
    )
    assert r.status_code == 401


def test_generate_report_no_binding_required(webapp_client):
    """跟個人綁定制的 /api/ai-coach/reports 不同：這裡完全沒有 CarBinding
    查詢——不管後面是因為沒設定 AI key（503）還是查無圈速（404）而停下來，
    都不應該是「你尚未綁定這節這台車」（403，ai_coach.py 那條路徑才有）。"""
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)

    r = webapp_client.post(
        "/api/sessions/sess-20260101-000000/coach-reports/AABBCCDDEEFF"
    )
    assert r.status_code != 403
    assert r.status_code in (202, 404, 503)


def test_status_endpoint_returns_none_when_no_report_exists(webapp_client):
    webapp_client.get("/api/auth/dev-login", follow_redirects=False)

    r = webapp_client.get(
        "/api/sessions/sess-nonexistent/coach-reports/AABBCCDDEEFF"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "none"
    assert body["report"] is None
    assert body["has_telemetry"] is False

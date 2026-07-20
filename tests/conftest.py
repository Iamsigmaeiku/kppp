"""Phase 2-4（services/webapp）測試共用 fixture。

services.webapp.app 的 FastAPI app 是 process 內單例（見 app.py 的
webapp_configured 旗標，避免重複掛 middleware/router）。所以整個 pytest
session 只在這裡呼叫一次 configure_app()，指向一個暫存的 SQLite 檔案，
其餘測試檔案共用同一個已設定好的 app；TestClient 本身則每個測試各自建立
一份，避免登入狀態（session cookie）跨測試互相污染。
"""

from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _webapp_test_env(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("webapp") / "test.sqlite3"
    os.environ["SQLITE_PATH"] = str(db_path)
    os.environ["AUTH_DEV_BYPASS"] = "true"
    os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
    os.environ.setdefault("INFLUX_URL", "http://localhost:8086")
    os.environ.setdefault("INFLUX_TOKEN", "kpp-dev-influx-token-change-me")
    os.environ.setdefault("INFLUX_ORG", "kpp")
    os.environ.setdefault("INFLUX_BUCKET", "decoder")
    os.environ.setdefault("TELEMETRY_INGEST_TOKEN", "test-telemetry-token")
    os.environ.setdefault("GRAFANA_EMBED_URL", "http://localhost:3000/grafana/d/kart-telemetry/kart-telemetry-f1?orgId=1&kiosk&theme=dark&refresh=2s")
    # 測試環境不應該真的打 Google/ExpTech，清掉這些讓 load_web_config()
    # 把 google/ai_coach 設為 None（dev bypass 讓登入照樣可測）。
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "AI_API_KEY"):
        os.environ.pop(key, None)
    yield


@pytest.fixture(scope="session")
def webapp_app(_webapp_test_env):
    from services.webapp.app import app, configure_app
    from services.webapp.db import init_db

    configure_app()
    asyncio.run(init_db(app.state.engine))
    return app


@pytest.fixture
def webapp_client(webapp_app):
    from fastapi.testclient import TestClient

    with TestClient(webapp_app) as client:
        yield client

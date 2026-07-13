"""Web 層設定：OAuth / SQLite / AI coach，與 decoder_ingest/config.py
（decoder/InfluxDB 設定）刻意分開，對應 services/webapp 與
services/decoder_ingest 的套件職責分工（見 app.py 說明）。共用同一份根目
錄 .env，load_dotenv 已在 decoder_ingest/config.py import 時執行過，這裡
再呼叫一次是安全的 no-op（override=False，檔案內容不變則結果相同）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


class WebConfigError(ValueError):
    """Web 層設定載入或驗證失敗。"""


@dataclass(frozen=True, slots=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AiCoachConfig:
    base_url: str
    api_key: str
    default_model: str
    auto_chat_model: str
    fast_model: str


@dataclass(frozen=True, slots=True)
class WebAppConfig:
    sqlite_path: Path
    secret_key: str
    auth_dev_bypass: bool
    avatar_upload_dir: Path
    google: GoogleOAuthConfig | None
    ai_coach: AiCoachConfig | None
    display_timezone: str
    telemetry_ingest_token: str
    grafana_embed_url: str


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load_web_config() -> WebAppConfig:
    secret_key = _env("SECRET_KEY")
    auth_dev_bypass = _env("AUTH_DEV_BYPASS", "false").lower() in ("1", "true", "yes")

    if not secret_key:
        if auth_dev_bypass:
            # 開發模式允許用一組固定但明顯是佔位的 key，方便本機/CI 沒設
            # SECRET_KEY 時仍能跑起來；正式環境一律要求真的設定過。
            secret_key = "dev-insecure-secret-key-do-not-use-in-production"
        else:
            raise WebConfigError("環境變數 SECRET_KEY 為必填（session cookie 簽章用）")

    google_client_id = _env("GOOGLE_CLIENT_ID")
    google_client_secret = _env("GOOGLE_CLIENT_SECRET")
    google_redirect_uri = _env("GOOGLE_REDIRECT_URI")
    google = None
    if google_client_id and google_client_secret and google_redirect_uri:
        google = GoogleOAuthConfig(
            client_id=google_client_id,
            client_secret=google_client_secret,
            redirect_uri=google_redirect_uri,
        )
    elif not auth_dev_bypass:
        raise WebConfigError(
            "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REDIRECT_URI 三者須同時設定"
            "（或設定 AUTH_DEV_BYPASS=true 以略過 Google 登入進行本機測試）"
        )

    ai_api_key = _env("AI_API_KEY")
    ai_base_url = _env("AI_BASE_URL")
    ai_coach = None
    if ai_api_key and ai_base_url:
        ai_coach = AiCoachConfig(
            base_url=ai_base_url,
            api_key=ai_api_key,
            default_model=_env("AI_DEFAULT_MODEL", "auto"),
            auto_chat_model=_env("AI_AUTO_CHAT_MODEL") or _env("AI_DEFAULT_MODEL", "auto"),
            fast_model=_env("AI_FAST_MODEL") or _env("AI_DEFAULT_MODEL", "auto"),
        )

    return WebAppConfig(
        sqlite_path=Path(_env("SQLITE_PATH", "services/webapp/kpp.sqlite3")),
        secret_key=secret_key,
        auth_dev_bypass=auth_dev_bypass,
        avatar_upload_dir=Path(_env("AVATAR_UPLOAD_DIR", "uploads/avatars")),
        google=google,
        ai_coach=ai_coach,
        display_timezone=_env("DISPLAY_TIMEZONE", "Asia/Taipei"),
        telemetry_ingest_token=_env("TELEMETRY_INGEST_TOKEN"),
        grafana_embed_url=_env(
            "GRAFANA_EMBED_URL",
            "http://localhost:3000/grafana/d/kart-telemetry/karting"
            "?orgId=1&kiosk=tv&theme=dark&refresh=2s",
        ),
    )

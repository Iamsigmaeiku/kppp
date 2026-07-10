"""services/webapp 是這次升級的組裝根：把 services/decoder_ingest.dashboard
既有的 FastAPI app（WebSocket 即時面板）當底層，再掛上這次新增的
auth/car_bindings/avatars/history/ai_coach 路由、SessionMiddleware、
Jinja2 樣板與靜態檔案。整個服務仍然只有一個 FastAPI instance、一個
uvicorn process，符合現有裸機 Windows 主機（無 Docker、無反向代理路徑
拆分）的部署現況——decoder_ingest 保持只管 TCP ingest + InfluxDB 寫入，
這裡才是使用者登入、瀏覽歷史、看 AI 教練報告的地方。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from services.decoder_ingest.config import load_influx_config
from services.decoder_ingest.dashboard import app as decoder_app, set_session_started_hook
from services.decoder_ingest.influx_reader import InfluxReader

from . import ai_coach, auth, avatars, car_bindings, history, pages, session_numbering, telemetry
from .auth_gate import RequireLoginMiddleware
from .config import load_web_config
from .db import make_engine, make_session_factory

app = decoder_app

WEBAPP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEBAPP_DIR / "templates"))
logger = logging.getLogger(__name__)


def _format_lap_time(value: float | None) -> str:
    if value is None:
        return "—"
    minutes, seconds = divmod(value, 60)
    if minutes:
        return f"{int(minutes)}:{seconds:06.3f}"
    return f"{seconds:.3f}s"


templates.env.filters["laptime"] = _format_lap_time


def _make_localtime_filter(tz: ZoneInfo):
    # SQLite 讀回來的 datetime 沒有 tzinfo，但存入時一律是 UTC（見 models.py
    # 的 _utcnow）；沒有 tzinfo 一律視為 UTC 再轉時區，避免顯示成 UTC 時刻
    # 卻被使用者誤讀成本地時間（見場次綁定時間顯示錯誤的回報）。
    def _localtime(value: datetime | None) -> str:
        if value is None:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")

    return _localtime


async def _ping_influx(reader: InfluxReader) -> None:
    """啟動時探活 Influx；失敗只打 log，不擋服務起來。"""
    try:
        client = await reader._get_client()
        # 輕量 health：打 /health 或跑空 query
        ready = await client.ping()
        if ready:
            logger.info("InfluxDB ping ok (%s)", reader._config.url)
        else:
            logger.error(
                "InfluxDB ping failed at %s — 排行榜/場次/遙測會無法連線。"
                "請確認 docker compose up -d 且 .env 的 INFLUX_* 正確。",
                reader._config.url,
            )
    except Exception:
        logger.exception(
            "InfluxDB unreachable at %s — 排行榜/場次會顯示「歷史資料庫目前無法連線」。"
            "請執行: docker compose up -d，並確認 INFLUX_TOKEN/ORG/BUCKET。",
            reader._config.url,
        )


def configure_app() -> None:
    """main.py 在啟動 uvicorn 前呼叫一次：把 web_config/session_factory/
    oauth 等放進 app.state，並掛上 middleware/routers/static mounts。用
    app.state 上的旗標擋掉重複呼叫（例如測試裡多次匯入這個模組）。
    """
    if getattr(app.state, "webapp_configured", False):
        return

    web_config = load_web_config()
    engine = make_engine(web_config.sqlite_path)
    session_factory = make_session_factory(engine)

    app.state.web_config = web_config
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.templates = templates
    app.state.telemetry_last = None
    templates.env.filters["localtime"] = _make_localtime_filter(
        ZoneInfo(web_config.display_timezone)
    )

    # 先加 Auth、後加 Session → 請求進來時 Session 先跑，session 才有值
    app.add_middleware(RequireLoginMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=web_config.secret_key,
        same_site="lax",
        https_only=not web_config.auth_dev_bypass,
    )

    app.state.oauth = auth.build_oauth(web_config.google) if web_config.google else None

    app.state.influx_reader = InfluxReader(load_influx_config())

    app.include_router(auth.router)
    app.include_router(car_bindings.router)
    app.include_router(avatars.router)
    app.include_router(history.router)
    app.include_router(ai_coach.router)
    app.include_router(telemetry.router)
    app.include_router(pages.router)

    web_config.avatar_upload_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/uploads/avatars",
        StaticFiles(directory=str(web_config.avatar_upload_dir)),
        name="avatars",
    )
    app.mount(
        "/webapp-static",
        StaticFiles(directory=str(WEBAPP_DIR / "static")),
        name="webapp-static",
    )

    # 場次每日編號（見 session_numbering.py）：decoder_ingest 開新場次時
    # 會透過這個 hook 通知，不需要知道 SQLite/編號邏輯本身怎麼運作。
    set_session_started_hook(session_numbering.on_session_started)

    async def _lifespan_ping() -> None:
        await _ping_influx(app.state.influx_reader)

    # 掛在既有 startup hooks 上（decoder_app 可能已有 lifespan；用事件最省事）
    @app.on_event("startup")
    async def _startup_influx_ping() -> None:
        await _lifespan_ping()

    app.state.webapp_configured = True

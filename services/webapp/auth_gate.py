"""未登入強制導向 /login。白名單：登入/OAuth、靜態資源、ESP32 ingest、
賽道 kiosk 唯讀端點（X-Kiosk-Token）。

SessionMiddleware 必須在這個 middleware「之後」註冊（Starlette 後加的先跑），
這樣進來時 request.session 已經可用。
"""

from __future__ import annotations

from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

KIOSK_TOKEN_HEADER = b"x-kiosk-token"

_WHITELIST_PREFIXES = (
    "/login",
    "/api/auth/",
    "/api/telemetry/ingest",  # ESP32 Bearer token，不是瀏覽器 session
    "/api/telemetry/frame-ingest",
    "/api/session/reset",  # 自帶 X-Session-Reset-Token，場務收場用
    "/health",  # keepalived / deploy 健康檢查
    "/version",  # deploy 確認 VIP 背後版本
    "/webapp-static/",
    "/docs",
    "/openapi.json",
    "/redoc",
)
# 注意：/grafana 不在白名單 — 未登入不能直接看儀表板；
# 登入後 /telemetry iframe 會帶 session cookie 進 /grafana。

# 賽道平板 kiosk：帶正確 X-Kiosk-Token 時可讀即時 WS + 排行／場次列表 JSON。
# 不放行寫入（archive／bindings／AI）與 Grafana／telemetry admin。
_KIOSK_WS_PATHS = frozenset({"/ws/laps", "/ws/positions"})
_KIOSK_EXACT_GET_PATHS = frozenset(
    {
        "/api/leaderboard",
        "/api/sessions",
    }
)


def _is_whitelisted(path: str) -> bool:
    if path in ("/login", "/favicon.ico"):
        return True
    return any(path.startswith(prefix) for prefix in _WHITELIST_PREFIXES)


def _header_value(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1").strip()
    return ""


def _kiosk_allowed(scope: Scope, path: str, expected_token: str) -> bool:
    if not expected_token:
        return False
    got = _header_value(scope, KIOSK_TOKEN_HEADER)
    if not got or got != expected_token:
        return False
    if scope["type"] == "websocket":
        return path in _KIOSK_WS_PATHS
    if scope["type"] == "http" and scope.get("method", "GET").upper() == "GET":
        return path in _KIOSK_EXACT_GET_PATHS
    return False


class RequireLoginMiddleware:
    """ASGI middleware：擋 HTML / API / WebSocket，未登入不可進站。"""

    def __init__(self, app: ASGIApp, kiosk_token: str = "") -> None:
        self.app = app
        self.kiosk_token = (kiosk_token or "").strip()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if _is_whitelisted(path):
            await self.app(scope, receive, send)
            return

        session = scope.get("session") or {}
        if session.get("user_id") is not None:
            await self.app(scope, receive, send)
            return

        if _kiosk_allowed(scope, path, self.kiosk_token):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # 拒絕未登入 WS：先 accept 再 close 會比較吵，直接不升級
            await send({"type": "websocket.close", "code": 4401})
            return

        request = Request(scope, receive)
        if path.startswith("/api/"):
            response: Response = JSONResponse(
                {"detail": "login required"}, status_code=401
            )
        else:
            nxt = path
            query = scope.get("query_string", b"").decode("latin-1")
            if query:
                nxt = f"{path}?{query}"
            response = RedirectResponse(
                url=f"/login?next={quote(nxt, safe='')}", status_code=302
            )
        await response(scope, receive, send)


class RequireLoginHTTPMiddleware(BaseHTTPMiddleware):
    """備用：僅 HTTP（測試用）。正式路徑用 RequireLoginMiddleware。"""

    def __init__(self, app: ASGIApp, kiosk_token: str = "") -> None:
        super().__init__(app)
        self.kiosk_token = (kiosk_token or "").strip()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_whitelisted(path):
            return await call_next(request)
        if request.session.get("user_id") is not None:
            return await call_next(request)
        if _kiosk_allowed(request.scope, path, self.kiosk_token):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        nxt = str(request.url.path)
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        return RedirectResponse(
            url=f"/login?next={quote(nxt, safe='')}", status_code=302
        )

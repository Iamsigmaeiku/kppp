"""Google OAuth 登入。使用 authlib 處理 PKCE/state，登入狀態存在簽章過的
httponly session cookie 裡（見 app.py 的 SessionMiddleware），只放
user_id，不放任何 token——這是單一網域的小型內部工具，不需要跨服務驗證
的 JWT。

AUTH_DEV_BYPASS 開啟時另外提供 /api/auth/dev-login，略過真的 Google
OAuth、直接用固定的假使用者登入，方便本機/CI 在沒有對外網域、沒辦法完成
真實 OAuth callback 的情況下，照樣把後面依賴登入狀態的功能跑起來。正式
環境的 .env 必須是 AUTH_DEV_BYPASS=false，config.py 沒設定 SECRET_KEY 時
也會直接拒絕啟動（除非同時開了 dev bypass），避免忘記關掉。
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .avatars import avatar_url_for
from .config import GoogleOAuthConfig
from .deps import get_current_user
from .models import User, public_display_name

router = APIRouter(prefix="/api/auth")
logger = logging.getLogger(__name__)

DEV_BYPASS_GOOGLE_SUB = "dev-bypass-fixed-user"


def _clear_oauth_states(session: dict) -> None:
    for key in list(session.keys()):
        if key.startswith("_state_"):
            session.pop(key)


def _oauth_interstitial(authorize_url: str) -> HTMLResponse:
    """200 + Set-Cookie 後再用 JS/meta 導向 Google。

    直接 302 到 Google 時，browser（尤其 Google 已登入、instant prompt=none
    bounce）常常還沒把 session cookie 寫穩就跳回 callback，authlib 就噴
    mismatching_state。先回 HTML 讓 cookie 落地再走。
    """
    safe_js = json.dumps(authorize_url)
    safe_attr = html.escape(authorize_url, quote=True)
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0;url={safe_attr}">
  <title>Redirecting…</title>
  <script>window.location.replace({safe_js});</script>
</head>
<body>
  <p>正在導向 Google 登入…</p>
  <p><a href="{safe_attr}">若未自動跳轉請點此</a></p>
</body>
</html>"""
    )


def build_oauth(google: GoogleOAuthConfig) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=google.client_id,
        client_secret=google.client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def user_public_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": public_display_name(user),
        "nickname": user.nickname,
        "google_display_name": user.display_name,
        "avatar_url": avatar_url_for(user),
    }


async def _upsert_user_from_userinfo(session: AsyncSession, userinfo: dict) -> User:
    result = await session.execute(
        select(User).where(User.google_sub == userinfo["sub"])
    )
    user = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if user is None:
        user = User(
            google_sub=userinfo["sub"],
            email=userinfo["email"],
            display_name=userinfo.get("name"),
            google_picture_url=userinfo.get("picture"),
            created_at=now,
            last_login_at=now,
        )
        session.add(user)
    else:
        user.email = userinfo["email"]
        user.display_name = userinfo.get("name")
        user.google_picture_url = userinfo.get("picture")
        user.last_login_at = now

    await session.commit()
    await session.refresh(user)
    return user


@router.get("/google/login")
async def google_login(request: Request):
    web_config = request.app.state.web_config
    if web_config.google is None:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")
    nxt = request.query_params.get("next")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        request.session["post_login_next"] = nxt

    _clear_oauth_states(request.session)

    oauth: OAuth = request.app.state.oauth
    redirect_uri = web_config.google.redirect_uri
    rv = await oauth.google.create_authorization_url(redirect_uri)
    await oauth.google.save_authorize_data(request, redirect_uri=redirect_uri, **rv)

    # authlib 會把整段 authorize URL 塞進 session；callback 用不到，拿掉縮小 cookie
    state = rv.get("state")
    if state:
        entry = request.session.get(f"_state_google_{state}")
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            entry["data"].pop("url", None)

    return _oauth_interstitial(rv["url"])


@router.get("/google/callback")
async def google_callback(request: Request):
    oauth: OAuth = request.app.state.oauth
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("google oauth callback failed: %s", exc)
        _clear_oauth_states(request.session)
        # 不要回 JSON 400：瀏覽器從 Google 跳回來時使用者只會看到爛錯誤頁
        return RedirectResponse(url="/login?error=oauth", status_code=302)

    userinfo = token.get("userinfo")
    if not userinfo or "sub" not in userinfo or "email" not in userinfo:
        logger.warning("google oauth incomplete userinfo: keys=%s", list(userinfo or {}))
        _clear_oauth_states(request.session)
        return RedirectResponse(url="/login?error=oauth", status_code=302)

    try:
        session_factory = request.app.state.session_factory
        async with session_factory() as session:
            user = await _upsert_user_from_userinfo(session, userinfo)
    except Exception:
        logger.exception("google oauth upsert user failed")
        return RedirectResponse(url="/login?error=oauth", status_code=302)

    request.session["user_id"] = user.id
    _clear_oauth_states(request.session)
    # 登入成功一律先到「我的資料」看綁定教學／綁車號；next 只留在 session 給未來擴充
    request.session.pop("post_login_next", None)
    return RedirectResponse(url="/profile")


@router.get("/dev-login")
async def dev_login(request: Request):
    web_config = request.app.state.web_config
    if not web_config.auth_dev_bypass:
        raise HTTPException(status_code=404, detail="not found")

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        user = await _upsert_user_from_userinfo(
            session,
            {
                "sub": DEV_BYPASS_GOOGLE_SUB,
                "email": "dev@example.local",
                "name": "Dev User",
                "picture": None,
            },
        )

    request.session["user_id"] = user.id
    # 開發模式也一律先進 profile（與 Google callback 一致）
    request.session.pop("post_login_next", None)
    return RedirectResponse(url="/profile")


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"status": "ok"}


@router.get("/me")
async def me(user: User | None = Depends(get_current_user)) -> dict:
    if user is None:
        return {"user": None}
    return {"user": user_public_dict(user)}

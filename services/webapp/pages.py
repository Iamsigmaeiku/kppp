"""登入頁 / 個人頁的 HTML route。刻意跟 auth.py（OAuth API 流程）、
car_bindings.py（綁定 API）分開，這裡只負責把資料組起來丟給樣板。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from services.decoder_ingest.dashboard import get_session_manager

from .avatars import avatar_url_for
from .deps import get_current_user, get_db
from .models import AiCoachReport, CarBinding, RaceSession, User, public_display_name
from .telemetry_access import can_view_telemetry
from .template_ctx import template_globals

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
@router.get("/dashboards", response_class=HTMLResponse)
@router.get("/dashboards/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request, user: User | None = Depends(get_current_user)
):
    # 雙保險：middleware 已會擋，這裡再強制未登入導向 /login
    if user is None:
        return RedirectResponse(url="/login?next=/", status_code=302)
    # 外部入口曾把公開網址做成 /dashboards，容易跟 Grafana 預設路由撞名；
    # 這幾個別名都導回同一個即時面板。
    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html", template_globals(user)
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(get_current_user)):
    nxt = request.query_params.get("next") or "/profile"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/profile"
    if user is not None:
        return RedirectResponse(url=nxt)

    # OAuth callback 回來後用 session 帶 next
    request.session["post_login_next"] = nxt

    web_config = request.app.state.web_config
    err = request.query_params.get("error")
    error_message = None
    if err == "oauth":
        error_message = "Google 登入失敗（連線中斷或狀態已過期），請再試一次。"

    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        template_globals(
            user,
            google_enabled=web_config.google is not None,
            dev_bypass=web_config.auth_dev_bypass,
            next=nxt,
            error_message=error_message,
        ),
    )


@router.get("/telemetry", response_class=HTMLResponse)
@router.get("/telemetry/", response_class=HTMLResponse)
async def telemetry_page(
    request: Request, user: User | None = Depends(get_current_user)
):
    if user is None:
        return RedirectResponse(url="/login?next=/telemetry", status_code=302)
    if not can_view_telemetry(user):
        return RedirectResponse(url="/", status_code=302)

    web_config = request.app.state.web_config
    last = getattr(request.app.state, "telemetry_last", None)
    return request.app.state.templates.TemplateResponse(
        request,
        "telemetry.html",
        template_globals(
            user,
            grafana_embed_url=web_config.grafana_embed_url,
            telemetry_last=last,
        ),
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        return RedirectResponse(url="/login?next=/profile")

    binding_result = await db.execute(
        select(CarBinding)
        .where(CarBinding.user_id == user.id)
        .options(selectinload(CarBinding.session))
        .order_by(CarBinding.bound_at.desc())
    )
    bindings = list(binding_result.scalars().all())

    sm = get_session_manager()
    current_session_id = sm.current_session_id if sm is not None else None
    current_race_session: RaceSession | None = None
    if current_session_id:
        current_race_session = await db.get(RaceSession, current_session_id)
        # 第一筆過線後 sm.numbered=True；若 SQLite 缺號就補一次
        if (
            sm is not None
            and sm.numbered
            and (current_race_session is None or current_race_session.session_number is None)
        ):
            from . import session_numbering

            await session_numbering.ensure_session_numbered(
                current_session_id, sm.session_started_at
            )
            await db.expire_all()
            current_race_session = await db.get(RaceSession, current_session_id)

    current_bindings = [
        b for b in bindings if current_session_id and b.session_id == current_session_id
    ]
    history_bindings = [
        b for b in bindings if not current_session_id or b.session_id != current_session_id
    ]

    report_result = await db.execute(
        select(AiCoachReport)
        .where(AiCoachReport.user_id == user.id)
        .order_by(AiCoachReport.created_at.desc())
    )
    reports_by_key: dict[str, dict] = {}
    for report in report_result.scalars().all():
        key = f"{report.session_id}::{report.transponder_id}"
        if key in reports_by_key:
            continue
        payload: dict = {
            "id": report.id,
            "status": report.status,
            "error_message": report.error_message,
            "report": None,
        }
        if report.status == "done" and report.response_json:
            try:
                payload["report"] = json.loads(report.response_json)
            except (TypeError, ValueError):
                pass
        reports_by_key[key] = payload

    return request.app.state.templates.TemplateResponse(
        request,
        "profile.html",
        template_globals(
            user,
            display_name=public_display_name(user),
            avatar_url=avatar_url_for(user),
            current_session_id=current_session_id,
            current_race_session=current_race_session,
            current_bindings=current_bindings,
            history_bindings=history_bindings,
            reports_by_key=reports_by_key,
            needs_bind=bool(current_session_id) and not current_bindings,
        ),
    )

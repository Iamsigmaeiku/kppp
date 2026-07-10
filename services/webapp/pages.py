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

from .avatars import avatar_url_for
from .deps import get_current_user, get_db
from .models import AiCoachReport, CarBinding, User

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
@router.get("/dashboards", response_class=HTMLResponse)
@router.get("/dashboards/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    # 外部入口曾把公開網址做成 /dashboards，容易跟 Grafana 預設路由撞名；
    # 這幾個別名都導回同一個即時面板，避免使用者因舊連結/錯路徑落到別的
    # 服務（沿用原本 decoder_ingest/dashboard.py 的行為）。
    return request.app.state.templates.TemplateResponse(request, "dashboard.html", {})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse(url="/profile")

    web_config = request.app.state.web_config
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        {
            "google_enabled": web_config.google is not None,
            "dev_bypass": web_config.auth_dev_bypass,
        },
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user is None:
        return RedirectResponse(url="/login")

    binding_result = await db.execute(
        select(CarBinding)
        .where(CarBinding.user_id == user.id)
        .options(selectinload(CarBinding.session))
        .order_by(CarBinding.bound_at.desc())
    )
    bindings = binding_result.scalars().all()

    report_result = await db.execute(
        select(AiCoachReport)
        .where(AiCoachReport.user_id == user.id)
        .order_by(AiCoachReport.created_at.desc())
    )
    reports_by_key: dict[str, dict] = {}
    for report in report_result.scalars().all():
        key = f"{report.session_id}::{report.transponder_id}"
        if key in reports_by_key:
            continue  # 已經是最新的一筆（依 created_at desc 排序取第一筆）
        try:
            reports_by_key[key] = json.loads(report.response_json)
        except (TypeError, ValueError):
            continue

    return request.app.state.templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "avatar_url": avatar_url_for(user),
            "bindings": bindings,
            "reports_by_key": reports_by_key,
        },
    )

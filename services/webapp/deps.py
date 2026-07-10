"""FastAPI dependencies：DB session、目前登入使用者（依 session cookie 裡
的 user_id 查表）。session_factory 由 app.py 在啟動時放進
`request.app.state.session_factory`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        return await session.get(User, user_id)


async def require_user(request: Request) -> User:
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    return user

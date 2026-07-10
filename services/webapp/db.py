"""SQLite (aiosqlite) 引擎/session factory。Schema 變更一律走 Alembic
migration（見 services/webapp/migrations/），init_db() 只在開發/測試時
提供「沒有 migration 也能建表」的捷徑，不取代 migration。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(sqlite_path: Path) -> AsyncEngine:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{sqlite_path}", echo=False)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """開發/測試用：直接依 models 目前的定義建表。正式環境應改用
    `alembic upgrade head`，讓 schema 變更有版本紀錄可追。
    """
    from . import models  # noqa: F401  (註冊 Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

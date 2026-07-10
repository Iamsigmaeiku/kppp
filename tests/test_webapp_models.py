"""SQLAlchemy models：驗證 car_bindings 的 UniqueConstraint(session_id,
transponder_id) 真的擋得住同一節同一支車被兩個不同使用者綁定。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from services.webapp import models
from services.webapp.db import Base, make_session_factory


async def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_session_factory(engine)


async def test_car_binding_unique_constraint_blocks_duplicate_transponder():
    session_factory = await _make_session_factory()
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        user_a = models.User(google_sub="a", email="a@example.com", created_at=now)
        user_b = models.User(google_sub="b", email="b@example.com", created_at=now)
        race_session = models.RaceSession(id="sess-1", started_at=now)
        session.add_all([user_a, user_b, race_session])
        await session.commit()

        session.add(
            models.CarBinding(
                user_id=user_a.id, session_id="sess-1", transponder_id="AABBCC"
            )
        )
        await session.commit()

        session.add(
            models.CarBinding(
                user_id=user_b.id, session_id="sess-1", transponder_id="AABBCC"
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_car_binding_allows_same_transponder_in_different_sessions():
    session_factory = await _make_session_factory()
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        user = models.User(google_sub="a", email="a@example.com", created_at=now)
        session.add_all(
            [
                user,
                models.RaceSession(id="sess-1", started_at=now),
                models.RaceSession(id="sess-2", started_at=now),
            ]
        )
        await session.commit()

        session.add(
            models.CarBinding(user_id=user.id, session_id="sess-1", transponder_id="AABBCC")
        )
        session.add(
            models.CarBinding(user_id=user.id, session_id="sess-2", transponder_id="AABBCC")
        )
        await session.commit()  # 不同 session_id，不應該衝突

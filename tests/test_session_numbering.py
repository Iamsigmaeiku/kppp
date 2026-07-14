"""session_numbering：開節即編號 + 列表顯示標籤。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def test_compute_display_labels_groups_by_local_day():
    from services.webapp.session_numbering import compute_display_labels

    tz = "Asia/Taipei"
    sessions = [
        SimpleNamespace(
            session_id="sess-later",
            started_at=datetime(2026, 7, 13, 10, 0, tzinfo=ZoneInfo("UTC")),
        ),
        SimpleNamespace(
            session_id="sess-earlier",
            started_at=datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("UTC")),
        ),
        SimpleNamespace(
            session_id="sess-next-day",
            started_at=datetime(2026, 7, 13, 16, 30, tzinfo=ZoneInfo("UTC")),
        ),
    ]
    labels = compute_display_labels(sessions, tz_name=tz)
    assert labels["sess-earlier"]["session_number"] == 1
    assert labels["sess-later"]["session_number"] == 2
    assert str(labels["sess-earlier"]["session_date"]) == "2026-07-13"
    assert labels["sess-next-day"]["session_number"] == 1
    assert str(labels["sess-next-day"]["session_date"]) == "2026-07-14"


async def test_on_session_started_assigns_number_immediately(webapp_app):
    from services.webapp import session_numbering
    from services.webapp.models import RaceSession

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    n1 = await session_numbering.on_session_started("sess-numbering-a", now)
    n2 = await session_numbering.on_session_started(
        "sess-numbering-b", now + timedelta(seconds=1)
    )
    assert n1 is not None and n2 is not None
    assert n2 == n1 + 1

    async with webapp_app.state.session_factory() as db:
        a = await db.get(RaceSession, "sess-numbering-a")
        assert a is not None
        assert a.session_number == n1
        assert a.session_date == now.date()


async def test_on_session_started_is_idempotent(webapp_app):
    from services.webapp import session_numbering

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    n1 = await session_numbering.on_session_started("sess-numbering-idem", now)
    n2 = await session_numbering.on_session_started("sess-numbering-idem", now)
    assert n1 is not None
    assert n1 == n2


async def test_backfill_returns_labels_even_with_sqlite_holes(webapp_app):
    """空殼烧掉 1..10 也不該讓列表退回裸 sess-…。"""
    from datetime import date as date_cls

    from services.webapp import session_numbering
    from services.webapp.models import RaceSession

    day = date_cls(2099, 3, 1)
    t0 = datetime(2099, 3, 1, 2, 0, 0, tzinfo=ZoneInfo("UTC"))

    async with webapp_app.state.session_factory() as db:
        for i in range(1, 11):
            db.add(
                RaceSession(
                    id=f"sess-burn-{i}",
                    started_at=t0 + timedelta(minutes=i),
                    ended_at=t0 + timedelta(minutes=i, seconds=30),
                    session_date=day,
                    session_number=i,
                )
            )
        await db.commit()

        sessions = [
            SimpleNamespace(
                session_id="sess-real-a",
                started_at=t0 + timedelta(hours=3),
                ended_at=t0 + timedelta(hours=3, minutes=20),
            ),
            SimpleNamespace(
                session_id="sess-real-b",
                started_at=t0 + timedelta(hours=4),
                ended_at=t0 + timedelta(hours=4, minutes=20),
            ),
        ]
        labels = await session_numbering.backfill_numbers_for_sessions(
            db, sessions, tz_name="Asia/Taipei"
        )

    assert labels["sess-real-a"]["session_number"] == 1
    assert labels["sess-real-b"]["session_number"] == 2


async def test_resolve_session_id_finds_matching_row(webapp_app):
    from services.webapp import session_numbering
    from services.webapp.models import RaceSession

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    n = await session_numbering.on_session_started("sess-numbering-resolve", now)
    assert n is not None
    async with webapp_app.state.session_factory() as db:
        row = await db.get(RaceSession, "sess-numbering-resolve")
        found = await session_numbering.resolve_session_id(
            db, session_date=row.session_date, session_number=row.session_number
        )
        assert found == "sess-numbering-resolve"


async def test_session_number_max_is_10():
    from services.webapp import session_numbering

    assert session_numbering.SESSION_NUMBER_MAX == 10

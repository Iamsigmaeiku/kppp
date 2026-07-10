"""session_numbering.py：場次每日編號（#1~#100，每天從 1 重新開始）。
走跟 tests/conftest.py 一樣的 webapp_app fixture（同一個已 configure_app()
的單例 app，指向暫存 SQLite），不需要真的啟動 decoder_ingest。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


async def test_on_session_started_assigns_sequential_numbers(webapp_app):
    from sqlalchemy import select

    from services.webapp import session_numbering
    from services.webapp.models import RaceSession

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    await session_numbering.on_session_started("sess-numbering-a", now)
    await session_numbering.on_session_started("sess-numbering-b", now + timedelta(seconds=1))

    async with webapp_app.state.session_factory() as db:
        result = await db.execute(
            select(RaceSession).where(
                RaceSession.id.in_(["sess-numbering-a", "sess-numbering-b"])
            )
        )
        rows = {rs.id: rs for rs in result.scalars().all()}

    assert rows["sess-numbering-a"].session_number is not None
    assert rows["sess-numbering-b"].session_number == rows["sess-numbering-a"].session_number + 1
    assert rows["sess-numbering-a"].session_date == now.date()


async def test_on_session_started_is_idempotent_per_session_id(webapp_app):
    from sqlalchemy import select

    from services.webapp import session_numbering
    from services.webapp.models import RaceSession

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    await session_numbering.on_session_started("sess-numbering-idem", now)
    async with webapp_app.state.session_factory() as db:
        first = await db.get(RaceSession, "sess-numbering-idem")
        first_number = first.session_number

    # 同一個 session_id 再被呼叫一次（模擬 hook 因為某種原因重複觸發）：
    # 編號不該被洗掉或換成別的數字。
    await session_numbering.on_session_started("sess-numbering-idem", now)
    async with webapp_app.state.session_factory() as db:
        second = await db.get(RaceSession, "sess-numbering-idem")

    assert second.session_number == first_number


async def test_resolve_session_id_finds_matching_row(webapp_app):
    from services.webapp import session_numbering

    tz = ZoneInfo(webapp_app.state.web_config.display_timezone)
    now = datetime.now(tz)

    await session_numbering.on_session_started("sess-numbering-resolve", now)
    async with webapp_app.state.session_factory() as db:
        from services.webapp.models import RaceSession

        row = await db.get(RaceSession, "sess-numbering-resolve")
        found = await session_numbering.resolve_session_id(
            db, session_date=row.session_date, session_number=row.session_number
        )
        assert found == "sess-numbering-resolve"

        missing = await session_numbering.resolve_session_id(
            db, session_date=row.session_date, session_number=9999
        )
        assert missing is None

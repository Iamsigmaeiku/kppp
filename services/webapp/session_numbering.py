"""場次每日編號（#1~#100，每天從 1 開始、滿 100 循環）：與 decoder_ingest
的 SessionManager 用 push-hook 串接（見 dashboard.py 的
set_session_started_hook），確保每一個真正開始的場次都會依時間先後拿到
編號，不管有沒有人真的去綁定或瀏覽這節——編號本身可重複使用（只是顯示
用的短標籤），真正對應資料的還是不會重複的 session_id。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RaceSession

logger = logging.getLogger(__name__)


async def on_session_started(session_id: str, started_at: datetime) -> None:
    """decoder_ingest 每次開啟新場次（服務啟動、或 archive_and_reset 換發
    新 session_id 後）都會呼叫一次；補建 sessions 列並指派當地日期的下一個
    編號。SQLite 寫入失敗（例如極端邊界情況）只記警告，不該讓
    decoder_ingest 的場次重置流程被 webapp 這邊的問題卡住。
    """
    # 延後到函式內部才 import，避免跟 app.py 在模組載入時互相 import
    # （app.py 的 configure_app() 會在啟動時把這個函式註冊成 hook）。
    from .app import app

    if not getattr(app.state, "webapp_configured", False):
        return

    session_factory = app.state.session_factory
    tz = ZoneInfo(app.state.web_config.display_timezone)

    at = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    local_date = at.astimezone(tz).date()

    async with session_factory() as db:
        try:
            existing = await db.get(RaceSession, session_id)
            if existing is not None and existing.session_number is not None:
                return  # 已經編過號了，避免同一個 session_id 被重複觸發時洗掉舊編號

            count = await db.scalar(
                select(func.count()).where(RaceSession.session_date == local_date)
            )
            number = ((count or 0) % 100) + 1

            if existing is None:
                db.add(
                    RaceSession(
                        id=session_id,
                        started_at=started_at,
                        session_date=local_date,
                        session_number=number,
                    )
                )
            else:
                existing.session_date = local_date
                existing.session_number = number

            await db.commit()
        except IntegrityError:
            # 極端邊界：同一天真的衝到第 101 節以上、或極少見的 race
            # condition 撞號。編號本身只是好看的顯示用標籤，寧可讓這節
            # 沒有編號（畫面上退回顯示原始 session_id），也不該讓場次
            # 重置流程整個失敗。
            await db.rollback()
            logger.warning(
                "session numbering skipped for %s (date=%s): unique constraint hit",
                session_id,
                local_date,
                exc_info=True,
            )
        except Exception:
            await db.rollback()
            logger.exception("session numbering failed for %s", session_id)


async def resolve_session_id(
    db: AsyncSession, *, session_date: date, session_number: int
) -> str | None:
    """依「今天第幾節」查回真正的 session_id，供 car_bindings.py 綁定時
    使用者輸入 #N 解析用。
    """
    result = await db.execute(
        select(RaceSession.id).where(
            RaceSession.session_date == session_date,
            RaceSession.session_number == session_number,
        )
    )
    return result.scalar_one_or_none()

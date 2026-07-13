"""使用者登入後，把自己綁到當節比賽裡的某支 transponder（車號），才能在
個人頁看到自己那節的圈速/AI 教練報告。

session_id 的權威來源是 decoder_ingest 的 SessionManager（同一個 process
內，透過 dashboard.py 新增的 get_session_manager() 取得）；SQLite 這邊的
sessions 表只在第一次被綁定時才補建對應列（見 _ensure_session_row），
decoder_ingest 本身不需要知道 SQLite 的存在，維持它只依賴 InfluxDB 的既有
設計（見 services/webapp/app.py 的套件職責分工說明）。

使用者輸入的 session_number（今天第幾節，見 session_numbering.py）只是
方便輸入用的短標籤，實際綁定仍然存 session_id（不會重複使用的
sess-YYYYMMDD-HHMMSS）——numbering 只負責把 #N 解析回對應的 session_id，
不會改變 CarBinding 本身的存法。留空 session_number 就沿用原本行為，
綁到目前正在進行的場次。

綁定嚴格 per-session：一人一節一台車；新節不會自動繼承上一節。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.dashboard import get_lap_tracker, get_session_manager

from . import session_numbering
from .deps import get_db, require_user
from .models import CarBinding, RaceSession, User

router = APIRouter(prefix="/api/bindings")


class BindRequest(BaseModel):
    car_number: str
    session_number: int | None = None


def _current_session_id() -> str:
    session_manager = get_session_manager()
    if session_manager is None:
        raise HTTPException(status_code=503, detail="lap tracker not initialized")
    return session_manager.current_session_id


def current_session_id_or_none() -> str | None:
    session_manager = get_session_manager()
    if session_manager is None:
        return None
    return session_manager.current_session_id


async def _resolve_session_id(
    db: AsyncSession, request: Request, session_number: int | None
) -> str:
    if session_number is None:
        return _current_session_id()

    web_config = request.app.state.web_config
    today = datetime.now(ZoneInfo(web_config.display_timezone)).date()
    session_id = await session_numbering.resolve_session_id(
        db, session_date=today, session_number=session_number
    )
    if session_id is None:
        raise HTTPException(
            status_code=404, detail=f"找不到今天第 {session_number} 節場次，請確認場次編號"
        )
    return session_id


async def _ensure_session_row(db: AsyncSession, session_id: str) -> None:
    existing = await db.get(RaceSession, session_id)
    if existing is not None:
        return
    session_manager = get_session_manager()
    started_at = (
        session_manager.session_started_at
        if session_manager is not None
        else datetime.now(timezone.utc)
    )
    db.add(RaceSession(id=session_id, started_at=started_at))
    await db.flush()


def _transponder_id_for_car(car_number: str) -> str | None:
    lap_tracker = get_lap_tracker()
    if lap_tracker is None:
        return None
    return lap_tracker.transponder_id_for_car(car_number)


@router.post("")
async def bind_car(
    body: BindRequest,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    car_number = body.car_number.strip()
    if not car_number:
        raise HTTPException(status_code=400, detail="車號不可為空")

    session_id = await _resolve_session_id(db, request, body.session_number)

    # 使用者只認得車號，實際綁定仍以 transponder_id 為準（同一顆晶片才是
    # 真正的計時識別碼）；車號不存在於目前的 CAR_NUMBER_MAP 代表現場還沒
    # 登記這個車號，明確回錯誤而不是假裝綁定成功。
    transponder_id = _transponder_id_for_car(car_number)
    if transponder_id is None:
        raise HTTPException(status_code=404, detail="找不到這個車號，請確認車號是否正確")

    await _ensure_session_row(db, session_id)

    tid_result = await db.execute(
        select(CarBinding).where(
            CarBinding.session_id == session_id,
            CarBinding.transponder_id == transponder_id,
        )
    )
    existing_tid = tid_result.scalar_one_or_none()
    if existing_tid is not None and existing_tid.user_id != user.id:
        raise HTTPException(status_code=409, detail="這個車號這節已經被其他人綁定")

    user_result = await db.execute(
        select(CarBinding).where(
            CarBinding.user_id == user.id,
            CarBinding.session_id == session_id,
        )
    )
    existing_user = user_result.scalar_one_or_none()

    if existing_user is not None:
        # 同一人同一節換車：更新既有列，不開新綁定、不繼承其他節
        if existing_tid is not None and existing_tid.id != existing_user.id:
            raise HTTPException(status_code=409, detail="這個車號這節已經被其他人綁定")
        existing_user.transponder_id = transponder_id
        existing_user.car_number = car_number
        existing_user.bound_at = datetime.now(timezone.utc)
        await db.commit()
        return {
            "status": "ok",
            "session_id": session_id,
            "transponder_id": transponder_id,
            "car_number": existing_user.car_number,
            "updated": True,
        }

    if existing_tid is not None:
        return {
            "status": "ok",
            "session_id": session_id,
            "transponder_id": transponder_id,
            "car_number": existing_tid.car_number,
        }

    binding = CarBinding(
        user_id=user.id,
        session_id=session_id,
        transponder_id=transponder_id,
        car_number=car_number,
    )
    db.add(binding)
    await db.commit()

    return {
        "status": "ok",
        "session_id": session_id,
        "transponder_id": transponder_id,
        "car_number": binding.car_number,
    }


@router.get("/me")
async def my_bindings(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(CarBinding)
        .where(CarBinding.user_id == user.id)
        .order_by(CarBinding.bound_at.desc())
    )
    bindings = result.scalars().all()
    live_sid = current_session_id_or_none()
    return {
        "current_session_id": live_sid,
        "bindings": [
            {
                "session_id": b.session_id,
                "transponder_id": b.transponder_id,
                "car_number": b.car_number,
                "bound_at": b.bound_at.isoformat(),
                "is_current_session": live_sid is not None and b.session_id == live_sid,
            }
            for b in bindings
        ],
    }


@router.get("/current")
async def current_session_binding(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """本節是否已綁定——給 topbar / profile banner 用，絕不回傳上一節。"""
    live_sid = current_session_id_or_none()
    if live_sid is None:
        return {"session_id": None, "bound": False, "binding": None}

    result = await db.execute(
        select(CarBinding).where(
            CarBinding.user_id == user.id,
            CarBinding.session_id == live_sid,
        )
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        return {"session_id": live_sid, "bound": False, "binding": None}
    return {
        "session_id": live_sid,
        "bound": True,
        "binding": {
            "session_id": binding.session_id,
            "transponder_id": binding.transponder_id,
            "car_number": binding.car_number,
        },
    }

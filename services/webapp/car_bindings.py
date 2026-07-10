"""使用者登入後，把自己綁到當節比賽裡的某支 transponder（車號），才能在
個人頁看到自己那節的圈速/AI 教練報告。

session_id 的權威來源是 decoder_ingest 的 SessionManager（同一個 process
內，透過 dashboard.py 新增的 get_session_manager() 取得）；SQLite 這邊的
sessions 表只在第一次被綁定時才補建對應列（見 _ensure_session_row），
decoder_ingest 本身不需要知道 SQLite 的存在，維持它只依賴 InfluxDB 的既有
設計（見 services/webapp/app.py 的套件職責分工說明）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.decoder_ingest.dashboard import get_lap_tracker, get_session_manager

from .deps import get_db, require_user
from .models import CarBinding, RaceSession, User

router = APIRouter(prefix="/api/bindings")


class BindRequest(BaseModel):
    car_number: str


def _current_session_id() -> str:
    session_manager = get_session_manager()
    if session_manager is None:
        raise HTTPException(status_code=503, detail="lap tracker not initialized")
    return session_manager.current_session_id


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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    car_number = body.car_number.strip()
    if not car_number:
        raise HTTPException(status_code=400, detail="車號不可為空")

    session_id = _current_session_id()

    # 使用者只認得車號，實際綁定仍以 transponder_id 為準（同一顆晶片才是
    # 真正的計時識別碼）；車號不存在於目前的 CAR_NUMBER_MAP 代表現場還沒
    # 登記這個車號，明確回錯誤而不是假裝綁定成功。
    transponder_id = _transponder_id_for_car(car_number)
    if transponder_id is None:
        raise HTTPException(status_code=404, detail="找不到這個車號，請確認車號是否正確")

    await _ensure_session_row(db, session_id)

    result = await db.execute(
        select(CarBinding).where(
            CarBinding.session_id == session_id,
            CarBinding.transponder_id == transponder_id,
        )
    )
    existing_binding = result.scalar_one_or_none()

    if existing_binding is not None:
        if existing_binding.user_id != user.id:
            raise HTTPException(status_code=409, detail="這個車號這節已經被其他人綁定")
        return {
            "status": "ok",
            "session_id": session_id,
            "transponder_id": transponder_id,
            "car_number": existing_binding.car_number,
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
    return {
        "bindings": [
            {
                "session_id": b.session_id,
                "transponder_id": b.transponder_id,
                "car_number": b.car_number,
                "bound_at": b.bound_at.isoformat(),
            }
            for b in bindings
        ]
    }

"""SQLite ORM models：users（Google 登入的會員資料）、sessions（場次，
與 InfluxDB session_archive 用同一組 session_id 對應）、car_bindings
（使用者與當節車號的綁定）、ai_coach_reports（AI 教練報告）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def public_display_name(user: "User") -> str:
    """暱稱優先，否則 Google display_name，再否則 email。"""
    for candidate in (user.nickname, user.display_name, user.email):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return "?"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    google_sub: Mapped[str] = mapped_column(unique=True, index=True)
    email: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None] = mapped_column(default=None)
    # 使用者自訂暱稱；Google 登入不會覆寫這個欄位。
    nickname: Mapped[str | None] = mapped_column(default=None)
    google_picture_url: Mapped[str | None] = mapped_column(default=None)
    # 本機上傳頭像的相對路徑；為 None 時前端 fallback 用 google_picture_url。
    avatar_path: Mapped[str | None] = mapped_column(default=None)
    # 保留給未來 TKS Line 會員 API 串接（見 line_stub.py），目前不使用。
    line_user_id: Mapped[str | None] = mapped_column(unique=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)

    car_bindings: Mapped[list["CarBinding"]] = relationship(back_populates="user")


class RaceSession(Base):
    """對應 InfluxDB session_archive 裡的同一個 session_id；SQLite 這邊只
    存給 UI/綁定用的中繼資料，圈速本身的權威資料仍在 InfluxDB。

    session_date/session_number 是給人看的「今天第幾節」標籤（見
    session_numbering.py），每天從 #1 重新開始、最多到 #100 循環——這只是
    可重複使用的顯示用短標籤，不是永久唯一 ID，真正的資料一律還是用
    session_id（不會重複的 sess-YYYYMMDD-HHMMSS）對應。
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "session_date", "session_number", name="uq_session_date_number"
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)  # e.g. "sess-20260710-143200"
    label: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column()
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    reset_trigger: Mapped[str] = mapped_column(default="manual")  # 'manual' | 'auto_idle'
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    session_date: Mapped[date | None] = mapped_column(default=None)
    session_number: Mapped[int | None] = mapped_column(default=None)

    car_bindings: Mapped[list["CarBinding"]] = relationship(back_populates="session")


class CarBinding(Base):
    """使用者登入後，把自己跟某一節比賽裡的某支 transponder 綁在一起，
    之後才能在個人頁看到自己那節的圈速/AI 教練報告。

    綁定嚴格 per-session：一人一節只能綁一台車；新節必須重新綁定，
    不會自動繼承上一節。
    """

    __tablename__ = "car_bindings"
    __table_args__ = (
        UniqueConstraint("session_id", "transponder_id", name="uq_session_transponder"),
        UniqueConstraint("user_id", "session_id", name="uq_user_session"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    transponder_id: Mapped[str] = mapped_column()
    car_number: Mapped[str | None] = mapped_column(default=None)
    bound_at: Mapped[datetime] = mapped_column(default=_utcnow)

    user: Mapped["User"] = relationship(back_populates="car_bindings")
    session: Mapped["RaceSession"] = relationship(back_populates="car_bindings")


class AiCoachReport(Base):
    """AI 教練報告：背景產生，status 為 pending/running/done/failed。
    同一 (user, session, tid) 可有多筆；UI 取最新一筆。
    """

    __tablename__ = "ai_coach_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str] = mapped_column()
    transponder_id: Mapped[str] = mapped_column()
    model: Mapped[str] = mapped_column(default="")
    prompt_version: Mapped[str] = mapped_column(default="")
    response_json: Mapped[str] = mapped_column(default="")  # done 時才有完整 JSON
    status: Mapped[str] = mapped_column(default="done")  # pending|running|done|failed
    error_message: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

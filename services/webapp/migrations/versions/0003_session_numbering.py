"""session numbering: daily #1-100 cycling label

Revision ID: 0003_session_numbering
Revises: 0002_ai_coach_reports
Create Date: 2026-07-10
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "0003_session_numbering"
down_revision = "0002_ai_coach_reports"
branch_labels = None
depends_on = None

# 對應 services/webapp/config.py 的 DISPLAY_TIMEZONE 預設值。遷移執行時機
# 不保證能讀到當下 .env 設定，直接寫死預設時區做回填，跟現行預設一致；
# 之後新場次一律由 session_numbering.py 用實際設定值計算，不受這裡影響。
DISPLAY_TZ = ZoneInfo("Asia/Taipei")


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("session_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("session_number", sa.Integer(), nullable=True))

    # 回填既有場次：依 started_at 換算成本地日期、同一天內依時間先後
    # 編號（1-based，滿 100 循環回 1——現實中不太可能同一天真的跑到
    # 第 101 節，但回填邏輯本身還是遵守同一套循環規則以求一致）。
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, started_at FROM sessions ORDER BY started_at ASC")
    ).fetchall()

    counters: dict[str, int] = {}
    for row in rows:
        started_at = row.started_at
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        local_date = started_at.astimezone(DISPLAY_TZ).date()
        key = local_date.isoformat()
        counters[key] = counters.get(key, 0) + 1
        number = ((counters[key] - 1) % 100) + 1
        conn.execute(
            sa.text(
                "UPDATE sessions SET session_date = :d, session_number = :n "
                "WHERE id = :id"
            ),
            {"d": local_date.isoformat(), "n": number, "id": row.id},
        )

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_session_date_number", ["session_date", "session_number"]
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint("uq_session_date_number", type_="unique")
        batch_op.drop_column("session_number")
        batch_op.drop_column("session_date")

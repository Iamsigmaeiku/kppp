"""session_ai_coach_reports table (no user_id — session-wide, no binding required)

Revision ID: 0006_session_ai_coach_reports
Revises: 0005_user_is_admin
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_session_ai_coach_reports"
down_revision = "0005_user_is_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_ai_coach_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.String, nullable=False),
        sa.Column("transponder_id", sa.String, nullable=False),
        sa.Column("car_number", sa.String, nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False, server_default=""),
        sa.Column("model", sa.String, nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String, nullable=False, server_default=""),
        sa.Column("response_json", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_session_ai_coach_reports_session_tid",
        "session_ai_coach_reports",
        ["session_id", "transponder_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_ai_coach_reports_session_tid",
        table_name="session_ai_coach_reports",
    )
    op.drop_table("session_ai_coach_reports")

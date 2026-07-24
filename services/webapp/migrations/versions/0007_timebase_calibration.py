"""timebase_calibration table

Revision ID: 0007_timebase_calibration
Revises: 0006_session_ai_coach_reports
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_timebase_calibration"
down_revision = "0006_session_ai_coach_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "timebase_calibration",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.String, nullable=False),
        sa.Column("transponder_id", sa.String, nullable=False),
        sa.Column("offset_sec", sa.Float, nullable=False),
        sa.Column("drift_sec_per_hour", sa.Float, nullable=False, server_default="0"),
        sa.Column("matched_pairs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("residual_std_sec", sa.Float, nullable=False, server_default="0"),
        sa.Column("quality", sa.String, nullable=False, server_default="failed"),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "transponder_id", name="uq_timebase_session_tid"
        ),
    )
    op.create_index(
        "ix_timebase_calibration_session_id",
        "timebase_calibration",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_timebase_calibration_session_id",
        table_name="timebase_calibration",
    )
    op.drop_table("timebase_calibration")

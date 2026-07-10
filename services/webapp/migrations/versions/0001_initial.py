"""initial schema: users, sessions, car_bindings

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("google_sub", sa.String, nullable=False, unique=True),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("display_name", sa.String, nullable=True),
        sa.Column("google_picture_url", sa.String, nullable=True),
        sa.Column("avatar_path", sa.String, nullable=True),
        sa.Column("line_user_id", sa.String, nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("label", sa.String, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_trigger", sa.String, nullable=False, server_default="manual"),
        sa.Column(
            "created_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    op.create_table(
        "car_bindings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.String, sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("transponder_id", sa.String, nullable=False),
        sa.Column("car_number", sa.String, nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "transponder_id", name="uq_session_transponder"
        ),
    )


def downgrade() -> None:
    op.drop_table("car_bindings")
    op.drop_table("sessions")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_table("users")

"""nickname, one-binding-per-user-session, ai report status

Revision ID: 0004_nickname_binding_ai_status
Revises: 0003_session_numbering
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_nickname_binding_ai_status"
down_revision = "0003_session_numbering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("nickname", sa.String(), nullable=True))

    with op.batch_alter_table("car_bindings") as batch_op:
        batch_op.create_unique_constraint("uq_user_session", ["user_id", "session_id"])

    with op.batch_alter_table("ai_coach_reports") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(), nullable=False, server_default="done")
        )
        batch_op.add_column(sa.Column("error_message", sa.String(), nullable=True))
        batch_op.alter_column(
            "model",
            existing_type=sa.String(),
            nullable=False,
            server_default="",
        )
        batch_op.alter_column(
            "prompt_version",
            existing_type=sa.String(),
            nullable=False,
            server_default="",
        )
        batch_op.alter_column(
            "response_json",
            existing_type=sa.Text(),
            nullable=False,
            server_default="",
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_coach_reports") as batch_op:
        batch_op.drop_column("error_message")
        batch_op.drop_column("status")

    with op.batch_alter_table("car_bindings") as batch_op:
        batch_op.drop_constraint("uq_user_session", type_="unique")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("nickname")

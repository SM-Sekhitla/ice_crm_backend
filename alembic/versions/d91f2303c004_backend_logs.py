"""Structured API and worker operational logs."""

import sqlalchemy as sa

from alembic import op

revision = "d91f2303c004"
down_revision = "c84e1202b003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "backend_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("message", sa.String(200), nullable=False),
        sa.Column("request_id", sa.String(36)),
        sa.Column("method", sa.String(10)),
        sa.Column("route", sa.String(200)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("error_type", sa.String(100)),
        sa.Column("outcome", sa.String(20)),
    )
    op.create_index("ix_backend_logs_created_at", "backend_logs", ["created_at"])


def downgrade():
    op.drop_index("ix_backend_logs_created_at", table_name="backend_logs")
    op.drop_table("backend_logs")

"""Retained user identities for audit-safe account deletion."""

import sqlalchemy as sa

from alembic import op

revision = "c84e1202b003"
down_revision = "f25c1101a001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("users", "deleted_at")

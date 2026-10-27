"""User invitations and first-login onboarding."""

import sqlalchemy as sa

from alembic import op

revision = "f25c1101a001"
down_revision = "ee3bce2121a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("password_set", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column(
        "users", sa.Column("onboarding_completed", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.create_table(
        "user_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("digest", sa.String(64), unique=True, nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("delivery_status", sa.String(20), nullable=False),
        sa.Column("delivery_reason", sa.String(500), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_invitations_user_id", "user_invitations", ["user_id"])
    op.create_index("ix_user_invitations_delivery_status", "user_invitations", ["delivery_status"])


def downgrade():
    op.drop_table("user_invitations")
    op.drop_column("users", "onboarding_completed")
    op.drop_column("users", "password_set")

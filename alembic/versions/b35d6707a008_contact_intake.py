"""Personal QR contact intake links and capture attribution."""

import sqlalchemy as sa
from alembic import op

revision = "b35d6707a008"
down_revision = "a24c5606f007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("contacts", sa.Column("captured_by", sa.String(36), nullable=True))
    op.create_foreign_key("fk_contacts_captured_by", "contacts", "users", ["captured_by"], ["id"], ondelete="SET NULL")
    op.create_table(
        "contact_intake_links",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("submission_count", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("contact_intake_links")
    op.drop_constraint("fk_contacts_captured_by", "contacts", type_="foreignkey")
    op.drop_column("contacts", "captured_by")

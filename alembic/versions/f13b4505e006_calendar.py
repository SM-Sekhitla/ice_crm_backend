"""Shared diary events with organiser ownership and optimistic locking."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f13b4505e006"
down_revision = "e02a3404d005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("location", sa.String(250), nullable=False),
        sa.Column("meeting_url", sa.String(1000), nullable=False),
        sa.Column("contact_id", sa.String(36), sa.ForeignKey("contacts.id", ondelete="SET NULL")),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id", ondelete="SET NULL")),
        sa.Column("participant_ids", postgresql.JSONB(), nullable=False),
        sa.Column("reminder_minutes", sa.Integer()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("ends_at > starts_at", name="calendar_event_interval"),
    )
    op.create_index("ix_calendar_events_starts_at", "calendar_events", ["starts_at"])
    op.create_index("ix_calendar_events_ends_at", "calendar_events", ["ends_at"])


def downgrade():
    op.drop_table("calendar_events")

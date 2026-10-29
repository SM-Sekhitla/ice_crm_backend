"""Store signature images durably for public email rendering."""

import sqlalchemy as sa
from alembic import op

revision = "a24c5606f007"
down_revision = "f13b4505e006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_images",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("content_type", sa.String(30), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("email_images")

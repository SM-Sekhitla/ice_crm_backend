"""Module/service labels and runtime diagnostic details."""

import sqlalchemy as sa

from alembic import op

revision = "e02a3404d005"
down_revision = "d91f2303c004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("backend_logs", sa.Column("module", sa.String(120), nullable=True))
    op.add_column("backend_logs", sa.Column("service", sa.String(32), nullable=True))
    op.add_column("backend_logs", sa.Column("details", sa.Text(), nullable=True))
    op.alter_column(
        "backend_logs",
        "message",
        type_=sa.String(2000),
        existing_type=sa.String(200),
        existing_nullable=False,
    )
    op.execute(
        "UPDATE backend_logs SET service = CASE WHEN source = 'worker' THEN 'email-worker' ELSE 'api' END, module = CASE WHEN source = 'worker' THEN 'app.services.worker' ELSE 'app.middleware.security' END"
    )
    op.create_index("ix_backend_logs_service_id", "backend_logs", ["service", "id"])
    op.create_index("ix_backend_logs_module_id", "backend_logs", ["module", "id"])


def downgrade():
    op.drop_index("ix_backend_logs_module_id", table_name="backend_logs")
    op.drop_index("ix_backend_logs_service_id", table_name="backend_logs")
    op.execute("UPDATE backend_logs SET message = left(message, 200)")
    op.alter_column(
        "backend_logs",
        "message",
        type_=sa.String(200),
        existing_type=sa.String(2000),
        existing_nullable=False,
    )
    op.drop_column("backend_logs", "details")
    op.drop_column("backend_logs", "service")
    op.drop_column("backend_logs", "module")

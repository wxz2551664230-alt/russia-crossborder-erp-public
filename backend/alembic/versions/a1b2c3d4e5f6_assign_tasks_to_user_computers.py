"""Bind each shared task to its originating user's selected computer."""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("created_by", sa.String(32), server_default="", nullable=False))
    op.add_column("jobs", sa.Column("target_worker_id", sa.String(100), server_default="", nullable=False))
    op.create_index("ix_jobs_target_worker_id", "jobs", ["target_worker_id"])
    op.add_column("worker_nodes", sa.Column("owner_user_id", sa.String(32), server_default="", nullable=False))
    op.create_index("ix_worker_nodes_owner_user_id", "worker_nodes", ["owner_user_id"])
    op.add_column("erp_user_sessions", sa.Column("worker_id", sa.String(100), server_default="", nullable=False))


def downgrade():
    op.drop_column("erp_user_sessions", "worker_id")
    op.drop_index("ix_worker_nodes_owner_user_id", table_name="worker_nodes")
    op.drop_column("worker_nodes", "owner_user_id")
    op.drop_index("ix_jobs_target_worker_id", table_name="jobs")
    op.drop_column("jobs", "target_worker_id")
    op.drop_column("jobs", "created_by")

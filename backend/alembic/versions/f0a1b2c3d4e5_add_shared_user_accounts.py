"""Shared ERP accounts, revocable browser sessions and operation audit."""
from alembic import op
import sqlalchemy as sa

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("erp_users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("erp_user_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("erp_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_erp_user_sessions_user_id", "erp_user_sessions", ["user_id"])
    op.create_index("ix_erp_user_sessions_expires_at", "erp_user_sessions", ["expires_at"])
    op.create_table("erp_auth_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False))
    op.create_table("erp_auth_control",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False))
    op.create_table("erp_user_audit",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("actor_name", sa.String(64), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("target", sa.String(500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_erp_user_audit_created_at", "erp_user_audit", ["created_at"])


def downgrade():
    # Operator must export account/audit records before using a downgrade.
    op.drop_table("erp_user_audit")
    op.drop_table("erp_auth_control")
    op.drop_table("erp_auth_throttles")
    op.drop_table("erp_user_sessions")
    op.drop_table("erp_users")

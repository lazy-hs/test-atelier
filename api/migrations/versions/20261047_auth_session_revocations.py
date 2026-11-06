"""Persist revocations for browser session nonces."""

from alembic import op
import sqlalchemy as sa


revision = "20261047_auth_session_revocations"
down_revision = "20261046_conversion_output_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("session_revocations"):
        op.create_table(
            "session_revocations",
            sa.Column("nonce_hash", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_session_revocations_user_id", "session_revocations", ["user_id"])
        op.create_index("ix_session_revocations_expires_at", "session_revocations", ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("session_revocations"):
        op.drop_index("ix_session_revocations_expires_at", table_name="session_revocations")
        op.drop_index("ix_session_revocations_user_id", table_name="session_revocations")
        op.drop_table("session_revocations")

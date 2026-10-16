"""Baseline existing schema and new workstation modules.

Revision ID: 20261015_initial
Revises:
"""

from alembic import op

from api.migrations.legacy_schema import metadata

revision = "20261015_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # This baseline is additive for existing SQLite installs created by create_all.
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Downgrading the baseline would remove user data; restore a database backup instead")

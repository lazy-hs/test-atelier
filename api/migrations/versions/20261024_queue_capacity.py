"""Reserve durable worker capacity atomically across API replicas."""

import sqlalchemy as sa
from alembic import op


revision = "20261024_queue_capacity"
down_revision = "20261023_job_leases"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, name: str) -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns(table)}
    if name not in existing:
        op.add_column(table, sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))


def _add_text_if_missing(table: str, name: str, default: str = "") -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns(table)}
    if name not in existing:
        op.add_column(table, sa.Column(name, sa.Text(), nullable=False, server_default=default))


def upgrade():
    _add_text_if_missing("ai_settings", "providers_json", "[]")
    _add_if_missing("ai_jobs", "queue_reserved")
    _add_if_missing("suggestion_jobs", "queue_reserved")
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("ai_queue_state"):
        op.create_table(
            "ai_queue_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("active_jobs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.String(40), nullable=False, server_default=""),
        )
    active = bind.execute(sa.text(
        "SELECT (SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running')) "
        "+ (SELECT COUNT(*) FROM suggestion_jobs WHERE status IN ('queued','running'))"
    )).scalar() or 0
    ai_jobs = sa.table("ai_jobs", sa.column("status"), sa.column("queue_reserved"))
    suggestion_jobs = sa.table("suggestion_jobs", sa.column("status"), sa.column("queue_reserved"))
    bind.execute(sa.update(ai_jobs).where(ai_jobs.c.status.in_(["queued", "running"])).values(queue_reserved=True))
    bind.execute(sa.update(suggestion_jobs).where(suggestion_jobs.c.status.in_(["queued", "running"])).values(queue_reserved=True))
    if bind.execute(sa.text("SELECT 1 FROM ai_queue_state WHERE id = 1")).scalar() is None:
        bind.execute(sa.text(
            "INSERT INTO ai_queue_state (id, active_jobs, updated_at) VALUES (1, :active, :updated)"
        ), {"active": int(active), "updated": ""})
    else:
        bind.execute(sa.text(
            "UPDATE ai_queue_state SET active_jobs = :active, updated_at = :updated WHERE id = 1"
        ), {"active": int(active), "updated": ""})


def downgrade():
    raise RuntimeError("Queue capacity reservations are additive; restore a database backup instead")

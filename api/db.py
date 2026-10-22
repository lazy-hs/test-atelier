import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import QueuePool
from .observability import env_float, env_int


def utcnow():
    return datetime.now(timezone.utc).isoformat()


# Keep the local SQLite file with the rest of the runtime data. Deployments
# using PostgreSQL (or an explicit DATABASE_URL) are unaffected.
_DEFAULT_DATABASE_URL = "sqlite:///./data/ai_studio.db"
database_url = os.getenv("DATABASE_URL", _DEFAULT_DATABASE_URL)
if database_url == _DEFAULT_DATABASE_URL:
    # Alembic runs before API startup and therefore cannot rely on the API's
    # storage initialization to create the SQLite parent directory.
    Path("data").mkdir(parents=True, exist_ok=True)
_is_sqlite = database_url.startswith("sqlite")
database_kind = "sqlite" if _is_sqlite else "postgresql" if database_url.startswith(("postgresql", "postgres")) else "unknown"
_is_memory_sqlite = _is_sqlite and (":memory:" in database_url or database_url.rstrip("/") in {"sqlite:", "sqlite+pysqlite:"})
_engine_options = {"pool_pre_ping": True}
if _is_sqlite:
    # AI and suggestion workers run in a thread pool.  SQLite's default
    # thread check rejects a pooled connection reused by another worker.
    _sqlite_timeout = env_float("AI_STUDIO_SQLITE_TIMEOUT", 30, 1.0)
    _engine_options["connect_args"] = {"check_same_thread": False, "timeout": _sqlite_timeout}
    if _is_memory_sqlite:
        # Use SQLite's shared-cache URI with a real pool.  StaticPool shares
        # one connection across request and worker threads, which can expose
        # stale snapshots or interleaved transactions under concurrent jobs.
        database_url = "sqlite:///file:test_atelier_shared?mode=memory&cache=shared&uri=true"
        _engine_options["connect_args"] = {"check_same_thread": False, "timeout": _sqlite_timeout, "uri": True}
        _engine_options["poolclass"] = QueuePool
else:
    # Keep connection pressure predictable under the API thread pool; the
    # values are configurable for larger Postgres deployments.
    _engine_options.update({
        "pool_size": env_int("DB_POOL_SIZE", 5, 1),
        "max_overflow": env_int("DB_MAX_OVERFLOW", 10, 0),
        "pool_timeout": env_int("DB_POOL_TIMEOUT", 30, 1),
    })
engine = create_engine(database_url, **_engine_options)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record):
        """Keep local SQLite reads from blocking a short worker commit.

        The test and single-process development setup uses a shared in-memory
        database. SQLite's shared-cache table locks are not resolved by the
        busy timeout, so read-uncommitted is needed for status polling while a
        worker finalizes a job. File-backed SQLite additionally benefits from
        WAL and the same explicit busy timeout.
        """
        cursor = dbapi_connection.cursor()
        try:
            timeout_ms = int(_sqlite_timeout * 1000)
            cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
            if _is_memory_sqlite:
                # Shared-cache in-memory SQLite otherwise reports SQLITE_LOCKED
                # for a status read racing a worker commit. This is limited to
                # the local test/development database; file-backed databases
                # retain normal read isolation.
                cursor.execute("PRAGMA read_uncommitted=1")
            else:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="reviewer")


class AiSettings(Base):
    __tablename__ = "ai_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    local_url: Mapped[str] = mapped_column(String(255), default="http://127.0.0.1:11434")
    local_model: Mapped[str] = mapped_column(String(120), default="qwen3:8b")
    remote_enabled: Mapped[bool] = mapped_column(default=False)
    remote_url: Mapped[str] = mapped_column(String(255), default="")
    remote_model: Mapped[str] = mapped_column(String(120), default="")
    remote_api_key: Mapped[str] = mapped_column(Text, default="")
    codex_enabled: Mapped[bool] = mapped_column(default=False)
    codex_command: Mapped[str] = mapped_column(String(255), default="codex")
    codex_model: Mapped[str] = mapped_column(String(120), default="")
    claude_enabled: Mapped[bool] = mapped_column(default=False)
    claude_command: Mapped[str] = mapped_column(String(255), default="claude")
    claude_model: Mapped[str] = mapped_column(String(120), default="")
    # Provider catalog is additive to the legacy single local/remote fields.
    # Keeping the legacy columns lets existing workers continue to run while
    # the admin UI can manage more than one connection.
    providers_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AiModel(Base):
    """Durable model endpoint catalog, separate from non-model AI tools.

    ``scope`` is deliberately explicit (``local`` or ``remote``) so callers
    do not infer deployment location from provider names or URLs.
    """
    __tablename__ = "ai_models"
    __table_args__ = (CheckConstraint("scope IN ('local', 'remote')", name="ck_ai_models_scope"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(16), default="remote")
    endpoint: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    protocol: Mapped[str] = mapped_column(String(32), default="openai")
    enabled: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str] = mapped_column(String(160), default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AiTool(Base):
    """Installed AI command-line tools such as Codex CLI and Claude Code."""
    __tablename__ = "ai_tools"
    id: Mapped[int] = mapped_column(primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=False)
    command: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class CodexSession(Base):
    """A durable project-scoped conversation with an isolated CLI workspace."""
    __tablename__ = "codex_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="新建 Codex 会话")
    status: Mapped[str] = mapped_column(String(20), default="ready")
    model: Mapped[str] = mapped_column(String(120), default="")
    working_directory: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class CodexMessage(Base):
    """Append-only transcript entries for a Codex session."""
    __tablename__ = "codex_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("codex_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AiQueueState(Base):
    """Singleton row used to atomically reserve durable worker capacity."""
    __tablename__ = "ai_queue_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    # Archived projects stay available for audit/read-only access but are
    # omitted from the default project picker.
    archived_at: Mapped[str | None] = mapped_column(String(40), nullable=True, default=None)


class WorkItem(Base):
    """A personal work-board entry, optionally linked to a project.

    Work items belong to the author rather than a project so the board can
    aggregate work across every project the user can access.
    """
    __tablename__ = "work_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="todo", index=True)
    start_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    due_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ProjectSkill(Base):
    """Project-local activation state for a skill from qa-doc-skills."""
    __tablename__ = "project_skills"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(String(120))
    source_path: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="reviewer", server_default="reviewer")


class MembershipEvent(Base):
    __tablename__ = "membership_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    before_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    after_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    filename: Mapped[str] = mapped_column(String(255))
    headers: Mapped[list] = mapped_column(JSON)
    rows: Mapped[list] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="preview")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    # Governance imports retain an immutable source fingerprint and snapshot
    # while legacy imports continue to use the original fields unchanged.
    kind: Mapped[str] = mapped_column(String(24), default="workspace", server_default="workspace")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_sheet: Mapped[str | None] = mapped_column(String(120), nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    parent_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    rules_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    import_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    original_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    directory: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(10), default="")
    smoke: Mapped[str] = mapped_column(String(10), default="")
    original: Mapped[list] = mapped_column(JSON)
    final_level: Mapped[str] = mapped_column(String(10), nullable=True)
    final_smoke: Mapped[str] = mapped_column(String(10), nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    reviewer: Mapped[str] = mapped_column(String(80), nullable=True)
    reviewed_at: Mapped[str] = mapped_column(String(40), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    final_categories: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    final_taxonomy_version_id: Mapped[int | None] = mapped_column(ForeignKey("taxonomy_versions.id"), nullable=True)
    platform_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    name_key: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    governance_status: Mapped[str] = mapped_column(String(24), default="active", server_default="active", index=True)
    domain: Mapped[str | None] = mapped_column(String(160), nullable=True)
    subdomain: Mapped[str | None] = mapped_column(String(240), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    precondition: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    tag_iteration: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tag_requirement: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tag_change: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("cases.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow, nullable=False)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(20), default="review", server_default="review")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class GovernanceDuplicateGroup(Base):
    __tablename__ = "governance_duplicate_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    gid: Mapped[str] = mapped_column(String(32), index=True)
    name_key: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    cluster_count: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(8), default="C")
    suggested_discard_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class GovernanceDuplicateMember(Base):
    __tablename__ = "governance_duplicate_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("governance_duplicate_groups.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    cluster_fp: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(24), default="keep")
    reason: Mapped[str] = mapped_column(Text, default="")


class GovernanceValidationRun(Base):
    __tablename__ = "governance_validation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    rows: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="blocked", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class GovernanceDirectory(Base):
    __tablename__ = "governance_directories"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    parent: Mapped[str] = mapped_column(String(1024), default="")
    level: Mapped[int] = mapped_column(Integer, default=0)
    domain: Mapped[str] = mapped_column(String(160), default="")
    subdomain: Mapped[str] = mapped_column(String(240), default="")
    whitelisted: Mapped[bool] = mapped_column(default=False)
    frozen: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(24), default="active")


class GovernanceChangeLog(Base):
    __tablename__ = "governance_change_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(32))
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    batch_key: Mapped[str] = mapped_column(String(64), index=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ts: Mapped[str] = mapped_column(String(40), default=utcnow)
    revert_of: Mapped[int | None] = mapped_column(ForeignKey("governance_change_logs.id"), nullable=True)


class GovernanceMetricSnapshot(Base):
    __tablename__ = "governance_metric_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class GovernanceExportJob(Base):
    __tablename__ = "governance_export_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="cases")
    format: Mapped[str] = mapped_column(String(8), default="xlsx")
    path: Mapped[str] = mapped_column(String(1024), default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    selfcheck: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class CaseComment(Base):
    __tablename__ = "case_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ConversionSettings(Base):
    __tablename__ = "conversion_settings"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    default_prefix_path: Mapped[str] = mapped_column(Text, default="")
    default_case_type: Mapped[str] = mapped_column(String(80), default="业务功能用例")
    default_case_status: Mapped[str] = mapped_column(String(40), default="正常")
    history_retention_days: Mapped[int] = mapped_column(Integer, default=180)


class ConversionRecord(Base):
    __tablename__ = "conversion_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    source_filename: Mapped[str] = mapped_column(String(255))
    output_filename: Mapped[str] = mapped_column(String(255))
    conversion_mode: Mapped[str] = mapped_column(String(40))
    output_mode: Mapped[str] = mapped_column(String(20))
    media_type: Mapped[str] = mapped_column(String(160))
    app_version: Mapped[str] = mapped_column(String(40))
    row_count: Mapped[int] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(Integer)
    preview: Mapped[dict] = mapped_column(JSON)


class SourceDocument(Base):
    __tablename__ = "source_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255))
    extracted_text: Mapped[str] = mapped_column(Text)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    provider: Mapped[str] = mapped_column(String(24), default="local")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class CaseDraft(Base):
    __tablename__ = "case_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class AiJob(Base):
    __tablename__ = "ai_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"))
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # Request correlation is copied from the enqueueing HTTP request so
    # background-worker logs remain traceable after the request returns.
    request_id: Mapped[str] = mapped_column(String(128), default="")
    # External workers use a short database lease so a crashed worker can be
    # recovered without allowing two workers to finalize the same job.
    lease_owner: Mapped[str] = mapped_column(String(128), default="")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    lease_heartbeat: Mapped[float] = mapped_column(default=0.0)
    queue_reserved: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Keep every prompt sent in this job so reviewers can inspect the exact
    # payload even when a task fails after one of several model calls.
    prompts: Mapped[list] = mapped_column(JSON, default=list)
    prompt_length: Mapped[int] = mapped_column(Integer, default=0)
    prompt_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class TaxonomyVersion(Base):
    __tablename__ = "taxonomy_versions"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_taxonomy_project_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    definitions: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    published_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SuggestionJob(Base):
    __tablename__ = "suggestion_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    taxonomy_version_id: Mapped[int | None] = mapped_column(ForeignKey("taxonomy_versions.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # Keep the originating request id for asynchronous job diagnostics.
    request_id: Mapped[str] = mapped_column(String(128), default="")
    lease_owner: Mapped[str] = mapped_column(String(128), default="")
    lease_until: Mapped[float] = mapped_column(default=0.0)
    lease_heartbeat: Mapped[float] = mapped_column(default=0.0)
    queue_reserved: Mapped[bool] = mapped_column(default=False)
    case_ids: Mapped[list] = mapped_column(JSON)
    total: Mapped[int] = mapped_column(Integer)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class CaseSuggestion(Base):
    __tablename__ = "case_suggestions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    taxonomy_version_id: Mapped[int | None] = mapped_column(ForeignKey("taxonomy_versions.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("suggestion_jobs.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    case_version: Mapped[int] = mapped_column(Integer)
    categories: Mapped[dict] = mapped_column(JSON)
    level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    smoke: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rationale: Mapped[str] = mapped_column(Text)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SmokeSuite(Base):
    __tablename__ = "smoke_suites"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    target_version: Mapped[str] = mapped_column(String(80))
    environment: Mapped[str] = mapped_column(String(80))
    required_chains: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class SmokeItem(Base):
    __tablename__ = "smoke_items"
    __table_args__ = (UniqueConstraint("suite_id", "case_id", name="uq_smoke_suite_case"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("smoke_suites.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    case_version: Mapped[int] = mapped_column(Integer)
    chain_name: Mapped[str] = mapped_column(String(120))
    estimated_seconds: Mapped[int] = mapped_column(Integer)
    confirmed_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("smoke_suites.id"))
    suite_revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    target_version: Mapped[str] = mapped_column(String(80))
    environment: Mapped[str] = mapped_column(String(80))
    required_chains: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="active")
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class RunCase(Base):
    __tablename__ = "run_cases"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_run_case"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    case_version: Mapped[int] = mapped_column(Integer)
    original_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(10))
    smoke: Mapped[str] = mapped_column(String(10))
    chain_name: Mapped[str] = mapped_column(String(120))
    estimated_seconds: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(20), default="not_run")
    actual_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    executed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_case_id: Mapped[int] = mapped_column(ForeignKey("run_cases.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)

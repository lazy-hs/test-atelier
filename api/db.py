import os
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow():
    return datetime.now(timezone.utc).isoformat()


database_url = os.getenv("DATABASE_URL", "sqlite:///./ai_studio.db")
engine = create_engine(database_url, pool_pre_ping=True, **({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool} if database_url == "sqlite://" else {}))
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
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


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
    status: Mapped[str] = mapped_column(String(24), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
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
    status: Mapped[str] = mapped_column(String(20), default="queued")
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

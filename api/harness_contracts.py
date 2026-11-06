"""Versioned wire contracts for the DeepSeek Harness integration.

The models in this module are deliberately independent from ``ai_engine``.
The existing provider models remain available during the migration, while the
Harness adapter uses explicit task and result versions that can be validated
before anything is written to the business tables.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskType = Literal["requirement-analysis", "test-cases", "skill-execution"]
GENERIC_SKILL_IDS = frozenset({"api-parsing", "dev-doc-analysis", "figma-to-prd"})
TaskStatus = Literal[
    "queued",
    "starting",
    "running",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
]


class _WireModel(BaseModel):
    """Reject accidental transport fields at the integration boundary."""

    model_config = ConfigDict(extra="forbid")


class HarnessTaskCreate(_WireModel):
    """Validated request to start one project-scoped Harness task."""

    task_type: TaskType
    task_version: Literal["v1"] = "v1"
    source_document_ids: list[str] = Field(default_factory=list, max_length=10)
    analysis_run_id: str | None = None
    skill_id: str = Field(min_length=1, max_length=80)
    session_id: str | None = None
    input: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("input")
    @classmethod
    def restrict_input(cls, value: dict) -> dict:
        if set(value) - {"language", "output_mode"}:
            raise ValueError("Harness input only accepts language and output_mode")
        language = value.get("language")
        if "language" in value and (not isinstance(language, str) or len(language) > 35
                                    or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language)):
            raise ValueError("Harness input language must be a language tag")
        if "output_mode" in value and value["output_mode"] != "structured":
            raise ValueError("Harness output_mode must be structured")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> "HarnessTaskCreate":
        if self.session_id is not None:
            raise ValueError("new Harness tasks cannot reuse a Session")
        if self.task_type == "requirement-analysis":
            if not self.source_document_ids:
                raise ValueError("requirement-analysis requires source_document_ids")
            if self.analysis_run_id is not None:
                raise ValueError("requirement-analysis cannot include analysis_run_id")
            if self.skill_id != "requirement-analysis":
                raise ValueError("requirement-analysis must use its matching skill")
        elif self.task_type == "test-cases":
            if not self.analysis_run_id:
                raise ValueError("test-cases requires analysis_run_id")
            if self.source_document_ids:
                raise ValueError("test-cases must reference the confirmed analysis run")
            if self.skill_id != "test-cases":
                raise ValueError("test-cases must use its matching skill")
        else:
            if not self.source_document_ids:
                raise ValueError("skill-execution requires source_document_ids")
            if self.analysis_run_id is not None:
                raise ValueError("skill-execution cannot include analysis_run_id")
            if self.skill_id not in GENERIC_SKILL_IDS:
                raise ValueError("skill-execution skill is not allowed")
        return self


class HarnessTaskView(_WireModel):
    """Task state returned by the Test Atelier API."""

    task_id: str
    project_id: int
    task_type: TaskType
    skill_id: str = Field(min_length=1, max_length=80)
    task_version: Literal["v1"]
    status: TaskStatus
    session_id: str | None = None
    session_ids: list[str] = Field(default_factory=list)
    # Keep the authorized input references in the task response so a
    # refreshed client can offer a safe retry without resending document text.
    source_document_ids: list[str] = Field(default_factory=list, max_length=10)
    retry_of: str | None = None
    analysis_run_id: str | None = None
    result_schema: Literal["requirement-analysis.v1", "test-cases.v1", "skill-execution.v1"]
    sequence: int = Field(ge=0)
    created_at: str
    updated_at: str


class HarnessConfirmation(_WireModel):
    """Canonical user decision forwarded to a waiting Harness task."""

    confirmation_id: str | None = Field(default=None, min_length=1, max_length=160)
    # ``id`` remains accepted for ACP/Host rolling compatibility; the API
    # always forwards the canonical ``confirmation_id`` field when present.
    id: str | None = Field(default=None, min_length=1, max_length=160)
    decision: Literal["approve", "reject", "cancel"]
    comment: str | None = Field(default=None, max_length=2000)
    ambiguity_choice: Literal["accept_suggestion", "provide_explanation", "skip", "mark_pending"] | None = None

    @model_validator(mode="after")
    def require_confirmation_id(self) -> "HarnessConfirmation":
        if not self.confirmation_id and not self.id:
            raise ValueError("confirmation_id is required")
        if self.ambiguity_choice is not None and self.decision != "approve":
            raise ValueError("an ambiguity choice requires approve")
        if self.ambiguity_choice == "provide_explanation" and not (self.comment or "").strip():
            raise ValueError("an explanation is required for this ambiguity choice")
        return self


class HarnessError(_WireModel):
    """Stable error payload; callers must branch on ``code`` not prose."""

    code: Literal[
        "unauthenticated",
        "forbidden",
        "project_not_found",
        "project_archived",
        "project_skill_disabled",
        "skill_not_allowed",
        "task_not_found",
        "task_conflict",
        "queue_full",
        "input_timeout",
        "provider_unavailable",
        "request_too_large",
        "output_too_large",
        "schema_invalid",
        "analysis_changed",
        "quality_gate_failed",
        "report_missing",
        "confirmation_unavailable",
        "invalid_confirmation_decision",
        "confirmation_id_mismatch",
        "cancelled",
        "internal_error",
    ]
    message: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=128)
    retryable: bool
    details: dict = Field(default_factory=dict)


class SourceRef(_WireModel):
    document_id: str = Field(min_length=1)
    locator: str = Field(min_length=1, max_length=240)


class QualityWarning(_WireModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=500)
    source_refs: list[SourceRef] = Field(default_factory=list)


class AcceptanceCriterion(_WireModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1)
    source_refs: list[SourceRef] = Field(min_length=1)


class RequirementItem(_WireModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    priority: Literal["high", "medium", "low"]
    source_refs: list[SourceRef] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class Ambiguity(_WireModel):
    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1)
    severity: Literal["blocking", "non_blocking"]
    related_requirement_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(min_length=1)
    resolution: str | None = None


class RequirementAnalysisData(_WireModel):
    requirements: list[RequirementItem] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    quality_warnings: list[QualityWarning] = Field(default_factory=list)


class RequirementAnalysisResult(_WireModel):
    """The complete ``requirement-analysis.v1`` result envelope."""

    schema_name: Literal["requirement-analysis.v1"] = Field(alias="schema")
    result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_document_ids: list[str] = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    warnings: list[QualityWarning] = Field(default_factory=list)
    data: RequirementAnalysisData
    report_markdown: str | None = Field(default=None, min_length=1, max_length=1_048_576)

    @field_validator("report_markdown")
    @classmethod
    def validate_report_markdown(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.lstrip().startswith("# "):
                raise ValueError("analysis report must begin with a Markdown heading")
            if len(value.encode("utf-8")) > 1_048_576:
                raise ValueError("analysis report exceeds the byte limit")
        return value


class CaseStep(_WireModel):
    order: int = Field(ge=1)
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)


class Traceability(_WireModel):
    requirement_id: str = Field(min_length=1)
    acceptance_criterion_id: str | None = None


class TestCaseItem(_WireModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1)
    # Optional in v1 for historical results; required for lossless export via
    # the upstream qa-doc-skills XMind generator.
    module_path: list[str] | None = Field(default=None, min_length=1, max_length=8)
    skill_priority: Literal["P0", "P1", "P2", "P3"] | None = None
    requirement_ids: list[str] = Field(min_length=1)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[CaseStep] = Field(min_length=1)
    expected: str = Field(min_length=1)
    method: str = Field(min_length=1, max_length=40)
    priority: Literal["high", "medium", "low"]
    labels: list[str] = Field(default_factory=list, max_length=20)
    traceability: list[Traceability] = Field(min_length=1)
    quality_warnings: list[QualityWarning] = Field(default_factory=list)


class TestCasesData(_WireModel):
    cases: list[TestCaseItem] = Field(default_factory=list)


class TestCasesResult(_WireModel):
    """The complete ``test-cases.v1`` result envelope."""

    schema_name: Literal["test-cases.v1"] = Field(alias="schema")
    result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    source_document_ids: list[str] = Field(min_length=1)
    analysis_run_id: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    warnings: list[QualityWarning] = Field(default_factory=list)
    data: TestCasesData


class SkillExecutionResult(_WireModel):
    """Generic, non-business projection for an allowlisted project skill."""

    schema_name: Literal["skill-execution.v1"] = Field(alias="schema")
    result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1, max_length=80)
    source_document_ids: list[str] = Field(min_length=1, max_length=10)
    generated_at: str = Field(min_length=1)
    warnings: list[QualityWarning] = Field(default_factory=list)
    # Skill-specific fields remain inside a namespaced data object. The API
    # stores it for review but never promotes it to published business rows.
    data: dict[str, Any]


def validate_requirement_analysis(
    payload: dict,
    source_document_ids: set[str],
) -> RequirementAnalysisResult:
    """Parse and cross-check a Harness requirement analysis result."""

    result = RequirementAnalysisResult.model_validate(payload)
    _ensure_task_sources(result.source_document_ids, source_document_ids)
    _ensure_warning_sources(result.warnings, source_document_ids)
    _ensure_warning_sources(result.data.quality_warnings, source_document_ids)

    requirement_ids: set[str] = set()
    criterion_ids: set[str] = set()
    for requirement in result.data.requirements:
        if requirement.id in requirement_ids:
            raise ValueError(f"duplicate requirement id: {requirement.id}")
        requirement_ids.add(requirement.id)
        for criterion in requirement.acceptance_criteria:
            if criterion.id in criterion_ids:
                raise ValueError(f"duplicate acceptance criterion id: {criterion.id}")
            criterion_ids.add(criterion.id)
            _ensure_source_refs(criterion.source_refs, source_document_ids)
        _ensure_source_refs(requirement.source_refs, source_document_ids)

    ambiguity_ids: set[str] = set()
    for ambiguity in result.data.ambiguities:
        if ambiguity.id in ambiguity_ids:
            raise ValueError(f"duplicate ambiguity id: {ambiguity.id}")
        ambiguity_ids.add(ambiguity.id)
        if ambiguity.severity == "blocking" and ambiguity.resolution is not None:
            raise ValueError("blocking ambiguity requires human confirmation")
        if not set(ambiguity.related_requirement_ids).issubset(requirement_ids):
            raise ValueError(f"ambiguity references an unknown requirement: {ambiguity.id}")
        _ensure_source_refs(ambiguity.source_refs, source_document_ids)
    return result


def validate_test_cases(
    payload: dict,
    source_document_ids: set[str],
    confirmed_requirement_ids: set[str],
    confirmed_criterion_owners: dict[str, str] | None = None,
) -> TestCasesResult:
    """Parse a case result and enforce traceability to a confirmed analysis."""

    result = TestCasesResult.model_validate(payload)
    _ensure_task_sources(result.source_document_ids, source_document_ids)
    _ensure_warning_sources(result.warnings, source_document_ids)
    criterion_owners = confirmed_criterion_owners or {}
    case_ids: set[str] = set()
    for case in result.data.cases:
        _ensure_warning_sources(case.quality_warnings, source_document_ids)
        if case.module_path is not None and any(not item.strip() or item != item.strip() or len(item) > 120
                                                for item in case.module_path):
            raise ValueError("case module path contains an invalid segment")
        if case.id in case_ids:
            raise ValueError(f"duplicate case id: {case.id}")
        case_ids.add(case.id)
        if case.method not in {"EP", "BVA", "ST", "EG"}:
            raise ValueError("case method is not supported by the Test Atelier draft schema")
        if not set(case.requirement_ids).issubset(confirmed_requirement_ids):
            raise ValueError(f"case references an unknown requirement: {case.id}")
        if len(case.requirement_ids) != len(set(case.requirement_ids)):
            raise ValueError(f"case repeats a requirement id: {case.id}")
        expected_orders = list(range(1, len(case.steps) + 1))
        if [step.order for step in case.steps] != expected_orders:
            raise ValueError(f"case steps must be contiguous from one: {case.id}")
        for trace in case.traceability:
            if trace.requirement_id not in confirmed_requirement_ids:
                raise ValueError(f"traceability references an unknown requirement: {case.id}")
            if trace.acceptance_criterion_id is not None and trace.acceptance_criterion_id not in criterion_owners:
                raise ValueError(f"traceability references an unknown criterion: {case.id}")
            if (trace.acceptance_criterion_id is not None
                    and criterion_owners[trace.acceptance_criterion_id] != trace.requirement_id):
                raise ValueError(f"traceability criterion belongs to another requirement: {case.id}")
        if {trace.requirement_id for trace in case.traceability} != set(case.requirement_ids):
            raise ValueError(f"traceability does not match case requirements: {case.id}")
    return result


def validate_skill_execution(
    payload: dict,
    source_document_ids: set[str],
    skill_id: str,
) -> SkillExecutionResult:
    """Validate the generic envelope without pretending to know skill output semantics."""

    result = SkillExecutionResult.model_validate(payload)
    if result.skill_id != skill_id:
        raise ValueError("skill-execution result skill_id does not match task")
    _ensure_task_sources(result.source_document_ids, source_document_ids)
    _ensure_warning_sources(result.warnings, source_document_ids)
    # Generic skills may deliver structured findings instead of Markdown, but
    # an empty envelope must never be presented as a completed skill result.
    stack = [value for key, value in result.data.items() if key != "metadata"]
    has_content = False
    while stack:
        value = stack.pop()
        if isinstance(value, str) and value.strip():
            has_content = True
            break
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, (bool, int, float)):
            has_content = True
            break
    if not has_content:
        raise ValueError("skill-execution requires non-empty deliverable data")
    output = result.data.get("output")
    if skill_id == "figma-to-prd" and (not isinstance(output, str)
                                        or not output.lstrip().startswith("# 产品需求文档（PRD）：")):
        raise ValueError("figma-to-prd requires the source skill's PRD heading")
    return result


def _ensure_task_sources(actual: list[str], expected: set[str]) -> None:
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("result source documents do not match the task input")


def _ensure_warning_sources(warnings: list[QualityWarning], allowed: set[str]) -> None:
    for warning in warnings:
        _ensure_source_refs(warning.source_refs, allowed)


def _ensure_source_refs(refs: list[SourceRef], allowed: set[str]) -> None:
    if not all(ref.document_id in allowed for ref in refs):
        raise ValueError("result contains an unknown source document")

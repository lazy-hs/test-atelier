"""Explicit adapters from completed Harness results to source skill artifacts."""

from __future__ import annotations

import json

from .harness_contracts import QualityWarning, TestCasesResult


MAX_XMIND_INPUT_BYTES = 16 * 1024 * 1024
MAX_XMIND_CASES = 5000
PLATFORM_TAGS = frozenset({"Pc", "Web", "Android", "IOS"})


class SkillArtifactUnavailable(ValueError):
    """The original result cannot be exported without inventing skill fields."""


def _skill_platform_tag(labels: list[str]) -> str | None:
    if len(labels) != 1:
        return None
    parts = labels[0].split(", ")
    if (not parts or len(parts) != len(set(parts)) or any(part not in PLATFORM_TAGS for part in parts)
            or ", ".join(parts) != labels[0]):
        return None
    return labels[0]


def _warning_note(warning: QualityWarning) -> str:
    locations = "; ".join(f"{ref.document_id}:{ref.locator}" for ref in warning.source_refs)
    return f"{warning.code} {warning.message}" + (f"（来源：{locations}）" if locations else "")


def skill_xmind_cases(result: dict, source_ids: set[str], *, task_id: str | None = None,
                      analysis_run_id: str | None = None) -> list[dict]:
    """Keep skill module and priority values; never infer them from business drafts."""
    try:
        validated = TestCasesResult.model_validate(result)
    except ValueError as exc:
        raise SkillArtifactUnavailable("原始用例结果不符合技能导出契约") from exc
    if len(validated.source_document_ids) != len(source_ids) or set(validated.source_document_ids) != source_ids:
        raise SkillArtifactUnavailable("原始用例来源与任务不匹配")
    if (task_id is not None and validated.task_id != task_id
            or analysis_run_id is not None and validated.analysis_run_id != analysis_run_id):
        raise SkillArtifactUnavailable("原始用例任务或分析归属不匹配")
    cases = validated.data.cases
    if not cases or len(cases) > MAX_XMIND_CASES:
        raise SkillArtifactUnavailable("原始用例数量为空或超过 XMind 导出上限")
    if any(not case.module_path or not case.skill_priority for case in cases):
        raise SkillArtifactUnavailable("原始用例缺少技能模块路径或 P0-P3 优先级，不能推断后导出")
    if any(_skill_platform_tag(case.labels) is None for case in cases):
        raise SkillArtifactUnavailable("原始用例缺少唯一且符合技能规范的适用端标签，不能推断后导出")
    if any(any(not part.strip() or part != part.strip() or len(part) > 120 for part in case.module_path)
           for case in cases):
        raise SkillArtifactUnavailable("原始用例包含无效的技能模块路径")
    if any(case.module_path[0] == "风险点" and len(case.module_path) < 2 for case in cases):
        raise SkillArtifactUnavailable("风险点用例缺少第二层风险类别")
    if not any(case.module_path[0] == "风险点" for case in cases):
        raise SkillArtifactUnavailable("原始用例缺少技能要求的风险点分类，不能作为技能 XMind 导出")
    if len({case.id for case in cases}) != len(cases):
        raise SkillArtifactUnavailable("原始用例包含重复 ID，不能作为技能 XMind 导出")
    if any(ref.document_id not in source_ids for warning in validated.warnings
           for ref in warning.source_refs):
        raise SkillArtifactUnavailable("原始用例质量提醒引用了未知来源文档")
    mapped = []
    for case in cases:
        if [step.order for step in case.steps] != list(range(1, len(case.steps) + 1)):
            raise SkillArtifactUnavailable("原始用例步骤序号不连续，不能作为技能 XMind 导出")
        if any(ref.document_id not in source_ids for warning in case.quality_warnings
               for ref in warning.source_refs):
            raise SkillArtifactUnavailable("原始用例质量提醒引用了未知来源文档")
        remarks = [
            f"用例 ID：{case.id}",
            f"Harness 任务：{validated.task_id}",
            f"分析运行：{validated.analysis_run_id}",
            f"结果 ID：{validated.result_id}",
            f"Session ID：{validated.session_id}",
            f"来源文档：{', '.join(validated.source_document_ids)}",
            f"生成时间：{validated.generated_at}",
            f"关联需求：{', '.join(case.requirement_ids)}",
            f"设计方法：{case.method}",
            f"整体预期：{case.expected}",
            "追溯：" + "; ".join(
                f"{trace.requirement_id}/{trace.acceptance_criterion_id or '未指定验收条件'}"
                for trace in case.traceability
            ),
        ]
        remarks.extend(f"质量提醒：{_warning_note(warning)}" for warning in case.quality_warnings)
        remarks.extend(f"全局质量提醒：{_warning_note(warning)}" for warning in validated.warnings)
        mapped.append({
            "模块": case.module_path,
            "用例标题": case.title,
            "优先级": case.skill_priority,
            "前置条件": "\n".join(case.preconditions),
            "步骤": [{"操作": step.action, "预期": step.expected} for step in case.steps],
            "备注": "\n".join(remarks),
            "标签": case.labels[0],
        })
    if len(json.dumps(mapped, ensure_ascii=False).encode("utf-8")) > MAX_XMIND_INPUT_BYTES:
        raise SkillArtifactUnavailable("原始用例超出技能 XMind 导出预算")
    return mapped

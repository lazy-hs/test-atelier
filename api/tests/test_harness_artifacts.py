from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

import pytest

from api.harness_artifacts import SkillArtifactUnavailable, skill_xmind_cases
from api.harness_contracts import validate_test_cases


def _result():
    case = {
        "id": "TC-1", "title": "边界失败", "module_path": ["登录", "边界"],
        "skill_priority": "P0", "requirement_ids": ["REQ-1"],
        "preconditions": ["已进入登录页"], "steps": [{"order": 1, "action": "提交超限值", "expected": "显示错误"}],
        "expected": "拒绝登录", "method": "BVA", "priority": "high", "labels": ["Web"],
        "traceability": [{"requirement_id": "REQ-1", "acceptance_criterion_id": "AC-1"}],
        "quality_warnings": [],
    }
    risk = {**deepcopy(case), "id": "TC-2", "title": "弱网中断", "module_path": ["风险点", "网络异常"],
            "skill_priority": "P2", "method": "EG"}
    return {
        "schema": "test-cases.v1", "result_id": "result-1", "task_id": "task-1",
        "session_id": "session-1", "source_document_ids": ["doc-1"], "analysis_run_id": "analysis-1",
        "generated_at": "2026-10-25T00:00:00Z", "warnings": [], "data": {"cases": [case, risk]},
    }


def test_xmind_adapter_preserves_skill_module_and_priority_without_mutating_original():
    result = _result()
    before = deepcopy(result)
    validate_test_cases(result, {"doc-1"}, {"REQ-1"}, {"AC-1": "REQ-1"})
    mapped = skill_xmind_cases(result, {"doc-1"})
    assert [item["模块"] for item in mapped] == [["登录", "边界"], ["风险点", "网络异常"]]
    assert [item["优先级"] for item in mapped] == ["P0", "P2"]
    assert mapped[0]["步骤"] == [{"操作": "提交超限值", "预期": "显示错误"}]
    assert mapped[0]["标签"] == "Web"
    assert "整体预期：拒绝登录" in mapped[0]["备注"]
    assert "追溯：REQ-1/AC-1" in mapped[0]["备注"]
    assert result == before


@pytest.mark.parametrize("change, message", [
    (lambda result: result["data"]["cases"][0].pop("module_path"), "缺少技能模块路径"),
    (lambda result: result["data"]["cases"][0].pop("skill_priority"), "缺少技能模块路径"),
    (lambda result: result["data"]["cases"].pop(), "风险点分类"),
    (lambda result: result["data"]["cases"][1].update(module_path=["风险点"]), "第二层风险类别"),
    (lambda result: result["data"]["cases"][0].update(labels=[]), "适用端标签"),
    (lambda result: result["data"]["cases"][0].update(labels=["Web", "IOS"]), "适用端标签"),
    (lambda result: result["data"]["cases"][0].update(labels=["WEB"]), "适用端标签"),
    (lambda result: result["data"]["cases"][0].update(labels=["Web,Web"]), "适用端标签"),
    (lambda result: result["data"]["cases"][0].update(labels=["Web, Web"]), "适用端标签"),
    (lambda result: result.update(source_document_ids=["foreign"]), "来源与任务不匹配"),
])
def test_xmind_adapter_refuses_to_fabricate_missing_skill_fields(change, message):
    result = _result()
    change(result)
    with pytest.raises(SkillArtifactUnavailable, match=message):
        skill_xmind_cases(result, {"doc-1"})


def test_case_schema_rejects_invalid_module_path_instead_of_exporting_it():
    result = _result()
    result["data"]["cases"][0]["module_path"] = [" 风险点", "分类"]
    with pytest.raises(ValueError, match="invalid segment"):
        validate_test_cases(result, {"doc-1"}, {"REQ-1"}, {"AC-1": "REQ-1"})


def test_xmind_adapter_keeps_an_explicit_multi_platform_tag_as_one_skill_value():
    result = _result()
    for case in result["data"]["cases"]:
        case["labels"] = ["Pc, Web, Android, IOS"]
    assert {item["标签"] for item in skill_xmind_cases(result, {"doc-1"})} == {"Pc, Web, Android, IOS"}


def test_xmind_adapter_preserves_case_specific_platform_labels_without_rewriting():
    result = _result()
    result["data"]["cases"][1]["labels"] = ["Pc"]
    assert [item["标签"] for item in skill_xmind_cases(result, {"doc-1"})] == ["Web", "Pc"]


def test_xmind_notes_keep_harness_provenance_and_all_warning_sources():
    result = _result()
    result["warnings"] = [{"code": "coverage", "message": "仍有覆盖风险", "source_refs": [
        {"document_id": "doc-1", "locator": "section-2"},
    ]}]
    result["data"]["cases"][0]["quality_warnings"] = [{"code": "boundary", "message": "边界待复核", "source_refs": [
        {"document_id": "doc-1", "locator": "section-1"},
    ]}]
    mapped = skill_xmind_cases(result, {"doc-1"}, task_id="task-1", analysis_run_id="analysis-1")
    note = mapped[0]["备注"]
    for value in ("Harness 任务：task-1", "分析运行：analysis-1", "结果 ID：result-1",
                  "Session ID：session-1", "来源文档：doc-1", "生成时间：2026-10-25T00:00:00Z",
                  "质量提醒：boundary 边界待复核（来源：doc-1:section-1）",
                  "全局质量提醒：coverage 仍有覆盖风险（来源：doc-1:section-2）"):
        assert value in note
    assert "全局质量提醒：coverage" in mapped[1]["备注"]


@pytest.mark.parametrize("change, message", [
    (lambda result: result.update(task_id="other-task"), "任务或分析归属不匹配"),
    (lambda result: result.update(analysis_run_id="other-analysis"), "任务或分析归属不匹配"),
    (lambda result: result["data"]["cases"][1].update(id="TC-1"), "重复 ID"),
    (lambda result: result["data"]["cases"][0]["steps"][0].update(order=2), "步骤序号不连续"),
    (lambda result: result.update(warnings=[{"code": "unknown", "message": "wrong source", "source_refs": [
        {"document_id": "elsewhere", "locator": "body"},
    ]}]), "未知来源文档"),
])
def test_xmind_adapter_rejects_result_misbindings_and_corrupt_case_sequences(change, message):
    result = _result()
    change(result)
    with pytest.raises(SkillArtifactUnavailable, match=message):
        skill_xmind_cases(result, {"doc-1"}, task_id="task-1", analysis_run_id="analysis-1")


def test_xmind_archive_keeps_source_notes_using_the_actual_skill_generator():
    from api.skill_runner import generate_xmind, skill_root

    if not (skill_root() / "scripts/generate_xmind.py").is_file():
        pytest.skip("External qa-doc-skills generator is not installed")
    content = generate_xmind(skill_xmind_cases(_result(), {"doc-1"}), "测试用例")
    with ZipFile(BytesIO(content)) as archive:
        assert archive.testzip() is None
        xml = archive.read("content.xml").decode("utf-8")
    assert xml.count("Harness 任务：task-1") == 2
    assert "分析运行：analysis-1" in xml
    assert "来源文档：doc-1" in xml
    assert "追溯：REQ-1/AC-1" in xml
    assert "priority-1" in xml and "priority-3" in xml
    assert "边界失败" in xml and "弱网中断" in xml

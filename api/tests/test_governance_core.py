import csv
from pathlib import Path

from api.governance_core import analyze, score, source_case_timestamps, validate_rows


def test_governance_score_includes_directory_and_timestamps():
    base = {"用例状态": "正常", "用例等级": "2", "预期结果": "ok", "前置条件": "ready"}
    without_metadata = score({**base})
    with_metadata = score({**base, "用例目录": "根 - 功能", "创建时间": "2026-10-20T10:00:00", "最后修改时间": "2026-10-21T10:00:00"})
    assert with_metadata - without_metadata == 4


def test_monitoring_center_golden_metrics():
    source = Path("/Users/lazy/WorkBuddy/监控中心用例/raw_cases.csv")
    if not source.exists():
        return
    with source.open(encoding="utf-8-sig", newline="") as handle:
        result = analyze(list(csv.DictReader(handle)))
    assert result["case_count"] == 3449
    assert result["directory_count"] == 230
    assert result["max_depth"] == 12
    assert result["duplicate_group_count"] == 417
    assert result["duplicate_case_count"] == 1195
    assert result["category_counts"] == {"A": 5, "B": 380, "C": 32}
    assert result["fingerprint_cluster_count"] == 654
    assert result["multi_cluster_group_count"] == 181
    assert result["discard_count"] == 545


def test_new_case_gate_separates_blockers_and_warnings():
    row = {
        "测试用例ID": "", "用例名称": "摄像机停用后不可访问", "用例类型": "业务功能用例", "用例等级": "1",
        "用例目录": "测试用例新(该目录下的所有用例都需要评审) - 监控中心模块用例 - 02_功能测试 - 00_能力基线 - 02_设备管理",
        "用例步骤": "1、停用摄像机", "前置条件": "摄像机已添加", "预期结果": "",
        "创建人": "tester", "备注": "[迭代:20261103][需求:REQ-1]", "端类型": "Web", "是否冒烟用例": "否",
    }
    result = validate_rows([row], [], whitelist={row["用例目录"]})
    assert result["blocked"] == 1
    assert result["can_promote"] is False
    assert not any(item["code"] == "required_tag" for item in result["errors"])



def test_source_case_timestamps_preserves_source_values_and_fills_missing_side():
    from datetime import datetime

    created, updated = source_case_timestamps({"创建时间": datetime(2026, 10, 21, 9, 54, 51), "最后修改时间": "2026-10-21 10:00:02"})
    assert created == "2026-10-21 09:54:51"
    assert updated == "2026-10-21 10:00:02"
    assert source_case_timestamps({"修改时间": "2026-10-22 11:12:13"}) == ("2026-10-22 11:12:13", "2026-10-22 11:12:13")


def test_newer_duplicate_case_has_higher_priority_from_timestamp_weights():
    rows = [
        {"测试用例ID": "old", "用例名称": "重复场景", "用例目录": "根 - 目录", "用例步骤": "打开", "预期结果": "成功", "创建时间": "2026-01-01 00:00:00", "最后修改时间": "2026-01-01 00:00:00"},
        {"测试用例ID": "new", "用例名称": "重复场景", "用例目录": "根 - 目录", "用例步骤": "打开", "预期结果": "成功", "创建时间": "2026-02-01 00:00:00", "最后修改时间": "2026-02-01 00:00:00"},
    ]
    members = analyze(rows)["groups"][0]["members"]
    assert members[0]["case_id"] == "new"
    assert members[0]["decision"] == "keep"
    assert members[1]["decision"] == "discard"

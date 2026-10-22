"""Pure case-governance rules shared by API workflows and regression tests.

The module deliberately accepts plain dictionaries.  It can therefore be used
by the HTTP layer without coupling the rules to SQLAlchemy or file storage.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
import difflib
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

SEP = " - "
SIMILARITY_THRESHOLD = 0.85
DEFAULT_RULES = {
    "root": "测试用例新(该目录下的所有用例都需要评审) - 监控中心模块用例",
    "max_depth": 6,
    "required_fields": ["用例名称", "用例类型", "用例等级", "用例目录", "用例步骤", "前置条件", "预期结果", "创建人"],
    "case_types": ["业务功能用例", "UI测试用例", "异常场景用例", "兼容性用例", "性能测试用例", "稳定性用例", "可靠性用例", "关联测试用例", "安全测试用例"],
    "levels": ["1", "2", "3", "4"],
    "status_values": ["正常", "待更新", "已废弃"],
    "smoke_values": ["是", "否"],
    "endpoint_values": ["win/mac/web", "ios/android", "appletv/androidtv"],
    "forbidden_dir_tokens": ["迭代", "阶段", "一期", "二期", "三期", "web/pc", "Web/PC", "移动端", "APP", "iOS", "Android", "HDMI", "补充用例", "废弃"],
    "tag_patterns": {"迭代": r"\[迭代:\d{8}\]", "需求": r"\[需求:[^\]]+\]"},
    "score_weights": {"directory": 2, "created_at": 1, "updated_at": 1},
}

CONTAINER_TOKENS = ("期", "阶段", "补充用例", "自测", "冒烟", "移动端", "web/pc", "HDMI")
ITER_RE = re.compile(r"^\d{8}迭代$")


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def split_dir(path: object) -> list[str]:
    return [part.strip() for part in text(path).split(SEP) if part.strip()]


def name_key(value: object) -> str:
    value = unicodedata.normalize("NFKC", text(value)).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def signature(row: dict) -> str:
    return (text(row.get("用例步骤")) + "|" + text(row.get("预期结果"))).strip()


def fingerprint(row: dict) -> str:
    return hashlib.md5(signature(row).encode("utf-8")).hexdigest()


def content_hash(row: dict) -> str:
    fields = ("用例名称", "用例目录", "用例步骤", "前置条件", "预期结果", "备注")
    return hashlib.sha256("\x1f".join(text(row.get(field)) for field in fields).encode("utf-8")).hexdigest()


def _load_json(path: Path, fallback: dict) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return {**fallback, **value}
    except (OSError, json.JSONDecodeError):
        return fallback.copy()


def load_rules(root: str | None = None) -> tuple[dict, str]:
    root_path = Path(root or os.getenv("TEST_ATELIER_CASE_RULES_ROOT", "")).expanduser()
    path = root_path / "rules.json" if root_path else None
    if not path or not path.is_file():
        return DEFAULT_RULES.copy(), "built-in"
    raw = path.read_bytes()
    return _load_json(path, DEFAULT_RULES), hashlib.sha256(raw).hexdigest()


def load_whitelist(root: str | None = None) -> tuple[set[str], str]:
    root_path = Path(root or os.getenv("TEST_ATELIER_CASE_RULES_ROOT", "")).expanduser()
    path = root_path / "catalog" / "目标目录规划.csv" if root_path else None
    if not path or not path.is_file():
        return set(), "built-in"
    raw = path.read_bytes()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        values = {text(row.get("目录路径")) for row in rows if text(row.get("目录路径"))}
    return values, hashlib.sha256(raw).hexdigest()


def predict_domain(path: object) -> str:
    value = text(path).lower()
    mappings = [
        (("权限", "隐私", "合规", "加密", "禁止访问", "授权"), "09_权限与隐私"),
        (("桌面小组件", "HDMI", "兼容"), "10_端与兼容"),
        (("POE", "添加", "扫描", "配网", "绑定", "解绑"), "01_设备接入"),
        (("PTZ", "云台", "预置位", "变焦", "巡航", "对讲", "语音"), "08_云台与对讲"),
        (("日报", "通知"), "07_通知与日报"),
        (("人脸", "AI", "智能检测", "识别", "车牌"), "06_AI检测与识别"),
        (("事件", "搜索", "追踪"), "05_事件中心"),
        (("录像", "回放", "归档", "下载", "剪辑", "倍速"), "04_录像与回放"),
        (("预览", "直播", "实时", "画面", "多视图", "录屏"), "03_实时预览"),
        (("IP变动", "停用", "启用", "重命名", "排序", "固件", "迁移", "摄像头设置"), "02_设备管理"),
    ]
    for keys, domain in mappings:
        if any(key.lower() in value for key in keys):
            return domain
    return "（未匹配，需人工归类）"


def source_case_timestamps(row: dict, fallback: str | None = None) -> tuple[str | None, str | None]:
    """Read case timestamps from imported source columns, not import time."""
    def value(keys: tuple[str, ...]) -> str | None:
        normalized = {text(key).casefold(): item for key, item in row.items()}
        for key in keys:
            item = normalized.get(key.casefold())
            if item is None or not text(item):
                continue
            if isinstance(item, datetime):
                return item.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(item, date):
                return item.strftime("%Y-%m-%d")
            return text(item)
        return None

    created = value(("创建时间", "创建日期", "created_at", "created time", "created"))
    updated = value(("最后修改时间", "修改时间", "最后更新时间", "updated_at", "updated time", "last modified", "modified"))
    if created is None and updated is not None:
        created = updated
    if updated is None and created is not None:
        updated = created
    return created or fallback, updated or created or fallback


def _timestamp(row: dict, *keys: str) -> str:
    for key in keys:
        value = text(row.get(key))
        if value:
            return value
    return ""


def _parse_timestamp(value: object) -> datetime | None:
    raw = text(value).replace("T", " ")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(raw[:19], pattern)
            except ValueError:
                continue
    return None


def _recency_bonus(row: dict, members: list[dict], rules: dict | None = None) -> int:
    """Give newer source records the configured timestamp weight within a duplicate group."""
    weights = {**DEFAULT_RULES["score_weights"], **((rules or {}).get("score_weights") or {})}
    bonus = 0
    for field, keys in (("created_at", ("创建时间", "created_at")),
                        ("updated_at", ("最后修改时间", "updated_at", "修改时间"))):
        current = _parse_timestamp(_timestamp(row, *keys))
        values = [_parse_timestamp(_timestamp(item, *keys)) for item in members]
        values = [item for item in values if item is not None]
        if current is None or len(values) < 2:
            continue
        oldest, newest = min(values), max(values)
        span = (newest - oldest).total_seconds()
        if span > 0:
            # Scale each timestamp dimension from 0..configured weight so a
            # newer duplicate wins without allowing timestamps to dominate
            # status, level, or content quality.
            ratio = (current - oldest).total_seconds() / span
            bonus += round(float(weights.get(field, 0)) * ratio)
    return bonus


def score(row: dict, case_name: str = "", rules: dict | None = None) -> int:
    weights = {**DEFAULT_RULES["score_weights"], **((rules or {}).get("score_weights") or {})}
    value = 0
    value += {"正常": 3, "待更新": 1, "已废弃": -6}.get(text(row.get("用例状态")) or "正常", 0)
    value += {"1": 2, "2": 1, "3": 0, "4": -1}.get(text(row.get("用例等级")), 0)
    value += 2 if text(row.get("预期结果")) else 0
    value += 1 if text(row.get("前置条件")) else 0
    value += int(weights.get("directory", 0)) if text(row.get("用例目录")) and len(split_dir(row.get("用例目录"))) <= 6 else 0
    value += int(weights.get("created_at", 0)) if _timestamp(row, "创建时间", "created_at") else 0
    value += int(weights.get("updated_at", 0)) if _timestamp(row, "最后修改时间", "updated_at", "修改时间") else 0
    directory = text(row.get("用例目录"))
    value -= 6 if "废弃" in directory else 0
    value -= 3 if "自测" in directory else 0
    value += 1 if "web/pc" in directory.lower() else 0
    if case_name and predict_domain(directory) == predict_domain(case_name):
        value += 2
    return value


def score_reason(row: dict, case_name: str = "", rules: dict | None = None) -> str:
    reasons: list[str] = []
    reasons.append("目录完整" if text(row.get("用例目录")) else "缺少用例目录")
    reasons.append("已记录创建时间" if _timestamp(row, "创建时间", "created_at") else "缺少创建时间")
    reasons.append("已记录最后修改时间" if _timestamp(row, "最后修改时间", "updated_at", "修改时间") else "缺少最后修改时间")
    if text(row.get("创建时间") or row.get("created_at")) or text(row.get("最后修改时间") or row.get("updated_at")):
        reasons.append("时间越新优先级越高")
    if text(row.get("用例状态")) == "已废弃":
        reasons.append("用例已废弃")
    if case_name and predict_domain(row.get("用例目录")) == predict_domain(case_name):
        reasons.append("目录与用例名称功能域一致")
    return "；".join(reasons)


def classify_group(rows: list[dict]) -> str:
    directories = {text(row.get("用例目录")) for row in rows}
    if len(directories) < len(rows):
        return "A"
    similarities = [difflib.SequenceMatcher(None, signature(left), signature(right)).ratio()
                    for index, left in enumerate(rows) for right in rows[index + 1:]]
    return "B" if max(similarities or [0]) >= 0.90 else "C"


def analyze(rows: Iterable[dict]) -> dict:
    rows = [dict(row) for row in rows]
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = name_key(row.get("用例名称"))
        if key:
            groups[key].append(row)
    duplicate_groups = []
    category_counts = Counter()
    discard_count = 0
    cluster_count = 0
    multi_cluster_count = 0
    for index, (key, members) in enumerate(sorted((item for item in groups.items() if len(item[1]) > 1), key=lambda item: (-len(item[1]), item[0])), 1):
        category = classify_group(members)
        category_counts[category] += 1
        ranked = sorted(members, key=lambda row: (-(score(row, text(row.get("用例名称"))) + _recency_bonus(row, members)), text(row.get("测试用例ID"))))
        clusters: dict[str, list[dict]] = defaultdict(list)
        for row in ranked:
            clusters[fingerprint(row)].append(row)
        cluster_count += len(clusters)
        multi_cluster_count += len(clusters) > 1
        decisions = {}
        keepers = []
        for cluster_members in clusters.values():
            keeper = cluster_members[0]
            keepers.append(keeper)
            for row in cluster_members:
                cid = text(row.get("测试用例ID"))
                if cid == text(keeper.get("测试用例ID")):
                    decisions[cid] = "keep" if text(row.get("用例状态")) != "已废弃" else "retired"
                else:
                    decisions[cid] = "discard" if category == "A" else "merge"
        discard_count += sum(decision in {"discard", "merge", "retired"} for decision in decisions.values())
        duplicate_groups.append({
            "gid": f"D{index:03d}", "name": text(members[0].get("用例名称")), "name_key": key,
            "size": len(members), "cluster_count": len(clusters), "category": category,
            "members": [{**row, "case_id": text(row.get("测试用例ID")), "fingerprint": fingerprint(row),
                          "score": score(row, text(row.get("用例名称"))) + _recency_bonus(row, members), "decision": decisions[text(row.get("测试用例ID"))],
                          "reason": score_reason(row, text(row.get("用例名称")))}
                         for row in ranked],
            "suggested_keep_ids": [text(row.get("测试用例ID")) for row in keepers],
            "suggested_discard_count": sum(decision in {"discard", "merge", "retired"} for decision in decisions.values()),
        })
    return {
        "case_count": len(rows), "directory_count": len({text(row.get("用例目录")) for row in rows if text(row.get("用例目录"))}),
        "max_depth": max((len(split_dir(row.get("用例目录"))) for row in rows), default=0),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_case_count": sum(group["size"] for group in duplicate_groups),
        "category_counts": dict(category_counts), "fingerprint_cluster_count": cluster_count,
        "multi_cluster_group_count": multi_cluster_count, "discard_count": discard_count,
        "status_counts": dict(Counter(text(row.get("用例状态")) or "正常" for row in rows)),
        "groups": duplicate_groups,
    }


def validate_rows(rows: Iterable[dict], existing: Iterable[dict] = (), *, mode: str = "new", rules: dict | None = None,
                 whitelist: set[str] | None = None) -> dict:
    rules = {**DEFAULT_RULES, **(rules or {})}
    whitelist = whitelist or set()
    existing_rows = [dict(row) for row in existing]
    names = {name_key(row.get("用例名称")): row for row in existing_rows}
    ids = {text(row.get("测试用例ID")) for row in existing_rows if text(row.get("测试用例ID"))}
    errors: list[dict] = []
    warnings: list[dict] = []
    for number, row in enumerate(rows, 2):
        label = f"第{number}行「{text(row.get('用例名称'))[:30]}」"
        def issue(collection: list, code: str, message: str):
            collection.append({"row": number, "code": code, "message": f"{label}：{message}"})
        strict = mode == "new"
        for field in rules.get("required_fields", []):
            if strict and not text(row.get(field)):
                issue(errors, "required", f"必填字段「{field}」为空")
        if text(row.get("用例类型")) and text(row.get("用例类型")) not in rules.get("case_types", []):
            issue(errors, "case_type", "用例类型不在允许集合")
        if text(row.get("用例等级")) and text(row.get("用例等级")) not in rules.get("levels", []):
            issue(errors, "level", "用例等级应为 1/2/3/4")
        if text(row.get("是否冒烟用例")) and text(row.get("是否冒烟用例")) not in rules.get("smoke_values", []):
            issue(errors, "smoke", "是否冒烟用例只能填 是/否")
        directory = text(row.get("用例目录"))
        parts = split_dir(directory)
        if directory:
            if not directory.startswith(text(rules.get("root"))): issue(errors, "directory_root", "目录不在监控中心模块下")
            if len(parts) > int(rules.get("max_depth", 6)):
                issue(errors if strict else warnings, "directory_depth", "目录层级超过上限")
            if mode == "new" and whitelist and directory not in whitelist:
                issue(errors, "directory_whitelist", "目录不在白名单")
            if mode == "new":
                bad = [token for token in rules.get("forbidden_dir_tokens", []) if token.lower() in directory.lower()]
                if bad: issue(errors, "directory_dimension", f"目录含被禁止的维度词 {bad}")
        key = name_key(row.get("用例名称"))
        if key and key in names:
            issue(errors, "duplicate_name", f"用例名与主库重复（{text(names[key].get('测试用例ID'))}）")
        elif key:
            for old_key, old in names.items():
                if abs(len(old_key) - len(key)) <= 6 and difflib.SequenceMatcher(None, key, old_key).ratio() >= float(rules.get("similarity_threshold", SIMILARITY_THRESHOLD)):
                    issue(warnings, "similar_name", f"与主库「{text(old.get('用例名称'))}」相似")
                    break
        cid = text(row.get("测试用例ID"))
        if cid and cid in ids: issue(errors, "duplicate_id", f"用例ID {cid} 与主库冲突")
        note = text(row.get("备注"))
        if strict:
            for tag, pattern in rules.get("tag_patterns", DEFAULT_RULES["tag_patterns"]).items():
                if not re.search(pattern, note): issue(errors, "required_tag", f"备注缺少标签 [{tag}:...]")
        steps, expected = text(row.get("用例步骤")), text(row.get("预期结果"))
        step_count = len(re.findall(r"(?m)^\s*\d+\s*[、.．)]", steps))
        expected_count = len(re.findall(r"(?m)^\s*\d+\s*[、.．)]", expected))
        if not step_count: issue(warnings, "steps_unordered", "用例步骤未编号")
        if step_count and expected_count and step_count != expected_count: issue(warnings, "aaa_mismatch", "步骤与预期结果条数不一致")
        if not text(row.get("前置条件")) or text(row.get("前置条件")) in {"无", "/"}: issue(warnings, "precondition", "前置条件为空或写无")
        if text(row.get("是否冒烟用例")) == "是" and text(row.get("用例等级")) in {"3", "4"}: issue(warnings, "smoke_level", "冒烟用例建议使用 1/2 级")
        if not text(row.get("端类型")): issue(warnings, "endpoint", "端类型未填写")
    return {"blocked": len(errors), "warnings": len(warnings), "errors": errors, "warning_items": warnings,
            "can_promote": not errors}


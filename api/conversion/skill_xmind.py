#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XMind 测试用例生成器
根据 JSON 数据生成符合导入规范的 .xmind 文件；根主题为**逻辑图向右**（structure-class: org.xmind.ui.logic.right）。

模板结构：
  根节点（项目名，不导入）
    └── 需求名称
          └── 需求模块（最多8层）
                └── 用例标题原文（P0–P3 使用 XMind 原生「优先级」标记，标题不再加 tc: 前缀）
                      │ 「笔记」：前置条件、备注、标签（非必填，均写入用例 topic 的 notes/plain；
                      │           不再生成 rc:/tag: 子节点）
                      ├── 步骤正文（写入 XMind 时去掉开头的「数字.」序号）
                      │     └── 预期正文（同上，顺序仍与 JSON 一致）

输入 JSON 格式：
[
  {
    "模块": ["模块1", "模块2"],          // 必填，数组，支持8层
    "用例标题": "测试用例名称",           // 必填
    "优先级": "P1",                      // 非必填，P0/P1/P2/P3
    "前置条件": "已登录系统",            // 非必填
    "步骤": [                            // 必填，至少1步
      {"操作": "步骤描述", "预期": "预期结果"}
    ],
    "备注": "备注内容",                  // 非必填
    "标签": "Web"                        // 非必填；适用端须与 SKILL 一致：Pc/Web/Android/IOS，多端用英文逗号+空格
  }
]

依赖：无额外依赖（使用标准库）
"""

import argparse
import hashlib
import json
import re
import sys
import time
from typing import List, Optional
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element, SubElement


# ── ID 生成 ──────────────────────────────────────────────────────────────────

_id_counter = 0

def _new_id(text: str = "") -> str:
    """生成唯一节点 ID"""
    global _id_counter
    _id_counter += 1
    raw = f"{text}{_id_counter}{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()[:26]


def _ts() -> str:
    return str(int(time.time() * 1000))


# 用例优先级 → XMind「标记 - 优先级」图标（数字 1–4）
# P0→优先级1，P1→优先级2，P2→优先级3，P3→优先级4（marker-id 与 XMind 内置一致）
_PRIORITY_MARKER_IDS = {
    "P0": "priority-1",
    "P1": "priority-2",
    "P2": "priority-3",
    "P3": "priority-4",
}


def _strip_leading_step_index(text: str) -> str:
    """保留步骤/预期开头的枚举序号（如 1. 2. 3.），不剥离。"""
    return text if text else ""


def _set_plain_notes(topic: Element, text: str) -> None:
    """为用例 topic 写入纯文本笔记（XMind content 2.0：notes/plain，客户端「笔记」面板）。"""
    body = str(text).strip()
    if not body:
        return
    notes_el = SubElement(topic, "notes")
    plain_el = SubElement(notes_el, "plain")
    plain_el.text = body


# ── XMind XML 构建 ───────────────────────────────────────────────────────────

def _make_topic(parent: Element, title: str, marker_ids: Optional[List[str]] = None) -> Element:
    """在 parent 的 children/topics 下创建一个子 topic。

    marker_ids: XMind 预置标记 id 列表（如 priority-2），写入 <marker-refs>，
    位于 title 之后，便于客户端显示为节点旁的优先级图标。
    """
    children = parent.find("children")
    if children is None:
        children = SubElement(parent, "children")

    topics = children.find("topics[@type='attached']")
    if topics is None:
        topics = SubElement(children, "topics", attrib={"type": "attached"})

    topic = SubElement(topics, "topic", attrib={
        "id": _new_id(title),
        "timestamp": _ts()
    })
    SubElement(topic, "title").text = title
    if marker_ids:
        refs = SubElement(topic, "marker-refs")
        for mid in marker_ids:
            if mid:
                SubElement(refs, "marker-ref", attrib={"marker-id": mid})
    return topic


def _build_module_path(root_topic: Element, module_path: list) -> Element:
    """
    沿模块路径逐层查找或创建节点，返回最末级模块节点。
    最多支持 8 层。
    """
    if len(module_path) > 8:
        raise ValueError(f"模块层数超过8层: {module_path}")

    current = root_topic
    for module_name in module_path:
        # 查找已有同名子节点
        found = None
        children = current.find("children")
        if children is not None:
            topics = children.find("topics[@type='attached']")
            if topics is not None:
                for t in topics.findall("topic"):
                    title_el = t.find("title")
                    if title_el is not None and title_el.text == module_name:
                        found = t
                        break
        current = found if found is not None else _make_topic(current, module_name)
    return current


def _build_test_case(parent: Element, case: dict):
    """在 parent 下构建单条测试用例节点及其子节点"""
    title = case.get("用例标题", "").strip()
    if not title:
        return

    priority = str(case.get("优先级") or "").strip().upper()
    tc_title = title
    marker_ids = None
    if priority in _PRIORITY_MARKER_IDS:
        marker_ids = [_PRIORITY_MARKER_IDS[priority]]

    tc_node = _make_topic(parent, tc_title, marker_ids)

    # 前置条件、备注、标签（非必填）→ 合并写入用例主题「笔记」plain；不生成 rc:/tag: 子节点
    note_parts: List[str] = []
    precondition = str(case.get("前置条件") or "").strip()
    if precondition:
        note_parts.append(precondition)
    remark = str(case.get("备注") or "").strip()
    if remark:
        note_parts.append(f"【备注】\n{remark}")
    tag = str(case.get("标签") or "").strip()
    if tag:
        note_parts.append(f"【标签】\n{tag}")
    if note_parts:
        _set_plain_notes(tc_node, "\n\n".join(note_parts))

    # 步骤 + 预期结果
    steps = case.get("步骤") or []
    for step in steps:
        action = _strip_leading_step_index(
            str(step.get("操作") or step.get("步骤") or "").strip()
        )
        expected = _strip_leading_step_index(
            str(step.get("预期") or step.get("预期结果") or "").strip()
        )
        if not action:
            continue
        step_node = _make_topic(tc_node, action)
        if expected:
            _make_topic(step_node, expected)


def build_xmind_content(cases: list, root_title: str = "测试用例") -> bytes:
    """构建 content.xml 内容"""
    xmap = Element("xmap-content", attrib={
        "version": "2.0",
        "xmlns": "urn:xmind:xmap:xmlns:content:2.0",
        "xmlns:fo": "http://www.w3.org/1999/XSL/Format",
        "xmlns:svg": "http://www.w3.org/2000/svg",
        "xmlns:xhtml": "http://www.w3.org/1999/xhtml",
        "xmlns:xlink": "http://www.w3.org/1999/xlink",
    })

    sheet = SubElement(xmap, "sheet", attrib={
        "id": _new_id("sheet"),
        "timestamp": _ts()
    })

    # 根节点：逻辑图向右（根在左、分支向右展开），避免默认「思维导图」向四周发散
    root_topic = SubElement(sheet, "topic", attrib={
        "id": _new_id(root_title),
        "timestamp": _ts(),
        "structure-class": "org.xmind.ui.logic.right",
    })
    SubElement(root_topic, "title").text = root_title
    SubElement(sheet, "title").text = "Sheet 1"

    # 按模块路径分组写入用例
    for case in cases:
        module_path = case.get("模块") or []
        if isinstance(module_path, str):
            module_path = [module_path]

        if not module_path:
            # 无模块，直接挂在根节点下
            parent = root_topic
        else:
            parent = _build_module_path(root_topic, module_path)

        _build_test_case(parent, case)

    ET.indent(xmap, space="  ")
    return ET.tostring(xmap, encoding="unicode", xml_declaration=False).encode("utf-8")


def build_manifest() -> bytes:
    """构建 META-INF/manifest.xml"""
    manifest = Element("manifest", attrib={
        "xmlns": "urn:xmind:xmap:xmlns:manifest:1.0"
    })
    SubElement(manifest, "file-entry", attrib={
        "full-path": "content.xml",
        "media-type": "text/xml"
    })
    SubElement(manifest, "file-entry", attrib={
        "full-path": "META-INF/",
        "media-type": ""
    })
    ET.indent(manifest, space="  ")
    return ET.tostring(manifest, encoding="unicode").encode("utf-8")


def generate_xmind(cases: list, output_path: str, root_title: str = "测试用例"):
    """生成 .xmind 文件（ZIP 格式）"""
    content_xml = build_xmind_content(cases, root_title)
    manifest_xml = build_manifest()

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", content_xml)
        zf.writestr("META-INF/manifest.xml", manifest_xml)

    print(f"已生成: {output_path}")


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="XMind 测试用例生成器")
    parser.add_argument("-o", "--output", required=True, help="输出 .xmind 文件路径")
    parser.add_argument("-d", "--data", help="JSON 格式测试用例数据（字符串）")
    parser.add_argument("-f", "--file", help="JSON 文件路径")
    parser.add_argument("--title", default="测试用例", help="根节点标题（默认：测试用例）")
    args = parser.parse_args()

    try:
        if args.file:
            cases = json.loads(Path(args.file).read_text(encoding="utf-8"))
        elif args.data:
            cases = json.loads(args.data)
        else:
            # 从 stdin 读取
            cases = json.loads(sys.stdin.read())

        if not isinstance(cases, list):
            raise ValueError("输入数据必须是 JSON 数组")

        generate_xmind(cases, args.output, args.title)

    except json.JSONDecodeError as e:
        print(f"JSON 解析错误: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"数据错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"生成失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

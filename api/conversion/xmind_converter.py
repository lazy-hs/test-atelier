import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from .converter import ConversionError, SOURCE_HEADERS


FIELD_NODES = [
    ("前置条件", "前置条件"),
    ("用例步骤", "用例步骤"),
    ("预期结果", "预期结果"),
    ("用例等级", "用例等级"),
    ("是否冒烟用例", "是否是冒烟用例"),
]

CONTENT_NS = "urn:xmind:xmap:xmlns:content:2.0"
MANIFEST_NS = "urn:xmind:xmap:xmlns:manifest:1.0"
STYLE_NS = "urn:xmind:xmap:xmlns:style:2.0"
META_NS = "urn:xmind:xmap:xmlns:meta:2.0"
PRIORITY_PATTERN = re.compile(r"^priority-([1-4])$")
STEP_PATTERN = re.compile(r"^(?:步骤\s*)?\d+\s*[.、)）:-]", re.IGNORECASE)


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"[\\/:*?\"<>|]", "_", stem).strip(" .") or "测试用例"


def _new_id() -> str:
    return uuid.uuid4().hex


def _clean_text(value) -> str:
    return str(value or "").strip()


def _lines(value) -> list[str]:
    return [line.strip() for line in _clean_text(value).splitlines() if line.strip()]


def _topic_children_json(topic: dict) -> list[dict]:
    children = topic.get("children") or {}
    if isinstance(children, list):
        return [child for child in children if isinstance(child, dict)]
    if not isinstance(children, dict):
        return []
    result = []
    for group in children.values():
        if isinstance(group, list):
            result.extend(child for child in group if isinstance(child, dict))
    return result


def _json_notes(topic: dict) -> str:
    notes = topic.get("notes")
    if isinstance(notes, str):
        return notes.strip()
    if not isinstance(notes, dict):
        return ""
    plain = notes.get("plain")
    if isinstance(plain, str):
        return plain.strip()
    if isinstance(plain, dict):
        return _clean_text(plain.get("content") or plain.get("text"))
    return ""


def _json_markers(topic: dict) -> list[str]:
    result = []
    for marker in topic.get("markers") or []:
        if isinstance(marker, str):
            result.append(marker)
        elif isinstance(marker, dict):
            marker_id = marker.get("markerId") or marker.get("marker-id") or marker.get("id")
            if marker_id:
                result.append(str(marker_id))
    return result


def _json_topic_to_tree(topic: dict) -> dict:
    return {
        "title": _clean_text(topic.get("title")) or "未命名节点",
        "node_type": "topic",
        "notes": _json_notes(topic),
        "markers": _json_markers(topic),
        "children": [_json_topic_to_tree(child) for child in _topic_children_json(topic)],
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_xml_child(topic: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(topic) if _local_name(child.tag) == name), None)


def _xml_topic_children(topic: ET.Element) -> list[ET.Element]:
    children = _direct_xml_child(topic, "children")
    if children is None:
        return []
    for topics in list(children):
        if _local_name(topics.tag) == "topics" and topics.attrib.get("type", "attached") == "attached":
            return [child for child in list(topics) if _local_name(child.tag) == "topic"]
    return []


def _xml_topic_to_tree(topic: ET.Element) -> dict:
    title_element = _direct_xml_child(topic, "title")
    notes_element = _direct_xml_child(topic, "notes")
    marker_refs = _direct_xml_child(topic, "marker-refs")
    plain = None
    if notes_element is not None:
        plain = next((item for item in notes_element.iter() if _local_name(item.tag) == "plain"), None)
    markers = []
    if marker_refs is not None:
        markers = [
            child.attrib.get("marker-id", "")
            for child in list(marker_refs)
            if _local_name(child.tag) == "marker-ref" and child.attrib.get("marker-id")
        ]
    return {
        "title": _clean_text(title_element.text if title_element is not None else "") or "未命名节点",
        "node_type": "topic",
        "notes": _clean_text(plain.text if plain is not None else ""),
        "markers": markers,
        "children": [_xml_topic_to_tree(child) for child in _xml_topic_children(topic)],
    }


def _read_roots(content: bytes) -> list[dict]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ConversionError("无法读取 XMind 文件，请确认文件未损坏。") from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > 80 * 1024 * 1024:
            raise ConversionError("XMind 解压内容超过安全限制。")
        names = set(archive.namelist())
        try:
            if "content.json" in names:
                payload = json.loads(archive.read("content.json").decode("utf-8-sig"))
                sheets = payload if isinstance(payload, list) else payload.get("sheets", [])
                roots = []
                for sheet in sheets:
                    root = sheet.get("rootTopic") or sheet.get("root_topic")
                    if isinstance(root, dict):
                        roots.append(_json_topic_to_tree(root))
                if roots:
                    return roots

            if "content.xml" in names:
                xml_root = ET.fromstring(archive.read("content.xml"))
                roots = []
                for sheet in xml_root.iter():
                    if _local_name(sheet.tag) != "sheet":
                        continue
                    topic = next((child for child in list(sheet) if _local_name(child.tag) == "topic"), None)
                    if topic is not None:
                        roots.append(_xml_topic_to_tree(topic))
                if roots:
                    return roots
        except (ValueError, KeyError, ET.ParseError, UnicodeDecodeError) as exc:
            raise ConversionError("XMind 内容格式无法解析。") from exc

    raise ConversionError("XMind 中未找到可读取的主题内容。")


def _parse_field_node(title: str) -> tuple[str, str] | None:
    normalized = title.strip()
    for label, source_key in FIELD_NODES:
        if normalized == label:
            return source_key, ""
        for delimiter in ("\n", "：", ":"):
            prefix = f"{label}{delimiter}"
            if normalized.startswith(prefix):
                return source_key, normalized[len(prefix):].strip()
    return None


def _priority_level(node: dict) -> str:
    for marker in node.get("markers") or []:
        match = PRIORITY_PATTERN.match(marker)
        if match:
            return match.group(1)
    return ""


def _looks_like_reference_case(node: dict) -> bool:
    if _priority_level(node) or node.get("notes"):
        return True
    children = node.get("children") or []
    if not children:
        return False
    if any(_priority_level(child) or child.get("notes") for child in children):
        return False
    if not all(STEP_PATTERN.match(_clean_text(child.get("title"))) for child in children):
        return False
    grandchildren = [grandchild for child in children for grandchild in child.get("children") or []]
    if any(_priority_level(grandchild) or grandchild.get("notes") for grandchild in grandchildren):
        return False
    return not any(grandchild.get("children") for grandchild in grandchildren)


def _reference_case(node: dict, ancestors: list[str], separator: str) -> dict:
    case = {header: "" for header in SOURCE_HEADERS}
    case["用例目录"] = separator.join(part for part in ancestors if part)
    case["用例名称"] = node.get("title") or "未命名用例"
    case["用例等级"] = _priority_level(node)
    case["前置条件"] = node.get("notes") or ""
    case["是否是冒烟用例"] = "否"
    steps = []
    expected_results = []
    for step in node.get("children") or []:
        steps.append(step.get("title") or "未命名步骤")
        expected_results.extend(
            expected.get("title") or "未命名预期结果"
            for expected in step.get("children") or []
        )
    case["用例步骤"] = "\n".join(steps)
    case["预期结果"] = "\n".join(expected_results)
    return case


def _extract_cases(node: dict, ancestors: list[str], separator: str) -> list[dict]:
    children = node.get("children") or []
    fields = {}
    for child in children:
        parsed = _parse_field_node(child.get("title", ""))
        if parsed:
            fields[parsed[0]] = parsed[1]

    if fields:
        node["node_type"] = "case"
        for child in children:
            child["node_type"] = "field"
        case = {header: "" for header in SOURCE_HEADERS}
        case["用例目录"] = separator.join(part for part in ancestors if part)
        case["用例名称"] = node.get("title") or "未命名用例"
        case.update(fields)
        return [case]

    if _looks_like_reference_case(node):
        node["node_type"] = "case"
        for step in children:
            step["node_type"] = "step"
            for expected in step.get("children") or []:
                expected["node_type"] = "expected"
        return [_reference_case(node, ancestors, separator)]

    if not children:
        node["node_type"] = "case"
        case = {header: "" for header in SOURCE_HEADERS}
        case["用例目录"] = separator.join(part for part in ancestors if part)
        case["用例名称"] = node.get("title") or "未命名用例"
        return [case]

    node["node_type"] = "directory"
    next_ancestors = ancestors + ([node.get("title", "")] if node.get("title") else [])
    cases = []
    for child in children:
        cases.extend(_extract_cases(child, next_ancestors, separator))
    return cases


def parse_xmind(content: bytes, filename: str, separator: str) -> dict:
    if Path(filename).suffix.lower() != ".xmind":
        raise ConversionError("请选择 .xmind 文件。")

    roots = _read_roots(content)
    cases = []
    for root in roots:
        root["node_type"] = "root"
        for child in root.get("children") or []:
            cases.extend(_extract_cases(child, [], separator))

    if not cases:
        raise ConversionError("XMind 中没有可转换的用例节点。")

    root_title = roots[0].get("title") or safe_stem(filename)
    tree = roots[0] if len(roots) == 1 else {
        "title": safe_stem(filename),
        "node_type": "root",
        "notes": "",
        "markers": [],
        "children": roots,
    }
    return {"root_title": root_title, "cases": cases, "tree": tree}


def _find_directory_child(node: dict, title: str) -> dict:
    for child in node["children"]:
        if child.get("node_type") == "directory" and child.get("title") == title:
            return child
    child = {"title": title, "node_type": "directory", "notes": "", "markers": [], "children": []}
    node["children"].append(child)
    return child


def _priority_marker(level: str) -> str:
    normalized = _clean_text(level).lower()
    if PRIORITY_PATTERN.match(normalized):
        return normalized
    if normalized in {"1", "2", "3", "4"}:
        return f"priority-{normalized}"
    p_level = re.fullmatch(r"p([0-3])", normalized)
    return f"priority-{int(p_level.group(1)) + 1}" if p_level else ""


def build_case_tree(cases: list[dict], root_title: str, separator: str) -> dict:
    root = {"title": root_title, "node_type": "root", "notes": "", "markers": [], "children": []}
    for case in cases:
        parent = root
        directory = _clean_text(case.get("用例目录"))
        for segment in [part.strip() for part in directory.split(separator) if part.strip()]:
            parent = _find_directory_child(parent, segment)

        marker = _priority_marker(case.get("用例等级", ""))
        case_node = {
            "title": _clean_text(case.get("用例名称")) or "未命名用例",
            "node_type": "case",
            "notes": _clean_text(case.get("前置条件")),
            "markers": [marker] if marker else [],
            "children": [],
        }
        steps = _lines(case.get("用例步骤"))
        expected_results = _lines(case.get("预期结果"))
        if not steps and expected_results:
            steps = ["用例步骤"]

        for index, step in enumerate(steps):
            step_node = {"title": step, "node_type": "step", "notes": "", "markers": [], "children": []}
            if index < len(expected_results):
                expected_for_step = [expected_results[index]]
                if index == len(steps) - 1 and len(expected_results) > len(steps):
                    expected_for_step.extend(expected_results[len(steps):])
                step_node["children"] = [
                    {"title": expected, "node_type": "expected", "notes": "", "markers": [], "children": []}
                    for expected in expected_for_step
                ]
            case_node["children"].append(step_node)
        parent["children"].append(case_node)
    return root


def _tree_to_json_topic(node: dict, is_root: bool = False) -> dict:
    topic = {"id": _new_id(), "class": "topic", "title": node.get("title") or "未命名节点"}
    if is_root:
        topic["structureClass"] = "org.xmind.ui.logic.right"
    if node.get("notes"):
        topic["notes"] = {"plain": {"content": node["notes"]}}
    if node.get("markers"):
        topic["markers"] = [{"markerId": marker} for marker in node["markers"]]
    children = node.get("children") or []
    if children:
        topic["children"] = {"attached": [_tree_to_json_topic(child) for child in children]}
    return topic


def _append_xml_topic(parent: ET.Element, node: dict, is_root: bool = False) -> ET.Element:
    attributes = {"id": _new_id()}
    if is_root:
        attributes["structure-class"] = "org.xmind.ui.logic.right"
    topic = ET.SubElement(parent, f"{{{CONTENT_NS}}}topic", attributes)
    title = ET.SubElement(topic, f"{{{CONTENT_NS}}}title")
    title.text = node.get("title") or "未命名节点"
    if node.get("markers"):
        marker_refs = ET.SubElement(topic, f"{{{CONTENT_NS}}}marker-refs")
        for marker in node["markers"]:
            ET.SubElement(marker_refs, f"{{{CONTENT_NS}}}marker-ref", {"marker-id": marker})
    if node.get("notes"):
        notes = ET.SubElement(topic, f"{{{CONTENT_NS}}}notes")
        plain = ET.SubElement(notes, f"{{{CONTENT_NS}}}plain")
        plain.text = node["notes"]
    children = node.get("children") or []
    if children:
        children_element = ET.SubElement(topic, f"{{{CONTENT_NS}}}children")
        topics = ET.SubElement(children_element, f"{{{CONTENT_NS}}}topics", {"type": "attached"})
        for child in children:
            _append_xml_topic(topics, child)
    return topic


def _build_content_xml(tree: dict, sheet_id: str) -> bytes:
    ET.register_namespace("", CONTENT_NS)
    root = ET.Element(f"{{{CONTENT_NS}}}xmap-content", {"version": "2.0"})
    sheet = ET.SubElement(root, f"{{{CONTENT_NS}}}sheet", {"id": sheet_id})
    sheet_title = ET.SubElement(sheet, f"{{{CONTENT_NS}}}title")
    sheet_title.text = tree.get("title") or "测试用例"
    _append_xml_topic(sheet, tree, is_root=True)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _build_manifest_xml() -> bytes:
    ET.register_namespace("", MANIFEST_NS)
    root = ET.Element(f"{{{MANIFEST_NS}}}manifest")
    for path, media_type in (("content.xml", "text/xml"), ("styles.xml", "text/xml"), ("meta.xml", "text/xml")):
        ET.SubElement(root, f"{{{MANIFEST_NS}}}file-entry", {"full-path": path, "media-type": media_type})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_xmind(cases: list[dict], source_filename: str, separator: str, app_version: str) -> tuple[bytes, str, dict]:
    root_title = safe_stem(source_filename)
    tree = build_case_tree(cases, root_title, separator)
    sheet_id = _new_id()
    content_json = [{"id": sheet_id, "class": "sheet", "title": root_title, "rootTopic": _tree_to_json_topic(tree, is_root=True)}]
    metadata = {"creator": {"name": "用例格式转换台", "version": app_version}, "activeSheetId": sheet_id}
    manifest_json = {
        "file-entries": {
            "content.json": {"media-type": "application/json"},
            "metadata.json": {"media-type": "application/json"},
            "content.xml": {"media-type": "text/xml"},
            "styles.xml": {"media-type": "text/xml"},
            "meta.xml": {"media-type": "text/xml"},
            "META-INF/manifest.xml": {"media-type": "text/xml"},
        }
    }
    styles_xml = f'<?xml version="1.0" encoding="UTF-8"?><xmap-styles xmlns="{STYLE_NS}" version="2.0"/>'.encode()
    meta_xml = f'<?xml version="1.0" encoding="UTF-8"?><meta xmlns="{META_NS}"/>'.encode()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.json", json.dumps(content_json, ensure_ascii=False, separators=(",", ":")))
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
        archive.writestr("manifest.json", json.dumps(manifest_json, ensure_ascii=False, separators=(",", ":")))
        archive.writestr("content.xml", _build_content_xml(tree, sheet_id))
        archive.writestr("styles.xml", styles_xml)
        archive.writestr("meta.xml", meta_xml)
        archive.writestr("META-INF/manifest.xml", _build_manifest_xml())

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return output.getvalue(), f"{root_title}_{timestamp}.xmind", tree


def xmind_xlsx_name(source_filename: str, output_mode: str) -> str:
    suffix = "路径拼接后" if output_mode == "after" else "路径拼接前"
    return f"{safe_stem(source_filename)}_{suffix}.xlsx"

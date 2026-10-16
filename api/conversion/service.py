"""The four conversion modes share the original converter's parsers and exporters."""

from pathlib import Path

from .converter import (
    ConversionError, SOURCE_HEADERS, TARGET_HEADERS, build_preview,
    export_source_workbook, export_workbook, parse_source, safe_output_name,
    transform_cases,
)
from .xmind_converter import build_case_tree, build_xmind, parse_xmind, safe_stem, xmind_xlsx_name

APP_VERSION = "0.2.0"
TEMPLATE = Path(__file__).parent / "templates" / "ugos_pro测试用例导入模板_20260901155913.xlsx"
PATH_SEPARATOR = " - "
CASE_TYPES = ["UI测试用例", "业务功能用例", "异常场景用例", "关联测试用例", "用户场景用例", "性能测试用例", "可靠性测试用例", "稳定性用例", "兼容性用例", "安全性测试用例", "其他"]
CASE_STATUSES = ["正常", "待更新", "已废弃"]
MODES = {"excel_to_excel", "xmind_to_excel", "xmind_to_final_excel", "excel_to_xmind"}


def _source_rows(cases):
    return [{header: case.get(header, "") for header in SOURCE_HEADERS} for case in cases]


def preview(content: bytes, filename: str, mode: str, output_mode: str, prefix: str, case_type: str, status: str) -> dict:
    if mode not in MODES or output_mode not in {"before", "after"}:
        raise ConversionError("不支持当前转换方式或输出格式。")
    if mode in {"xmind_to_excel", "xmind_to_final_excel"}:
        output_mode = "after" if mode == "xmind_to_final_excel" else output_mode
        parsed = parse_xmind(content, filename, PATH_SEPARATOR)
        original = _source_rows(parsed["cases"])
        if output_mode == "after":
            _validate_options(case_type, status)
            target = transform_cases(original, prefix, case_type, status, PATH_SEPARATOR)
            target_headers = TARGET_HEADERS
        else:
            target, target_headers = original, SOURCE_HEADERS
        return {
            "filename": filename, "sheet_name": parsed["root_title"], "row_count": len(target),
            "preview_type": mode, "output_mode": output_mode, "source_kind": "tree",
            "source_tree": parsed["tree"], "source_headers": [], "source_rows": [],
            "target_kind": "table", "target_headers": target_headers, "target_rows": target,
            "original_headers": SOURCE_HEADERS, "original": original, "converted": target,
            "export_label": "导出 Excel",
        }
    if mode == "excel_to_xmind":
        parsed = parse_source(content, filename)
        original = _source_rows(parsed["cases"])
        tree = build_case_tree(original, safe_stem(filename), PATH_SEPARATOR)
        return {
            "filename": filename, "sheet_name": parsed["sheet_name"], "row_count": len(original),
            "preview_type": mode, "output_mode": "after", "source_kind": "table",
            "source_headers": SOURCE_HEADERS, "source_rows": original, "target_kind": "tree",
            "target_tree": tree, "target_headers": [], "target_rows": [],
            "original_headers": SOURCE_HEADERS, "original": original, "converted": [],
            "export_label": "导出 XMind",
        }
    _validate_options(case_type, status)
    result = build_preview(content, filename, prefix, case_type, status, PATH_SEPARATOR)
    result["output_mode"] = "after"
    return result


def _validate_options(case_type: str, status: str) -> None:
    if case_type not in CASE_TYPES or status not in CASE_STATUSES:
        raise ConversionError("用例类型或状态不在当前配置允许范围内。")


def export(content: bytes, filename: str, mode: str, output_mode: str, prefix: str, case_type: str, status: str):
    result = preview(content, filename, mode, output_mode, prefix, case_type, status)
    if mode == "excel_to_xmind":
        output, output_name, _ = build_xmind(result["source_rows"], filename, PATH_SEPARATOR, APP_VERSION)
        media_type = "application/vnd.xmind.workbook"
    elif mode == "xmind_to_excel" and output_mode == "before":
        output = export_source_workbook(result["target_rows"], result["sheet_name"])
        output_name = xmind_xlsx_name(filename, "before")
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        output = export_workbook(result["target_rows"], TEMPLATE)
        output_name = xmind_xlsx_name(filename, "after") if mode.startswith("xmind_to") else safe_output_name(filename)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return result, output, output_name, media_type

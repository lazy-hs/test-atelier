"""Deterministic document windows for the Harness large-input workflow.

The legacy skill runner already processes large requirement packs in bounded
windows.  Harness tasks need the same source-preserving plan before a map/reduce
transport is enabled, so this module keeps the chunking rules independent from
HTTP, SQLAlchemy and any model provider.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True)
class HarnessChunk:
    """One source-preserving analysis window."""

    document_id: str
    filename: str
    ordinal: int
    start: int
    end: int
    text: str

    @property
    def locator(self) -> str:
        return f"字符 {self.start}-{self.end}"


def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def chunk_limits() -> tuple[int, int, int]:
    """Return window size, overlap and total-window limit."""

    size = _bounded_env("TEST_ATELIER_HARNESS_CHUNK_CHARS", 12_000, 1_000, 64_000)
    overlap = _bounded_env("TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", 400, 0, size // 2)
    maximum = _bounded_env("TEST_ATELIER_HARNESS_MAX_CHUNKS", 128, 1, 2_000)
    return size, overlap, maximum


def _window_end(text: str, start: int, proposed: int) -> int:
    """Prefer a paragraph or line boundary without exceeding the window."""

    if proposed >= len(text):
        return len(text)
    paragraph = text.rfind("\n\n", start, proposed)
    line = text.rfind("\n", start, proposed)
    boundary = max(paragraph + 2 if paragraph >= 0 else -1, line + 1 if line >= 0 else -1)
    if boundary > start + proposed // 3:
        return boundary
    return proposed


def plan_document_chunks(documents: list[dict]) -> list[HarnessChunk]:
    """Split extracted documents while retaining exact source offsets.

    The function does not truncate input.  Callers can use the returned count
    to decide whether to run a bounded map/reduce task or ask the user to split
    the material into separate tasks.
    """

    size, overlap, maximum = chunk_limits()
    chunks: list[HarnessChunk] = []
    for document in documents:
        document_id = str(document.get("id") or "").strip()
        filename = str(document.get("filename") or document_id or "document")
        text = str(document.get("text") or "")
        if not document_id or not text.strip():
            continue
        start = 0
        ordinal = 0
        while start < len(text):
            end = _window_end(text, start, min(len(text), start + size))
            if end <= start:
                end = min(len(text), start + size)
            chunks.append(HarnessChunk(document_id, filename, ordinal, start, end, text[start:end]))
            ordinal += 1
            if len(chunks) > maximum:
                raise ValueError(f"Harness 文档超过单次分块上限（最多 {maximum} 个窗口）")
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks


def _transport_byte_ranges(data: bytes, max_bytes: int, json_max_bytes: int | None = None) -> Iterator[tuple[int, int]]:
    if max_bytes < 4:
        raise ValueError("Harness 传输窗口过小")
    start = 0
    while start < len(data):
        end = min(start + max_bytes, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        if end <= start:
            raise ValueError("Harness 传输窗口无法容纳 UTF-8 字符")
        if json_max_bytes is not None:
            while True:
                escaped_size = len(json.dumps(data[start:end].decode("utf-8"), ensure_ascii=False).encode("utf-8"))
                if escaped_size <= json_max_bytes:
                    break
                proposed = start + max(4, (end - start) * json_max_bytes // escaped_size - 4)
                end = min(end - 1, proposed)
                while end < len(data) and data[end] & 0xC0 == 0x80:
                    end -= 1
                if end <= start:
                    raise ValueError("Harness JSON 传输窗口无法容纳 UTF-8 字符")
        yield start, end
        start = end


def count_transport_chunks(data: bytes, max_bytes: int = 768 * 1024, json_max_bytes: int | None = None) -> int:
    """Count the exact UTF-8 upload windows without re-encoding the source."""

    return sum(1 for _ in _transport_byte_ranges(data, max_bytes, json_max_bytes))


def iter_transport_chunks(text: str | bytes, max_bytes: int = 768 * 1024,
                          json_max_bytes: int | None = None) -> Iterator[str]:
    """Split text or already encoded UTF-8 for the Host's staged input API."""

    data = text.encode("utf-8") if isinstance(text, str) else text
    for start, end in _transport_byte_ranges(data, max_bytes, json_max_bytes):
        yield data[start:end].decode("utf-8")


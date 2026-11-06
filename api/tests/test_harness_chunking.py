import pytest
import httpx

from api.harness_chunking import count_transport_chunks, iter_transport_chunks, plan_document_chunks
from api.harness_routes import _staged_input_manifest


def test_chunk_plan_preserves_offsets_and_source_ids(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_CHARS", "1000")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", "30")
    text = "第一段需求。" * 220
    chunks = plan_document_chunks([{"id": "doc-1", "filename": "需求.md", "text": text}])

    assert len(chunks) > 1
    assert all(item.document_id == "doc-1" for item in chunks)
    assert [item.ordinal for item in chunks] == list(range(len(chunks)))
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)
    assert all(item.start < item.end for item in chunks)
    assert all(item.locator.startswith("字符 ") for item in chunks)


def test_chunk_plan_prefers_line_boundaries(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_CHARS", "256")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", "0")
    chunks = plan_document_chunks([{"id": "doc-2", "filename": "r.txt", "text": ("123456\n" + "abcdef\n" + "xyz\n") * 40}])

    assert chunks[0].text.startswith("123456\nabcdef\nxyz\n")
    assert all(item.text.endswith("\n") or item.end == len(("123456\n" + "abcdef\n" + "xyz\n") * 40) for item in chunks)


def test_chunk_plan_enforces_total_window_limit(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_CHARS", "1000")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_CHUNKS", "2")
    try:
        plan_document_chunks([{"id": "doc-3", "filename": "r.txt", "text": "x" * 2501}])
    except ValueError as exc:
        assert "最多 2 个窗口" in str(exc)
    else:
        raise AssertionError("expected the chunk limit to fail closed")


def test_transport_chunks_preserve_utf8_across_byte_boundaries():
    text = "你好🙂" * 1000
    chunks = list(iter_transport_chunks(text, 11))
    assert len(chunks) > 1
    assert all(0 < len(chunk.encode("utf-8")) <= 11 for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.parametrize("size", [4, 5, 8, 11, 16, 1024, 768 * 1024])
def test_manifest_chunk_count_matches_uploaded_utf8_windows(size):
    text = "你好🙂a\n" * 8000
    chunks = list(iter_transport_chunks(text, size))
    assert count_transport_chunks(text.encode("utf-8"), size) == len(chunks)
    assert list(iter_transport_chunks(text.encode("utf-8"), size)) == chunks
    assert "".join(chunks) == text


@pytest.mark.parametrize("text", ["\x00" * 70000, ("\t\n\\\"🙂" * 12000)])
def test_staged_manifest_accounts_for_json_escaping_before_host_upload(text):
    limit = 64 * 1024
    manifest, chunk_bytes, encoded = _staged_input_manifest([{"id": "source", "text": text}], limit)
    parts = list(iter_transport_chunks(encoded[0], chunk_bytes, limit - 4096))
    assert len(parts) == manifest[0]["chunks"]
    assert "".join(parts) == text
    for index, part in enumerate(parts):
        request = httpx.Request("POST", "http://127.0.0.1:8787/v1/tasks/test/input/chunks",
                                json={"document_index": 0, "chunk_index": index, "text": part})
        assert len(request.content) <= limit


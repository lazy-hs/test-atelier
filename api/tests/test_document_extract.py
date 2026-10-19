import io
import subprocess
import zipfile

import pytest

from api import document_extract


def test_legacy_word_conversion_timeout_is_user_facing(monkeypatch):
    monkeypatch.setattr(document_extract.shutil, "which", lambda name: "/usr/bin/soffice")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs.get("timeout", 90))

    monkeypatch.setattr(document_extract, "_run_external", timeout)

    with pytest.raises(document_extract.DocumentExtractTimeout, match="转换超时"):
        document_extract.extract("需求.doc", b"not-a-real-doc")


def test_legacy_word_conversion_timeout_does_not_echo_process_output(monkeypatch):
    monkeypatch.setattr(document_extract.shutil, "which", lambda name: "/usr/bin/soffice")

    def timeout(*args, **kwargs):
        error = subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs.get("timeout", 90))
        error.stderr = b"password=secret document body"
        raise error

    monkeypatch.setattr(document_extract, "_run_external", timeout)

    with pytest.raises(ValueError) as error:
        document_extract.extract("需求.doc", b"not-a-real-doc")
    assert "secret" not in str(error.value)
    assert "document body" not in str(error.value)


def test_document_tools_do_not_inherit_application_or_provider_secrets(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:password@db/app")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/tmp/desktop.sock")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = document_extract._tool_environment()

    assert "AI_STUDIO_SESSION_SECRET" not in environment
    assert "DATABASE_URL" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_APP_TOOLS_PIPE_PATH" not in environment
    assert environment["PATH"] == "/usr/bin"


def test_docx_path_traversal_is_rejected_before_parser():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")
    with pytest.raises(ValueError, match="损坏或无法读取"):
        document_extract.extract("需求.docx", stream.getvalue())


def test_docx_archive_entry_count_is_bounded():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for index in range(document_extract.MAX_DOCX_ENTRIES + 1):
            archive.writestr(f"word/entry-{index}.xml", "x")
    with pytest.raises(ValueError, match="损坏或无法读取"):
        document_extract.extract("需求.docx", stream.getvalue())


def test_image_decompression_bomb_is_user_facing(monkeypatch):
    def bomb(*_args, **_kwargs):
        raise document_extract.Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(document_extract.Image, "open", bomb)
    with pytest.raises(ValueError, match="图片损坏或格式不支持"):
        document_extract.extract("需求.png", b"not-an-image")


def test_pdfium_renderer_is_closed_after_ocr_pages(monkeypatch):
    class FakePdfPage:
        def extract_text(self):
            return ""

    class FakeReader:
        pages = [FakePdfPage()]
        is_encrypted = False

    class FakeRenderedImage:
        def save(self, target, format):
            target.write(b"png")

    class FakeRenderedPage:
        def render(self, scale):
            return self

        def to_pil(self):
            return FakeRenderedImage()

    class FakeRenderer:
        def __init__(self, _content):
            self.closed = False

        def __getitem__(self, _index):
            return FakeRenderedPage()

        def close(self):
            self.closed = True

    renderer = FakeRenderer(b"")
    monkeypatch.setattr(document_extract, "PdfReader", lambda _content: FakeReader())
    monkeypatch.setattr(document_extract.pdfium, "PdfDocument", lambda _content: renderer)
    monkeypatch.setattr(document_extract, "_ocr", lambda _content: ("识别文本", None))

    text, warnings = document_extract.extract("需求.pdf", b"pdf")

    assert text == "[第 1 页]\n识别文本"
    assert any(item.startswith("第 1 页使用 OCR") for item in warnings)
    assert renderer.closed is True

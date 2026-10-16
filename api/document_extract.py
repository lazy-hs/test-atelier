"""Extract reviewable text without invoking a model or trusting document instructions."""

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from docx import Document
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

ALLOWED = {".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".doc", ".docx"}


def _ocr(content: bytes) -> tuple[str, str | None]:
    executable = shutil.which("tesseract")
    if not executable:
        return "", "本机未安装 Tesseract，图片或扫描页无法识别；请安装中文和英文语言包后重新上传"
    try:
        result = subprocess.run([executable, "stdin", "stdout", "-l", "chi_sim+eng"], input=content,
                                capture_output=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        return "", "OCR 超时，请使用更清晰或更小的图片"
    if result.returncode:
        return "", "OCR 失败，请检查 Tesseract 的 chi_sim 与 eng 语言包"
    return result.stdout.decode("utf-8", errors="replace").strip(), None


def _docx(content: bytes) -> str:
    document = Document(io.BytesIO(content))
    chunks = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            chunks.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(chunks)


def extract(filename: str, content: bytes) -> tuple[str, list[str]]:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("仅支持 Markdown、文本、图片、PDF 和 Word 文档")
    warnings = []
    if suffix in {".md", ".markdown", ".txt"}:
        text = content.decode("utf-8-sig", errors="replace")
        if "\ufffd" in text:
            warnings.append("部分字符无法按 UTF-8 解码，请核对原文")
    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            image = Image.open(io.BytesIO(content))
            if image.width * image.height > 30_000_000:
                raise ValueError("图片像素超过限制")
            image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("图片损坏或格式不支持") from exc
        text, warning = _ocr(content)
        if warning:
            warnings.append(warning)
    elif suffix == ".doc":
        executable = shutil.which("soffice")
        if not executable:
            raise ValueError("读取旧版 .doc 需要 LibreOffice，请安装后重试")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "source.doc").write_bytes(content)
            proc = subprocess.run([executable, f"-env:UserInstallation={(path / 'profile').as_uri()}",
                                   "--headless", "--convert-to", "docx", "--outdir", directory, str(path / "source.doc")],
                                  capture_output=True, timeout=90, check=False)
            converted = path / "source.docx"
            if proc.returncode or not converted.exists():
                raise ValueError("旧版 Word 转换失败，请核对文件是否损坏")
            text = _docx(converted.read_bytes())
        warnings.append("旧版 Word 经 LibreOffice 转换，请核对表格与版式内容")
    elif suffix == ".docx":
        try:
            text = _docx(content)
        except Exception as exc:
            raise ValueError("Word 文档损坏或无法读取") from exc
    else:
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("加密 PDF 暂不支持")
            if len(reader.pages) > 100:
                raise ValueError("PDF 超过 100 页限制")
            renderer = None
            pages = []
            recognized = False
            for index, page in enumerate(reader.pages):
                page_text = (page.extract_text() or "").strip()
                if len(page_text) < 20:
                    renderer = renderer or pdfium.PdfDocument(content)
                    image = renderer[index].render(scale=2).to_pil()
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    ocr_text, warning = _ocr(buffer.getvalue())
                    if warning:
                        warnings.append(f"第 {index + 1} 页：{warning}")
                    if ocr_text:
                        page_text = ocr_text
                        warnings.append(f"第 {index + 1} 页使用 OCR，请核对识别内容")
                recognized = recognized or bool(page_text)
                pages.append(f"[第 {index + 1} 页]\n{page_text}")
            text = "\n\n".join(pages) if recognized else ""
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF 损坏或无法读取") from exc
    if not text.strip():
        warnings.append("未提取到可分析文字，不能启动 AI 分析")
    return text[:400_000], warnings

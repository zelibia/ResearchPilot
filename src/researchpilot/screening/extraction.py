"""PDF/image text and metadata extraction with an optional OCR port."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..literature.papers import ParsedPDF, parse_pdf
from ..storage.v116.normalization import normalize_doi, normalize_title, title_quality
from .contracts import CandidateMetadata

_ABSTRACT_RE = re.compile(
    r"(?is)\babstract\b\s*[:.\-]?\s*(.*?)(?=\bkeywords?\b|\b1\.?\s*introduction\b|\bintroduction\b)",
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")


class ExtractionFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OCRPort(Protocol):
    @property
    def available(self) -> bool: ...

    def extract_pdf(self, path: Path, *, max_pages: int = 20) -> list[str]: ...

    def extract_image(self, path: Path) -> str: ...


class TesseractOCR:
    """Render pages with PyMuPDF and invoke the local Tesseract binary."""

    def __init__(self, *, language: str = "") -> None:
        self.language = language or os.environ.get("TESSERACT_LANG", "").strip()

    @property
    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def extract_pdf(self, path: Path, *, max_pages: int = 20) -> list[str]:
        if not self.available:
            raise ExtractionFailure("OCR_UNAVAILABLE", "系统未安装 tesseract")
        try:
            import pymupdf
        except ImportError as exc:
            raise ExtractionFailure("OCR_UNAVAILABLE", "未安装 PyMuPDF") from exc
        pages: list[str] = []
        with tempfile.TemporaryDirectory(prefix="rp_screen_ocr_") as tmp:
            with pymupdf.open(path) as doc:
                for index in range(min(doc.page_count, max_pages)):
                    pix = doc.load_page(index).get_pixmap(matrix=pymupdf.Matrix(2, 2))
                    image = Path(tmp) / f"page-{index + 1}.png"
                    pix.save(image)
                    pages.append(self._run(image))
        return pages

    def extract_image(self, path: Path) -> str:
        if not self.available:
            raise ExtractionFailure("OCR_UNAVAILABLE", "系统未安装 tesseract")
        return self._run(path)

    def _run(self, image: Path) -> str:
        command = ["tesseract", str(image), "stdout"]
        if self.language:
            command.extend(["-l", self.language])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExtractionFailure("OCR_FAILED", f"OCR 执行失败：{exc}") from exc
        if completed.returncode != 0:
            raise ExtractionFailure(
                "OCR_FAILED",
                f"OCR 执行失败：{completed.stderr.strip()[:300]}",
            )
        return completed.stdout or ""


@dataclass
class ExtractionResult:
    page_count: int
    pages: list[str]
    metadata: CandidateMetadata
    provenance: dict[str, dict[str, object]]
    warnings: list[str] = field(default_factory=list)
    text_layer_status: str = "missing"
    language: str = ""
    fulltext_char_count: int = 0
    abstract: str = ""
    ocr_used: bool = False
    ocr_pages: list[str] = field(default_factory=list)


def _source_name(source: str) -> str:
    return {
        "embedded": "pdf_embedded",
        "first_page": "first_page",
        "filename": "filename",
        "body": "body",
        "manual": "manual",
        "crossref": "crossref",
        "openalex": "openalex",
        "ocr": "ocr",
    }.get(source, "legacy")


def _author_list(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    parts = re.split(r"\s*[;；]\s*|\s+and\s+", text)
    return [item.strip(" ,") for item in parts if item.strip(" ,")]


def _extract_abstract(pages: list[str]) -> str:
    if not pages:
        return ""
    match = _ABSTRACT_RE.search(pages[0])
    if not match:
        return ""
    abstract = re.sub(r"\s+", " ", match.group(1)).strip()
    return abstract[:5000]


def _detect_language(text: str) -> str:
    if not text.strip():
        return ""
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    if cjk >= 20 and cjk >= letters * 0.15:
        return "zh"
    return "en" if letters else ""


def _local_venue(parsed: ParsedPDF) -> tuple[str, str]:
    for key in ("journal", "container-title", "subject", "source"):
        value = str(parsed.metadata.get(key) or "").strip()
        if value and len(value) <= 250 and value.lower() not in {
            "microsoft word",
            "latex",
            "pdf",
        }:
            return value, "embedded"
    first = re.sub(r"\s+", " ", parsed.first_page[:1200])
    match = re.search(
        r"(?:journal|proceedings|transactions)\s+of\s+[A-Za-z &]{3,80}",
        first,
        re.IGNORECASE,
    )
    return (match.group(0).strip(), "first_page") if match else ("", "")


def metadata_from_pdf(
    parsed: ParsedPDF,
    *,
    original_filename: str,
    ocr_pages: list[str] | None = None,
) -> tuple[CandidateMetadata, dict[str, dict[str, object]], list[str]]:
    pages = list(ocr_pages or parsed.pages)
    text = "\n".join(pages)
    warnings = list(parsed.warnings)
    provenance: dict[str, dict[str, object]] = {}

    title = parsed.title_guess.strip()
    title_source = parsed.title_source or parsed.guess_sources.get("title", "")
    if not title:
        stem = Path(original_filename).stem.replace("_", " ").replace("-", " ").strip()
        if title_quality(stem):
            title = stem
            title_source = "filename"
    if title:
        provenance["title"] = {
            "source": _source_name("ocr" if ocr_pages and title_source != "filename" else title_source),
            "confidence": 0.65 if title_source == "first_page" else 0.9,
        }

    authors_raw = parsed.authors_guess.strip()
    if authors_raw:
        provenance["authors"] = {
            "source": _source_name("ocr" if ocr_pages else parsed.guess_sources.get("authors", "")),
            "confidence": 0.55 if parsed.guess_sources.get("authors") == "embedded" else 0.7,
        }

    doi = normalize_doi(parsed.doi_guess or (_DOI_RE.search(text[:12000]).group(0) if _DOI_RE.search(text[:12000]) else ""))
    if doi:
        provenance["doi"] = {
            "source": _source_name("ocr" if ocr_pages else parsed.guess_sources.get("doi", "")),
            "confidence": 0.8,
        }

    year = parsed.year_guess or (_YEAR_RE.search(text[:8000]).group(0) if _YEAR_RE.search(text[:8000]) else "")
    if year:
        provenance["year"] = {
            "source": _source_name("ocr" if ocr_pages else parsed.guess_sources.get("year", "")),
            "confidence": 0.65,
        }

    venue, venue_source = _local_venue(parsed)
    if venue:
        provenance["venue"] = {"source": _source_name(venue_source), "confidence": 0.55}

    abstract = _extract_abstract(pages)
    if abstract:
        provenance["abstract"] = {
            "source": _source_name("ocr" if ocr_pages else "body"),
            "confidence": 0.75,
        }

    metadata = CandidateMetadata(
        title=title,
        titleRaw=title,
        authors=_author_list(authors_raw),
        authorsRaw=authors_raw,
        year=year,
        venue=venue,
        doi=doi,
        abstract=abstract,
        language=_detect_language(text),
        externalIds={"doi": doi} if doi else {},
        sourceUrls=[],
        entryType="article",
    )
    if not title:
        warnings.append("未识别到可用标题，需要人工补充后才能自动评估")
    if not text.strip():
        warnings.append("没有可读正文，只能进行元数据级评估")
    return metadata, provenance, list(dict.fromkeys(warnings))


def extract_document(
    path: Path,
    *,
    file_name: str,
    source_kind: str,
    ocr: OCRPort | None = None,
) -> ExtractionResult:
    ocr = ocr or TesseractOCR()
    if source_kind == "pdf":
        try:
            parsed = parse_pdf(path, max_pages=60)
        except Exception as exc:
            raise ExtractionFailure("PDF_PARSE_FAILED", f"PDF 解析失败：{exc}") from exc
        pages = list(parsed.pages)
        warnings = list(parsed.warnings)
        ocr_used = False
        if sum(len(re.sub(r"\s+", "", page)) for page in pages) < 20:
            if ocr.available:
                try:
                    ocr_pages = ocr.extract_pdf(path)
                except ExtractionFailure as exc:
                    warnings.append(str(exc))
                    ocr_pages = []
                if ocr_pages:
                    pages = ocr_pages
                    ocr_used = True
            else:
                warnings.append("未检测到文本层，且 OCR 不可用")
        metadata, provenance, meta_warnings = metadata_from_pdf(
            parsed,
            original_filename=file_name,
            ocr_pages=pages if ocr_used else None,
        )
        warnings.extend(meta_warnings)
        char_count = sum(len(re.sub(r"\s+", "", page)) for page in pages)
        text_layer = (
            "available"
            if char_count >= 20 and ocr_used
            else "available"
            if char_count >= 20 and not warnings
            else "partial"
            if char_count >= 20
            else "missing"
        )
        if not metadata.title:
            raise ExtractionFailure(
                "METADATA_TITLE_MISSING",
                "未识别到标题；可直接补录元数据，或重试 OCR/解析",
            )
        return ExtractionResult(
            page_count=parsed.page_count,
            pages=pages,
            metadata=metadata,
            provenance=provenance,
            warnings=list(dict.fromkeys(warnings)),
            text_layer_status=text_layer,
            language=metadata.language,
            fulltext_char_count=char_count,
            abstract=metadata.abstract,
            ocr_used=ocr_used,
            ocr_pages=pages if ocr_used else [],
        )

    if source_kind == "image":
        if not ocr.available:
            raise ExtractionFailure("OCR_UNAVAILABLE", "当前环境未安装 OCR，无法解析图片")
        try:
            text = ocr.extract_image(path)
        except ExtractionFailure:
            raise
        pages = [text]
        pseudo = ParsedPDF(path=str(path), page_count=1, pages=pages)
        metadata, provenance, warnings = metadata_from_pdf(
            pseudo,
            original_filename=file_name,
            ocr_pages=pages,
        )
        if not metadata.title:
            first_line = next((line.strip() for line in text.splitlines() if len(line.strip()) >= 8), "")
            if first_line:
                metadata.title = first_line[:300]
                metadata.titleRaw = metadata.title
                provenance["title"] = {"source": "ocr", "confidence": 0.55}
        if not metadata.title:
            raise ExtractionFailure("METADATA_TITLE_MISSING", "图片 OCR 后仍未识别到标题")
        return ExtractionResult(
            page_count=1,
            pages=pages,
            metadata=metadata,
            provenance=provenance,
            warnings=warnings,
            text_layer_status="available",
            language=metadata.language,
            fulltext_char_count=len(re.sub(r"\s+", "", text)),
            abstract=metadata.abstract,
            ocr_used=True,
            ocr_pages=pages,
        )
    raise ExtractionFailure("UPLOAD_INVALID_TYPE", f"不支持的源类型：{source_kind}")

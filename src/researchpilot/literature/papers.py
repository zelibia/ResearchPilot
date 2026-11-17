"""PDF 解析。

原则：
1. 保留页码，任何抽取出来的内容都能回溯到第几页。
2. 解析不了就写进 warnings，不假装成功。扫描件、跨栏、公式属于"可能解析不完整"。
3. 元数据只做启发式猜测，一律标 verified=False，等外部检索核实后才置 True。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .text_quality import describe_controls, has_suspicious_controls

log = logging.getLogger(__name__)

DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def pymupdf_status() -> tuple[bool, str]:
    """返回 (是否可用, 版本号或缺失说明)。

    界面用它如实显示环境状态。缺包时必须能被一眼看出来，
    而不是等用户上传了 PDF 才在报错里发现。
    """
    try:
        import pymupdf

        return True, getattr(pymupdf, "__version__", "版本未知")
    except ImportError:
        pass
    try:
        import fitz  # 旧模块名，PyMuPDF < 1.24

        version = getattr(fitz, "VersionBind", None) or getattr(fitz, "__version__", "版本未知")
        return True, f"{version}（旧模块名 fitz）"
    except ImportError:
        return False, "未安装"


@dataclass
class ParsedPDF:
    path: str
    sha256: str = ""
    page_count: int = 0
    pages: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    title_guess: str = ""
    authors_guess: str = ""
    year_guess: str = ""
    doi_guess: str = ""
    warnings: list[str] = field(default_factory=list)
    # 每个猜测值来自哪里（embedded=PDF 内嵌元数据 / first_page=首页正文）。
    # 冲突报告里要说明依据：内嵌作者元数据经常是转存者或工具留下的，不可信。
    guess_sources: dict[str, str] = field(default_factory=dict)
    # 原始标题（不做任何清理）与它的来源，供入库后保留与追溯
    title_raw: str = ""
    title_source: str = ""

    @property
    def first_page(self) -> str:
        return self.pages[0] if self.pages else ""

    def excerpt(self, max_chars: int = 12000, pages: int = 6) -> str:
        """给 LLM 的正文节选，带页码标记，控制长度。"""
        chunks: list[str] = []
        total = 0
        for i, page in enumerate(self.pages[:pages], start=1):
            block = f"\n--- 第 {i} 页 ---\n{page}"
            chunks.append(block)
            total += len(block)
            if total >= max_chars:
                break
        return "".join(chunks)[:max_chars]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _guess_metadata(parsed: ParsedPDF) -> None:
    """从首页与 PDF 内嵌元数据里做保守猜测。全部标为未核实。"""
    first = parsed.first_page
    compact = re.sub(r"\s+", " ", first)

    doi = DOI_RE.search(compact)
    if doi:
        parsed.doi_guess = doi.group(0).rstrip(".,;")
        parsed.guess_sources["doi"] = "first_page"

    year = YEAR_RE.search(compact)
    if year:
        parsed.year_guess = year.group(0)
        parsed.guess_sources["year"] = "first_page"

    # 标题：内嵌元数据优先，否则取首页前几行里最像标题的一行
    embedded_title = (parsed.metadata.get("title") or "").strip()
    if len(embedded_title) > 8:
        parsed.title_guess = embedded_title
        parsed.guess_sources["title"] = "embedded"
    else:
        for line in first.splitlines()[:25]:
            candidate = line.strip()
            if 12 <= len(candidate) <= 200 and not DOI_RE.search(candidate):
                letters = sum(ch.isalpha() for ch in candidate)
                if letters >= len(candidate) * 0.6:
                    parsed.title_guess = candidate
                    parsed.guess_sources["title"] = "first_page"
                    break

    authors = (parsed.metadata.get("author") or "").strip()
    if authors:
        parsed.authors_guess = authors
        # 注意：PDF 内嵌的 /Author 经常是转存者、批注者或生成工具留下的，
        # 并不等于论文作者（Bensen 2007 的这份 PDF 里它就是 "neeraj"）。
        # 记为 embedded，让核查冲突时能说清依据。
        parsed.guess_sources["authors"] = "embedded"
    else:
        for line in first.splitlines()[:40]:
            candidate = line.strip()
            if 5 <= len(candidate) <= 250 and re.search(
                r"(and\s+[A-Z]|,\s*[A-Z][a-z]+\s+[A-Z]\.)", candidate
            ):
                parsed.authors_guess = candidate
                parsed.guess_sources["authors"] = "first_page"
                break


def parse_pdf(path: str | Path, max_pages: int = 60, sha256: str | None = None) -> ParsedPDF:
    """解析 PDF。缺 PyMuPDF 时抛出带指引的 RuntimeError，让上层显示真实原因。"""
    file_path = Path(path)
    parsed = ParsedPDF(path=str(file_path))
    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    try:
        import pymupdf  # PyMuPDF 1.24+ 的正式模块名
    except ImportError:
        try:
            import fitz as pymupdf  # 兼容旧版；fitz 已标记弃用
        except ImportError as exc:
            raise RuntimeError(
                "未安装 PyMuPDF，无法解析 PDF。\n"
                f"当前解释器：{sys.executable}\n"
                f"修复命令：{sys.executable} -m pip install pymupdf"
            ) from exc

    # 调用方已经算过 sha256 时直接复用，避免对大文件重复哈希。
    parsed.sha256 = sha256 or sha256_file(file_path)
    with pymupdf.open(file_path) as doc:
        parsed.page_count = doc.page_count
        if doc.page_count > max_pages:
            parsed.warnings.append(
                f"文档共 {doc.page_count} 页，本次只解析前 {max_pages} 页"
            )
        parsed.metadata = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        for index in range(min(doc.page_count, max_pages)):
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            if not text.strip():
                parsed.warnings.append(f"第 {index + 1} 页未提取到文本（可能是扫描件或纯图页）")
            parsed.pages.append(text)

    if not any(p.strip() for p in parsed.pages):
        parsed.warnings.append("整篇未提取到文本，PDF 可能是扫描件，需要 OCR 才能使用")

    _guess_metadata(parsed)

    # 标题质量检查：控制字符异常要如实写进 warnings，但原始值一律保留
    parsed.title_raw = parsed.title_guess
    parsed.title_source = parsed.guess_sources.get("title", "")
    if has_suspicious_controls(parsed.title_guess):
        parsed.warnings.append(describe_controls(parsed.title_guess))
    log.info(
        "解析 PDF %s：%d 页，%d 条警告",
        file_path.name,
        parsed.page_count,
        len(parsed.warnings),
    )
    return parsed

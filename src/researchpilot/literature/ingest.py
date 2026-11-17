"""把一篇 PDF 走完整的「去重 → 落地 → 解析 → 入库」路径导入项目。

界面（工作台 1）与命令行工具 `scripts/ingest_pdf.py` 共用这一条路径，
避免出现"界面能解析、脚本不能"或反过来的两套行为。

**这里只做本地解析**：抽文本、按页留存、猜元数据、算 sha256。
不需要大模型，也不需要联网。猜出来的元数据一律 `verified=False`。

结构化分析（研究问题 / 方法步骤 / 假设 / 局限）属于另一件事，由
`agent/llm.py` 负责，需要配置 LLM；本模块不生成任何解释性内容。

**幂等去重**（要求 C1-5）：同一份文件按 sha256 判重，同一篇文献按 DOI 判重
（DOI 大小写不敏感）。命中已有记录时不再新建 Paper，返回已有那条并说明依据。

**跨项目命中**（``matched_by="library"``）：同一篇文献已经在**别的项目**里时，
同样不新建记录，而是把那条库记录交回给调用方。要不要把它加入当前项目由用户决定
（界面给一个"加入本项目"的按钮，命令行只如实说明）。文献库的合并规则见
`literature/library.py`；这里只做判定，不复制记录。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import PATHS
from ..storage import repository as repo
from ..storage.models import Paper, Project
from . import library
from .normalize import normalize_doi
from .papers import parse_pdf, sha256_file

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """一次导入的结果。

    ``created=False`` 表示命中了已有记录，没有新建。``matched_by`` 说明命中的是
    哪一种：``sha256``（本项目同一份文件）、``doi``（本项目同一篇文献）、
    ``library``（**别的项目**里已有这篇文献）。``library`` 时 ``paper`` 是库里那条
    记录，它的 ``project_id`` 不是当前项目。
    """

    paper: Paper
    created: bool
    matched_by: str = ""   # ""（新建）/ "sha256" / "doi" / "library"
    note: str = ""
    # 命中库时，这条记录属于哪个项目（供界面显示"已在 XX 项目里"）
    library_project_id: str = ""


def find_paper_by_doi(project_id: str, doi: str) -> Paper | None:
    """按 DOI 找已有文献。DOI 大小写不敏感，比较前统一规范化。"""
    target = normalize_doi(doi)
    if not target:
        return None
    for paper in repo.list_all(Paper, project_id):
        if paper.doi and normalize_doi(paper.doi) == target:
            return paper
    return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_pages_text(file_path: Path, pages: list[str]) -> None:
    """正文按页留存，供后续 LLM 分析与报告引用页码。"""
    text_dump = file_path.with_suffix(".pages.txt")
    with text_dump.open("w", encoding="utf-8") as fh:
        for index, page in enumerate(pages, start=1):
            fh.write(f"\n===== 第 {index} 页 =====\n{page}")


def ingest_pdf_path(
    project: Project, path: str | Path, *, library_check: bool = True
) -> IngestResult:
    """解析磁盘上已有的 PDF 并入库。

    先按 sha256 判断是不是同一份文件，再按 DOI 判断是不是同一篇文献；
    两者命中任一都不新建 Paper。``library_check=False`` 只关掉"其它项目里是否
    已有这篇"的判定（项目内去重的单元测试用它），项目内去重不受影响。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    digest = sha256_file(file_path)   # 只算一次：去重与解析共用
    existing = repo.find_paper_by_sha256(project.id, digest)
    if existing is not None:
        note = f"文件内容与已有文献相同（sha256 一致），未重复入库：{existing.id}"
        log.info("跳过重复文件 %s：%s", file_path.name, note)
        return IngestResult(paper=existing, created=False, matched_by="sha256", note=note)

    parsed = parse_pdf(file_path, sha256=digest)

    duplicate = find_paper_by_doi(project.id, parsed.doi_guess)
    if duplicate is not None:
        where = f"，已有文件：{duplicate.local_path}" if duplicate.local_path else ""
        note = (
            f"DOI {normalize_doi(parsed.doi_guess)} 已存在于 {duplicate.id}"
            f"{where}，未重复入库"
        )
        log.info("跳过同 DOI 文献 %s：%s", file_path.name, note)
        return IngestResult(paper=duplicate, created=False, matched_by="doi", note=note)

    # 本项目里没有，但文献库里（别的项目）有：不新建记录，交回库记录。
    elsewhere = (
        library.find_in_other_projects(project.id, sha256=digest, doi=parsed.doi_guess)
        if library_check
        else None
    )
    if elsewhere is not None:
        note = "这篇文献已经在文献库里（其它项目用过），没有重复入库"
        log.info("命中文献库 %s：%s", file_path.name, note)
        return IngestResult(
            paper=elsewhere,
            created=False,
            matched_by="library",
            note=note,
            library_project_id=elsewhere.project_id,
        )

    paper = repo.save(
        Paper(
            id=repo.new_id("paper"),
            project_id=project.id,
            title=parsed.title_guess or file_path.stem,
            authors=parsed.authors_guess,
            year=parsed.year_guess,
            doi=parsed.doi_guess,
            verified=False,  # 启发式猜测，必须经外部检索核实后才能置 True
            source_urls=[],
            local_path=str(file_path),
            sha256=parsed.sha256,
            page_count=parsed.page_count,
            parse_warnings=parsed.warnings,
            guess_sources=dict(parsed.guess_sources),
            title_raw=parsed.title_raw,
            title_source=parsed.title_source,
        )
    )

    _write_pages_text(file_path, parsed.pages)

    log.info(
        "解析并入库存入 %s（%d 页，paper_id=%s）",
        file_path.name,
        parsed.page_count,
        paper.id,
    )
    return IngestResult(paper=paper, created=True)


def ingest_pdf_bytes(
    project: Project, filename: str, data: bytes, *, library_check: bool = True
) -> IngestResult:
    """把上传的字节落盘到 outputs/uploads/<project_id>/ 再入库。

    界面走这个入口，与命令行工具共用同一条解析与保存路径。
    内容重复时不落盘、不解析、不新建记录。
    """
    digest = _sha256_bytes(data)
    existing = repo.find_paper_by_sha256(project.id, digest)
    if existing is not None:
        note = f"文件内容与已有文献相同（sha256 一致），未重复入库：{existing.id}"
        log.info("跳过重复上传 %s：%s", filename, note)
        return IngestResult(paper=existing, created=False, matched_by="sha256", note=note)

    # 硬依据先行：文件内容一致的记录若在别的项目里，连落盘都不必做。
    elsewhere = (
        library.find_in_other_projects(project.id, sha256=digest)
        if library_check
        else None
    )
    if elsewhere is not None:
        note = "这篇文献已经在文献库里（其它项目用过），没有重复入库"
        log.info("上传命中文献库 %s：%s", filename, note)
        return IngestResult(
            paper=elsewhere,
            created=False,
            matched_by="library",
            note=note,
            library_project_id=elsewhere.project_id,
        )

    target_dir = PATHS.uploads / project.id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(filename).name
    target.write_bytes(data)

    result = ingest_pdf_path(project, target, library_check=library_check)
    if not result.created and result.matched_by in {"doi", "library"}:
        # 同一篇文献的另一份文件：原文已有记录，这份刚写进去的副本不再保留，
        # 否则 uploads 里会堆积同一篇文献的多个副本。
        try:
            target.unlink()
            result.note += "；刚上传的副本已删除"
        except OSError as exc:  # pragma: no cover - 文件系统异常
            log.warning("删除重复副本失败：%s", exc)
    return result


def attach_pdf_bytes_to_paper(
    paper: Paper,
    filename: str,
    data: bytes,
) -> Paper:
    """Attach a user-provided PDF to an existing metadata-only Paper record.

    This is intentionally different from ``ingest_pdf_bytes``: a duplicate DOI
    must fill the existing recommendation instead of creating or discarding a
    second Paper record.
    """
    if not data.startswith(b"%PDF-"):
        raise ValueError("上传内容不是 PDF")
    digest = _sha256_bytes(data)
    target_dir = PATHS.uploads / paper.project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(filename or paper.id).stem[:80] or paper.id
    target = target_dir / f"{stem}_{digest[:8]}.pdf"
    temporary = target.with_suffix(".part")
    temporary.write_bytes(data)
    try:
        parsed = parse_pdf(temporary, sha256=digest)
        uploaded_doi = normalize_doi(parsed.doi_guess)
        existing_doi = normalize_doi(paper.doi)
        if uploaded_doi and existing_doi and uploaded_doi != existing_doi:
            raise ValueError(
                f"上传文件的 DOI（{uploaded_doi}）与当前文献"
                f"（{existing_doi}）不一致。"
            )
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    _write_pages_text(target, parsed.pages)
    paper.local_path = str(target)
    paper.sha256 = digest
    paper.page_count = parsed.page_count
    paper.parse_warnings = list(parsed.warnings)
    if not paper.title and parsed.title_guess:
        paper.title = parsed.title_guess
        paper.title_raw = parsed.title_raw
        paper.title_source = parsed.title_source
    if not paper.authors and parsed.authors_guess:
        paper.authors = parsed.authors_guess
        paper.authors_raw = parsed.authors_guess
        paper.author_source = "pdf:upload"
    if not paper.year and parsed.year_guess:
        paper.year = parsed.year_guess
    if not paper.doi and parsed.doi_guess:
        paper.doi = parsed.doi_guess
    repo.save(paper)
    log.info(
        "本地 PDF 已附到已有文献 %s（%d 页）",
        paper.id,
        parsed.page_count,
    )
    return paper

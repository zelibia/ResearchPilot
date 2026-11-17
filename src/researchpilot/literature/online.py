"""Lawful online reading for AI-recommended papers without a local PDF.

Only OpenAlex metadata, its public abstract, and locations explicitly marked
as open access are used. Publisher challenge pages and paywalled HTML are
rejected as non-PDF content.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import PATHS
from ..storage import repository as repo
from ..storage.models import Paper, QueryKind, QueryStatus, SourceRecord
from . import openalex
from .net import BibliographyUnavailable, CachedBinary, CachedResponse, HttpClient
from .normalize import normalize_doi
from .papers import parse_pdf

log = logging.getLogger(__name__)

MAX_ONLINE_PDF_BYTES = 30 * 1024 * 1024


class OnlineSourceUnavailable(RuntimeError):
    """No lawful online source can be read for this paper."""


@dataclass
class OnlineSource:
    work: openalex.OpenAlexWork
    query_value: str
    response: CachedResponse
    source_record_id: str = ""

    @property
    def abstract(self) -> str:
        return self.work.abstract

    @property
    def pdf_url(self) -> str:
        return self.work.pdf_url if self.work.is_oa else ""

    @property
    def landing_url(self) -> str:
        return self.work.landing_url or (
            f"https://doi.org/{self.work.doi}" if self.work.doi else ""
        )

    @property
    def license(self) -> str:
        return self.work.license


def _record_lookup(
    paper: Paper,
    source: OnlineSource,
) -> str:
    record = repo.save(SourceRecord(
        id=repo.new_id("src"),
        project_id=paper.project_id,
        paper_id=paper.id,
        provider=openalex.PROVIDER,
        query_kind=QueryKind.WORK_LOOKUP,
        query_value=source.query_value,
        external_id=source.work.openalex_id,
        fetched_at=source.response.fetched_at,
        status=QueryStatus.OK,
        from_cache=source.response.from_cache,
        stale=source.response.stale,
        http_status=source.response.status_code,
        cache_path=source.response.cache_path,
        result_count=1,
        note=(
            f"online reading; abstract={bool(source.abstract)}; "
            f"open_access={source.work.is_oa}"
        ),
    ))
    return record.id


def fetch_online_source(
    paper: Paper, client: HttpClient | None = None
) -> OnlineSource:
    """Resolve DOI/OpenAlex ID to metadata, abstract, and OA locations."""
    client = client or HttpClient()
    doi = normalize_doi(paper.doi)
    openalex_id = str((paper.external_ids or {}).get("openalex") or "")
    if not doi and not openalex_id:
        raise OnlineSourceUnavailable(
            "这篇推荐文献没有 DOI 或 OpenAlex ID，无法在线定位原文。"
        )
    query_value = doi or openalex_id
    try:
        if doi:
            work, response = openalex.fetch_by_doi(doi, client)
        else:
            work, response = openalex.fetch_by_id(openalex_id, client)
    except BibliographyUnavailable as exc:
        raise OnlineSourceUnavailable(f"OpenAlex 查询失败：{exc}") from exc
    if work is None:
        raise OnlineSourceUnavailable(
            f"OpenAlex 没有找到这篇文献：{query_value}"
        )
    source = OnlineSource(work=work, query_value=query_value, response=response)
    source.source_record_id = _record_lookup(paper, source)
    return source


def download_open_pdf(
    source: OnlineSource,
    client: HttpClient | None = None,
) -> CachedBinary:
    """Download an explicitly open-access PDF; reject HTML and paywalls."""
    if not source.work.is_oa or not source.pdf_url:
        raise OnlineSourceUnavailable("OpenAlex 没有提供开放获取 PDF。")
    client = client or HttpClient()
    try:
        binary = client.get_bytes(
            openalex.PROVIDER,
            source.pdf_url,
            max_bytes=MAX_ONLINE_PDF_BYTES,
        )
    except BibliographyUnavailable as exc:
        raise OnlineSourceUnavailable(f"开放全文下载失败：{exc}") from exc
    if not binary.ok:
        raise OnlineSourceUnavailable(
            f"开放全文下载失败：HTTP {binary.status_code}"
        )
    if not binary.content.lstrip().startswith(b"%PDF-"):
        raise OnlineSourceUnavailable(
            "开放全文地址返回的不是 PDF，可能是出版社验证页或付费墙。"
        )
    return binary


def _write_pages(path: Path, pages: list[str]) -> None:
    text_path = path.with_suffix(".pages.txt")
    with text_path.open("w", encoding="utf-8") as handle:
        for index, page in enumerate(pages, start=1):
            handle.write(f"\n===== 第 {index} 页 =====\n{page}")


def attach_pdf_to_paper(
    paper: Paper,
    content: bytes,
    source: OnlineSource,
) -> Paper:
    """Attach an online OA PDF to the existing metadata-only Paper record."""
    if not content.startswith(b"%PDF-"):
        raise ValueError("下载内容不是 PDF")

    digest = hashlib.sha256(content).hexdigest()
    target_dir = PATHS.uploads / paper.project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"online_{paper.id}_{digest[:8]}.pdf"
    temporary = target.with_suffix(".part")
    temporary.write_bytes(content)
    try:
        parsed = parse_pdf(temporary, sha256=digest)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    _write_pages(target, parsed.pages)
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
        paper.author_source = "pdf:online"
    if not paper.year and parsed.year_guess:
        paper.year = parsed.year_guess
    if not paper.doi and parsed.doi_guess:
        paper.doi = parsed.doi_guess
    urls = list(paper.source_urls or [])
    for url in (source.pdf_url, source.landing_url):
        if url and url not in urls:
            urls.append(url)
    paper.source_urls = urls
    online_meta = dict(paper.external_metadata or {})
    online_meta["online_fulltext"] = {
        "provider": openalex.PROVIDER,
        "openalex_id": source.work.openalex_id,
        "pdf_url": source.pdf_url,
        "landing_url": source.landing_url,
        "license": source.license,
        "is_oa": source.work.is_oa,
        "fetched_at": source.response.fetched_at,
        "sha256": digest,
        "source_record_id": source.source_record_id,
    }
    paper.external_metadata = online_meta
    paper.summary = {
        **(paper.summary or {}),
        "online_source": {
            "status": "fulltext",
            "provider": openalex.PROVIDER,
            "pdf_url": source.pdf_url,
            "landing_url": source.landing_url,
            "license": source.license,
            "fetched_at": source.response.fetched_at,
            "source_record_id": source.source_record_id,
        },
    }
    repo.save(paper)
    log.info(
        "在线全文已附到 %s：%s（%d 页）",
        paper.id,
        source.pdf_url,
        parsed.page_count,
    )
    return paper

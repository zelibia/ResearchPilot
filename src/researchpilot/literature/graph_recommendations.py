"""OpenAlex discovery candidates for the citation graph.

Recommendations are intentionally not citation edges. They are transient
candidates until the user explicitly confirms one as a manually added relation.
Similar works come from OpenAlex ``related_works``. Co-cited works are counted
from references shared by works that cite the current paper.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..storage import repository as repo
from ..storage.models import (
    Paper,
    QueryKind,
    QueryStatus,
    ReviewStatus,
    SourceRecord,
)
from . import openalex
from .net import BibliographyUnavailable, HttpClient
from .normalize import normalize_doi
from .openalex import OpenAlexWork

log = logging.getLogger(__name__)


@dataclass
class GraphRecommendation:
    openalex_id: str
    title: str
    authors: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    cited_by_count: int = 0
    sources: list[str] = field(default_factory=list)
    evidence_count: int = 0

    @property
    def source_label(self) -> str:
        return " + ".join(dict.fromkeys(self.sources))

    def to_dict(self) -> dict[str, Any]:
        return {
            "openalex_id": self.openalex_id,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "cited_by_count": self.cited_by_count,
            "sources": list(self.sources),
            "evidence_count": self.evidence_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GraphRecommendation":
        return cls(
            openalex_id=str(value.get("openalex_id") or ""),
            title=str(value.get("title") or ""),
            authors=str(value.get("authors") or ""),
            year=str(value.get("year") or ""),
            venue=str(value.get("venue") or ""),
            doi=str(value.get("doi") or ""),
            cited_by_count=int(value.get("cited_by_count") or 0),
            sources=[str(item) for item in value.get("sources") or []],
            evidence_count=int(value.get("evidence_count") or 0),
        )

    def as_work(self) -> OpenAlexWork:
        return OpenAlexWork(
            openalex_id=self.openalex_id,
            doi=self.doi,
            title=self.title,
            authors=[item.strip() for item in self.authors.split(";") if item.strip()],
            year=self.year,
            venue=self.venue,
            cited_by_count=self.cited_by_count,
        )


@dataclass
class RecommendationReport:
    items: list[GraphRecommendation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)


def _record_source(
    paper: Paper,
    query_value: str,
    note: str,
    count: int,
    response: Any = None,
    status: QueryStatus = QueryStatus.OK,
) -> str:
    record = repo.save(SourceRecord(
        id=repo.new_id("src"),
        project_id=paper.project_id,
        paper_id=paper.id,
        provider=openalex.PROVIDER,
        query_kind=QueryKind.RELATED,
        query_value=query_value,
        external_id=paper.external_ids.get("openalex", ""),
        fetched_at=getattr(response, "fetched_at", ""),
        status=status,
        from_cache=bool(getattr(response, "from_cache", False)),
        stale=bool(getattr(response, "stale", False)),
        http_status=int(getattr(response, "status_code", 0) or 0),
        cache_path=str(getattr(response, "cache_path", "") or ""),
        result_count=count,
        note=note,
    ))
    return record.id


def _known_paper_ids(paper: Paper) -> tuple[set[str], set[str]]:
    """OpenAlex/DOI ids already in the project and rejected counterpart ids."""
    known: set[str] = set()
    rejected_papers: set[str] = set()
    papers = repo.list_all(Paper, paper.project_id)
    by_id = {item.id: item for item in papers}
    for item in papers:
        openalex_id = str(item.external_ids.get("openalex") or "").strip()
        doi = normalize_doi(item.doi)
        if openalex_id:
            known.add(openalex_id)
        if doi:
            known.add(doi)
    for relation in repo.list_paper_relations(paper.project_id):
        if relation.review_status is not ReviewStatus.REJECTED:
            continue
        other_id = (
            relation.to_paper_id
            if relation.from_paper_id == paper.id
            else relation.from_paper_id
        )
        other = by_id.get(other_id)
        if other is None:
            continue
        rejected_papers.add(other.id)
        openalex_id = str(other.external_ids.get("openalex") or "").strip()
        doi = normalize_doi(other.doi)
        if openalex_id:
            known.add(openalex_id)
        if doi:
            known.add(doi)
    return known, rejected_papers


def _is_known(work: OpenAlexWork, known: set[str]) -> bool:
    return bool(
        (work.openalex_id and work.openalex_id in known)
        or (normalize_doi(work.doi) and normalize_doi(work.doi) in known)
    )


def fetch_graph_recommendations(
    paper: Paper,
    client: HttpClient | None = None,
    limit: int = 10,
) -> RecommendationReport:
    """Fetch similar and co-cited works without writing citation relations."""
    client = client or HttpClient()
    limit = max(1, min(int(limit), 50))
    report = RecommendationReport()
    doi = normalize_doi(paper.doi)
    openalex_id = str(paper.external_ids.get("openalex") or "")
    query_value = doi or openalex_id
    if not query_value:
        report.errors.append("这篇文献没有 DOI 或 OpenAlex ID，无法拓展推荐。")
        return report

    try:
        if doi:
            work, response = openalex.fetch_by_doi(doi, client)
        else:
            work, response = openalex.fetch_by_id(openalex_id, client)
    except BibliographyUnavailable as exc:
        report.errors.append(f"OpenAlex：{exc}")
        report.source_record_ids.append(_record_source(
            paper, query_value, "获取当前论文失败", 0, status=QueryStatus.ERROR
        ))
        return report
    if work is None:
        report.errors.append(f"OpenAlex 没有这条记录：{query_value}")
        report.source_record_ids.append(_record_source(
            paper, query_value, "当前论文未找到", 0, response, QueryStatus.NOT_FOUND
        ))
        return report

    candidates: dict[str, GraphRecommendation] = {}

    related_ids = list(dict.fromkeys(work.related_works))[:limit]
    if related_ids:
        try:
            related, related_response = openalex.fetch_by_ids(
                related_ids, client, limit=limit
            )
        except (BibliographyUnavailable, ValueError) as exc:
            report.errors.append(f"相似文献查询失败：{exc}")
            report.source_record_ids.append(_record_source(
                paper, work.openalex_id, "相似文献查询失败", 0,
                status=QueryStatus.ERROR,
            ))
        else:
            report.source_record_ids.append(_record_source(
                paper, work.openalex_id, "similar: OpenAlex related_works",
                len(related), related_response,
            ))
            for item in related[:limit]:
                if not item.openalex_id:
                    continue
                candidates[item.openalex_id] = GraphRecommendation(
                    openalex_id=item.openalex_id,
                    title=item.title,
                    authors="; ".join(item.authors),
                    year=item.year,
                    venue=item.venue,
                    doi=item.doi,
                    cited_by_count=item.cited_by_count,
                    sources=["相似文献"],
                )

    # Co-citation: references repeatedly appearing beside the current paper in
    # works that cite it. We fetch at most 2*limit citing works to bound latency.
    try:
        citing, cite_response = openalex.fetch_cited_by(
            work.openalex_id, client, limit=max(limit, min(limit * 2, 25))
        )
    except (BibliographyUnavailable, ValueError) as exc:
        report.errors.append(f"共引文献查询失败：{exc}")
        report.source_record_ids.append(_record_source(
            paper, work.openalex_id, "共引文献查询失败", 0,
            status=QueryStatus.ERROR,
        ))
    else:
        counts: Counter[str] = Counter()
        for citing_work in citing:
            for ref_id in set(citing_work.referenced_works):
                if ref_id and ref_id != work.openalex_id:
                    counts[ref_id] += 1
        co_ids = [
            item_id for item_id, count in counts.most_common()
            if count >= 2
        ][:limit]
        if co_ids:
            try:
                co_cited, co_response = openalex.fetch_by_ids(
                    co_ids, client, limit=limit
                )
            except (BibliographyUnavailable, ValueError) as exc:
                report.errors.append(f"共引文献详情查询失败：{exc}")
                report.source_record_ids.append(_record_source(
                    paper, work.openalex_id, "共引文献详情查询失败", 0,
                    status=QueryStatus.ERROR,
                ))
            else:
                report.source_record_ids.append(_record_source(
                    paper, work.openalex_id, "co-citation: shared references",
                    len(co_cited), co_response,
                ))
                for item in co_cited:
                    if not item.openalex_id:
                        continue
                    existing = candidates.get(item.openalex_id)
                    if existing is None:
                        existing = GraphRecommendation(
                            openalex_id=item.openalex_id,
                            title=item.title,
                            authors="; ".join(item.authors),
                            year=item.year,
                            venue=item.venue,
                            doi=item.doi,
                            cited_by_count=item.cited_by_count,
                        )
                        candidates[item.openalex_id] = existing
                    if "共引文献" not in existing.sources:
                        existing.sources.append("共引文献")
                    existing.evidence_count = counts.get(item.openalex_id, 0)
        report.source_record_ids.append(_record_source(
            paper, work.openalex_id, "co-citation: candidate scan",
            len(co_ids), cite_response,
        ))

    known, rejected_papers = _known_paper_ids(paper)
    filtered = [
        item for item in candidates.values()
        if not _is_known(item.as_work(), known)
        and item.openalex_id not in known
    ]
    # Interleave sources so neither one starves the other, then apply the
    # top-level per-direction limit to the combined recommendation count.
    filtered.sort(
        key=lambda item: (
            -len(item.sources),
            -item.evidence_count,
            -item.cited_by_count,
            item.year,
            item.title,
        )
    )
    report.items = filtered[:limit]
    if len(candidates) > len(report.items):
        report.notes.append(
            f"已按每方向上限保留 {len(report.items)} 条，并去重已入库或已驳回文献"
        )
    log.info(
        "拓展推荐 %s：%s 条（相似/共引），排除已入库或已驳回 %s 条",
        paper.id, len(report.items), len(candidates) - len(report.items),
    )
    return report

"""把 OpenAlex 的引用关系落成项目内的关系数据（C2）。

三条铁律：

1. **方向明确**：`cites` 的 `from` 是施动方。
   参考文献边是 (本文 → 被引文献)；施引边是 (施引文献 → 本文)。
   写反了整张图就反了，所以方向有专门测试（T-C2-01/02）。
2. **引用事实与语义判断分开**：OpenAlex 只能提供 `cites`；
   `extends` / `compares` / `replaces` / `diverges` 属于判断，
   只能由人提出（模型提出的必须是 pending，且人确认后才可能变成已验证）。
3. **相关文献只当候选**：OpenAlex 的 `related_works` 是算法相似度，**不是引用**。
   把它们建成 `cites` 边就是编造，所以只导入成候选文献，一条边都不建。

每次拉取都写 `SourceRecord`，每条边记 `source_record_id`，
可以一直追到"哪次查询、什么时候、对方原始响应是什么"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..storage import repository as repo
from ..storage.models import (
    Certainty,
    Paper,
    PaperRelation,
    QueryKind,
    QueryStatus,
    RelationSource,
    RelationType,
    ReviewStatus,
    SourceRecord,
    VerificationStatus,
)
from . import openalex
from .ingest import find_paper_by_doi
from .net import BibliographyUnavailable, CachedResponse, HttpClient
from .normalize import normalize_doi
from .openalex import OpenAlexWork

log = logging.getLogger(__name__)


@dataclass
class CitationReport:
    paper_id: str
    openalex_id: str = ""
    references_fetched: int = 0
    citations_fetched: int = 0
    related_fetched: int = 0
    papers_imported: int = 0
    edges_created: int = 0
    edges_existing: int = 0
    source_record_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [
            f"参考文献 {self.references_fetched}",
            f"施引 {self.citations_fetched}",
        ]
        if self.related_fetched:
            parts.append(f"相关候选 {self.related_fetched}（只导入不建边）")
        parts.append(f"新增关系 {self.edges_created}")
        if self.edges_existing:
            parts.append(f"已存在 {self.edges_existing}")
        if self.papers_imported:
            parts.append(f"新导入候选文献 {self.papers_imported}")
        if self.errors:
            parts.append(f"错误 {len(self.errors)}")
        return "；".join(parts)


def _record_source(
    project_id: str,
    paper_id: str,
    kind: QueryKind,
    query_value: str,
    response: CachedResponse | None,
    status: QueryStatus = QueryStatus.OK,
    external_id: str = "",
    result_count: int = 0,
    note: str = "",
) -> str:
    record = repo.save(
        SourceRecord(
            id=repo.new_id("src"),
            project_id=project_id,
            paper_id=paper_id,
            provider=openalex.PROVIDER,
            query_kind=kind,
            query_value=query_value,
            external_id=external_id,
            fetched_at=response.fetched_at if response else "",
            status=status,
            from_cache=bool(response.from_cache) if response else False,
            stale=bool(response.stale) if response else False,
            http_status=response.status_code if response else 0,
            cache_path=response.cache_path if response else "",
            result_count=result_count,
            note=note,
        )
    )
    return record.id


def import_candidate_paper(project_id: str, work: OpenAlexWork) -> tuple[Paper, bool]:
    """把一条 OpenAlex 记录落成项目内的 Paper。

    这类记录标 `origin="openalex"`、`verification_status=unverified`：
    元数据来自可信来源，但**没有和论文本体核对过**，所以不冒充"已核实"。
    """
    existing = repo.find_paper_by_openalex_id(project_id, work.openalex_id)
    if existing is None and work.doi:
        existing = find_paper_by_doi(project_id, work.doi)
    if existing is not None:
        # 已有记录：只补外部 ID，不动用户可能已经改过的字段
        changed = False
        if work.openalex_id and existing.external_ids.get("openalex") != work.openalex_id:
            existing.external_ids["openalex"] = work.openalex_id
            changed = True
        if work.doi and not existing.external_ids.get("doi"):
            existing.external_ids["doi"] = work.doi
            changed = True
        if changed:
            repo.save(existing)
        return existing, False

    paper = repo.save(
        Paper(
            id=repo.new_id("paper"),
            project_id=project_id,
            title=work.title or "（OpenAlex 未给出标题）",
            authors="; ".join(work.authors),
            year=work.year,
            venue=work.venue,
            doi=work.doi,
            verified=False,
            verification_status=VerificationStatus.UNVERIFIED,
            external_ids={"openalex": work.openalex_id, "doi": work.doi},
            external_metadata={"openalex": work.summary()},
            origin="openalex",
        )
    )
    return paper, True


def _citation_edge(
    project_id: str,
    source_paper_id: str,
    target_paper_id: str,
    *,
    external_id: str,
    locator: str,
    source_record_id: str,
    note: str = "",
) -> PaperRelation:
    """构造一条 OpenAlex 引用边。方向：from 引用了 to。"""
    return PaperRelation(
        id=repo.new_id("prel"),
        project_id=project_id,
        from_paper_id=source_paper_id,
        to_paper_id=target_paper_id,
        relation_type=RelationType.CITES,
        source=RelationSource.OPENALEX,
        provider=openalex.PROVIDER,
        external_id=external_id,
        locator=locator,
        source_record_id=source_record_id,
        certainty=Certainty.STATED,   # 引用是事实，不是推断
        verified=True,                # 来源是 OpenAlex 的真实引用记录
        review_status=ReviewStatus.PENDING,  # 是否纳入本研究由人决定
        review_note=note,
    )


def fetch_citation_network(
    paper: Paper,
    client: HttpClient | None = None,
    limit: int = 20,
    include_references: bool = True,
    include_citations: bool = True,
    include_related: bool = False,
) -> CitationReport:
    """拉取一篇文献的引用网络并入库。

    `limit` 是每个方向的条数上限：外部服务的单次返回有限，也避免一次导入几百条。
    `include_related` 打开时只把相关文献导入成候选，**不建任何边**（见模块说明第 3 条）。
    """
    client = client or HttpClient()
    report = CitationReport(paper_id=paper.id)
    project_id = paper.project_id

    doi = normalize_doi(paper.doi)
    openalex_id = paper.external_ids.get("openalex", "")
    if not doi and not openalex_id:
        report.errors.append(
            "这篇文献既没有 DOI 也没有 OpenAlex ID，无法拉取引用关系。"
            "可先做一次元数据核查。"
        )
        return report

    # 1) 取本文的工作记录（缓存命中时几乎不花时间）
    query_value = doi or openalex_id
    try:
        if doi:
            work, response = openalex.fetch_by_doi(doi, client)
        else:
            work, response = openalex.fetch_by_id(openalex_id, client)
    except BibliographyUnavailable as exc:
        report.errors.append(f"OpenAlex：{exc}")
        report.source_record_ids.append(
            _record_source(project_id, paper.id, QueryKind.WORK_LOOKUP, query_value,
                           None, QueryStatus.ERROR, note=str(exc)[:300])
        )
        return report

    if work is None:
        report.errors.append(f"OpenAlex 没有这条记录：{query_value}")
        report.source_record_ids.append(
            _record_source(project_id, paper.id, QueryKind.WORK_LOOKUP, query_value,
                           response, QueryStatus.NOT_FOUND)
        )
        return report

    if work.openalex_id and paper.external_ids.get("openalex") != work.openalex_id:
        paper.external_ids["openalex"] = work.openalex_id
        repo.save(paper)
    report.openalex_id = work.openalex_id
    report.source_record_ids.append(
        _record_source(project_id, paper.id, QueryKind.WORK_LOOKUP, query_value,
                       response, QueryStatus.OK, work.openalex_id, result_count=1)
    )

    # 2) 参考文献：本文引用别人 → from=本文, to=被引文献
    if include_references and work.referenced_works:
        try:
            refs, ref_response = openalex.fetch_by_ids(
                work.referenced_works, client, limit=limit
            )
        except (BibliographyUnavailable, ValueError) as exc:
            report.errors.append(f"参考文献查询失败：{exc}")
            report.source_record_ids.append(
                _record_source(project_id, paper.id, QueryKind.REFERENCES,
                               work.openalex_id, None, QueryStatus.ERROR,
                               note=str(exc)[:300])
            )
        else:
            ref_record_id = _record_source(
                project_id, paper.id, QueryKind.REFERENCES, work.openalex_id,
                ref_response, QueryStatus.OK, result_count=len(refs),
            )
            report.source_record_ids.append(ref_record_id)
            report.references_fetched = len(refs)
            for item in refs:
                candidate, created = import_candidate_paper(project_id, item)
                report.papers_imported += int(created)
                _, is_new = repo.add_paper_relation(
                    _citation_edge(
                        project_id, paper.id, candidate.id,
                        external_id=item.openalex_id,
                        locator="OpenAlex referenced_works",
                        source_record_id=ref_record_id,
                    )
                )
                report.edges_created += int(is_new)
                report.edges_existing += int(not is_new)

    # 3) 施引文献：别人引用本文 → from=施引文献, to=本文
    if include_citations:
        try:
            citing, cite_response = openalex.fetch_cited_by(
                work.openalex_id, client, limit=limit
            )
        except (BibliographyUnavailable, ValueError) as exc:
            report.errors.append(f"施引查询失败：{exc}")
            report.source_record_ids.append(
                _record_source(project_id, paper.id, QueryKind.CITATIONS,
                               work.openalex_id, None, QueryStatus.ERROR,
                               note=str(exc)[:300])
            )
        else:
            cite_record_id = _record_source(
                project_id, paper.id, QueryKind.CITATIONS, work.openalex_id,
                cite_response, QueryStatus.OK, result_count=len(citing),
            )
            report.source_record_ids.append(cite_record_id)
            report.citations_fetched = len(citing)
            for item in citing:
                candidate, created = import_candidate_paper(project_id, item)
                report.papers_imported += int(created)
                _, is_new = repo.add_paper_relation(
                    _citation_edge(
                        project_id, candidate.id, paper.id,
                        external_id=item.openalex_id,
                        locator="OpenAlex cites filter",
                        source_record_id=cite_record_id,
                    )
                )
                report.edges_created += int(is_new)
                report.edges_existing += int(not is_new)

    # 4) 相关文献：只导入候选，不建边——"相关"不是引用事实
    if include_related:
        related_ids = work.related_works[:limit]
        if related_ids:
            try:
                related, related_response = openalex.fetch_by_ids(related_ids, client)
            except (BibliographyUnavailable, ValueError) as exc:
                report.errors.append(f"相关文献查询失败：{exc}")
                report.source_record_ids.append(
                    _record_source(project_id, paper.id, QueryKind.RELATED,
                                   work.openalex_id, None, QueryStatus.ERROR,
                                   note=str(exc)[:300])
                )
            else:
                report.source_record_ids.append(
                    _record_source(project_id, paper.id, QueryKind.RELATED,
                                   work.openalex_id, related_response, QueryStatus.OK,
                                   result_count=len(related))
                )
                report.related_fetched = len(related)
                for item in related:
                    _, created = import_candidate_paper(project_id, item)
                    report.papers_imported += int(created)
                report.warnings.append(
                    f"相关文献 {len(related)} 条已作为候选导入，"
                    "未自动建立引用边（相关≠引用，不能当成引用事实）"
                )

    log.info("拉取引用关系 %s：%s", paper.id, report.describe())
    return report

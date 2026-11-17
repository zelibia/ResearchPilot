"""文献元数据核查：把本地解析结果与外部权威元数据逐字段对比。

对应要求：
- C1-4 DOI / 标题 / 作者 / 年份一致性核查；冲突保留异常状态，不自动覆盖。
- C1-6 所有外部查询记录来源、查询时间、外部 ID 和核实状态。

四条规则：

1. 只对比**两边都有值**的字段。一边为空记为 missing，不算冲突。
2. 冲突时**保留本地值不动**，外部值原样记进核查表，由人来决定改不改。
3. 只有本地为空的字段才允许用外部值补上，记为 filled——补空不算覆盖。
4. 引用计数这类统计差异**不算冲突**：Crossref 与 OpenAlex 口径不同，
   Bensen 2007 一篇是 2041、另一篇是 2492，两者都对。

核查函数只依赖 provider 的规范化记录，不自己发请求，因此可以拿固定样例离线回归。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..storage import repository as repo
from ..storage.models import (
    Paper,
    QueryKind,
    QueryStatus,
    SourceRecord,
    VerificationStatus,
)
from . import crossref, openalex
from .crossref import CrossrefWork
from .net import BibliographyUnavailable, CachedResponse, HttpClient
from .normalize import mentions_surname, normalize_doi, title_similarity
from .openalex import OpenAlexWork

log = logging.getLogger(__name__)

TITLE_MATCH_THRESHOLD = 0.90


@dataclass
class FieldCheck:
    """一个字段在某个来源上的一次对比。"""

    field: str
    local: str
    external: str
    status: str          # match / mismatch / missing_local / missing_external
    source: str          # crossref / openalex
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    paper_id: str
    status: VerificationStatus
    checks: list[FieldCheck] = field(default_factory=list)
    filled_fields: list[str] = field(default_factory=list)
    providers_ok: list[str] = field(default_factory=list)
    providers_failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_record_ids: list[str] = field(default_factory=list)
    checked_at: str = ""

    @property
    def conflicts(self) -> list[FieldCheck]:
        return [c for c in self.checks if c.status == "mismatch"]

    def describe(self) -> str:
        parts = [f"状态 {self.status.value}"]
        if self.providers_ok:
            parts.append(f"来源 {'/'.join(self.providers_ok)}")
        if self.conflicts:
            parts.append(f"冲突 {len(self.conflicts)} 项：" +
                         "、".join(sorted({c.field for c in self.conflicts})))
        if self.filled_fields:
            parts.append(f"补空 {len(self.filled_fields)} 项：" + "、".join(self.filled_fields))
        if self.providers_failed:
            parts.append(f"未取到 {'/'.join(self.providers_failed)}")
        return "；".join(parts)


def _check_field(
    name: str,
    local: str,
    external: str,
    source: str,
    matcher,
    note: str = "",
) -> FieldCheck:
    if not local and not external:
        return FieldCheck(name, local, external, "missing_external", source, note)
    if not local:
        return FieldCheck(name, local, external, "missing_local", source, note)
    if not external:
        return FieldCheck(name, local, external, "missing_external", source, note)
    matched = bool(matcher(local, external))
    return FieldCheck(
        name, local, external, "match" if matched else "mismatch", source, note
    )


def build_checks(
    local: dict[str, str],
    crossref: CrossrefWork | None,
    openalex: OpenAlexWork | None,
) -> list[FieldCheck]:
    """对比本地猜测与各来源的权威值。纯函数，离线可测。"""
    checks: list[FieldCheck] = []

    if crossref is not None:
        checks.append(
            _check_field(
                "doi", local.get("doi", ""), crossref.doi, "crossref",
                lambda a, b: normalize_doi(a) == normalize_doi(b),
                note="DOI 大小写不敏感，比较前已统一规范化",
            )
        )
        checks.append(
            _check_field(
                "title", local.get("title", ""), crossref.title, "crossref",
                lambda a, b: title_similarity(a, b) >= TITLE_MATCH_THRESHOLD,
                note=f"标题相似度阈值 {TITLE_MATCH_THRESHOLD}",
            )
        )
        checks.append(
            _check_field(
                "year", local.get("year", ""), crossref.year, "crossref",
                lambda a, b: str(a).strip() == str(b).strip(),
            )
        )
        checks.append(
            _check_field(
                "authors", local.get("authors", ""), crossref.first_author, "crossref",
                lambda a, b: mentions_surname(a, b),
                note="只比姓氏，容忍 Bensen / Bensen, G. D. / G. D. Bensen 三种写法",
            )
        )

    if openalex is not None:
        checks.append(
            _check_field(
                "doi", local.get("doi", ""), openalex.doi, "openalex",
                lambda a, b: normalize_doi(a) == normalize_doi(b),
            )
        )
        checks.append(
            _check_field(
                "title", local.get("title", ""), openalex.title, "openalex",
                lambda a, b: title_similarity(a, b) >= TITLE_MATCH_THRESHOLD,
            )
        )
        checks.append(
            _check_field(
                "year", local.get("year", ""), openalex.year, "openalex",
                lambda a, b: str(a).strip() == str(b).strip(),
            )
        )
        checks.append(
            _check_field(
                "authors", local.get("authors", ""), openalex.first_author, "openalex",
                lambda a, b: mentions_surname(a, b),
            )
        )
    return checks


def decide_status(
    checks: list[FieldCheck], providers_ok: list[str], providers_failed: list[str]
) -> VerificationStatus:
    """按核查结果定状态。有冲突就是冲突，取不到数据就是未核实。"""
    compared = [c for c in checks if c.status in {"match", "mismatch"}]
    if any(c.status == "mismatch" for c in compared):
        return VerificationStatus.CONFLICT
    if not compared:
        # 一个字段都没比成：要么没有 DOI，要么两个来源都取不到
        return VerificationStatus.UNVERIFIED
    if providers_failed and not providers_ok:
        return VerificationStatus.UNVERIFIED
    matches = [c for c in compared if c.status == "match"]
    fields = {c.field for c in matches}
    if {"doi", "title", "year", "authors"} <= fields:
        return VerificationStatus.VERIFIED
    return VerificationStatus.PARTIAL


def _record(
    project_id: str,
    paper_id: str,
    provider: str,
    kind: QueryKind,
    query_value: str,
    response: CachedResponse | None = None,
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
            provider=provider,
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


def verify_paper(
    paper: Paper,
    client: HttpClient | None = None,
    crossref_work: CrossrefWork | None = None,
    openalex_work: OpenAlexWork | None = None,
    use_network: bool = True,
) -> VerificationReport:
    """核查一篇文献并落库。

    `crossref_work` / `openalex_work` 用于离线测试：传进来就不再发请求。
    """
    client = client or HttpClient()
    checked_at = datetime.now().isoformat(timespec="seconds")
    report = VerificationReport(
        paper_id=paper.id, status=VerificationStatus.UNVERIFIED, checked_at=checked_at
    )

    doi = normalize_doi(paper.doi) or normalize_doi(paper.external_ids.get("doi", ""))
    if not doi:
        report.errors.append("没有 DOI，无法核查。可先在「修正元数据」里补上 DOI。")
        paper.verification_status = VerificationStatus.UNVERIFIED
        paper.metadata_checks = []
        repo.save(paper)
        return report

    if crossref_work is None and use_network:
        try:
            crossref_work, response = crossref.fetch_doi(doi, client)
            if crossref_work is None:
                report.providers_failed.append("crossref")
                report.source_record_ids.append(
                    _record(
                        paper.project_id, paper.id, "crossref", QueryKind.DOI_LOOKUP,
                        doi, response, QueryStatus.NOT_FOUND, note="Crossref 没有这条 DOI",
                    )
                )
            else:
                report.providers_ok.append("crossref")
                report.source_record_ids.append(
                    _record(
                        paper.project_id, paper.id, "crossref", QueryKind.DOI_LOOKUP,
                        doi, response, QueryStatus.OK, crossref_work.doi or doi,
                        result_count=1,
                    )
                )
        except (BibliographyUnavailable, ValueError) as exc:
            report.providers_failed.append("crossref")
            report.errors.append(f"Crossref：{exc}")
            report.source_record_ids.append(
                _record(
                    paper.project_id, paper.id, "crossref", QueryKind.DOI_LOOKUP,
                    doi, None, QueryStatus.ERROR, note=str(exc)[:300],
                )
            )
    elif crossref_work is not None:
        report.providers_ok.append("crossref")

    if openalex_work is None and use_network:
        try:
            openalex_work, response = openalex.fetch_by_doi(doi, client)
            if openalex_work is None:
                report.providers_failed.append("openalex")
                report.source_record_ids.append(
                    _record(
                        paper.project_id, paper.id, "openalex", QueryKind.WORK_LOOKUP,
                        doi, response, QueryStatus.NOT_FOUND, note="OpenAlex 没有这条 DOI",
                    )
                )
            else:
                report.providers_ok.append("openalex")
                report.source_record_ids.append(
                    _record(
                        paper.project_id, paper.id, "openalex", QueryKind.WORK_LOOKUP,
                        doi, response, QueryStatus.OK, openalex_work.openalex_id,
                        result_count=1,
                    )
                )
        except (BibliographyUnavailable, ValueError) as exc:
            report.providers_failed.append("openalex")
            report.errors.append(f"OpenAlex：{exc}")
            report.source_record_ids.append(
                _record(
                    paper.project_id, paper.id, "openalex", QueryKind.WORK_LOOKUP,
                    doi, None, QueryStatus.ERROR, note=str(exc)[:300],
                )
            )
    elif openalex_work is not None:
        report.providers_ok.append("openalex")

    local = {
        "doi": paper.doi,
        "title": paper.title,
        "year": paper.year,
        "authors": paper.authors,
    }
    report.checks = build_checks(local, crossref_work, openalex_work)
    report.status = decide_status(report.checks, report.providers_ok, report.providers_failed)

    # 只补空字段，绝不覆盖已有值
    authoritative = crossref_work or openalex_work
    if authoritative is not None:
        for paper_field in ("title", "authors", "year", "venue", "doi"):
            if getattr(paper, paper_field):
                continue
            value = getattr(authoritative, paper_field, "")
            if paper_field == "authors" and isinstance(value, list):
                value = "; ".join(value)
            if value:
                setattr(paper, paper_field, value)
                report.filled_fields.append(paper_field)

    paper.external_ids = {}
    if crossref_work is not None:
        paper.external_ids["doi"] = crossref_work.doi or doi
        paper.external_ids["crossref"] = crossref_work.doi or doi
    if openalex_work is not None:
        paper.external_ids["openalex"] = openalex_work.openalex_id
        paper.external_ids.setdefault("doi", openalex_work.doi or doi)
    if not paper.external_ids:
        paper.external_ids["doi"] = doi

    paper.external_metadata = {}
    if crossref_work is not None:
        paper.external_metadata["crossref"] = crossref_work.summary()
    if openalex_work is not None:
        paper.external_metadata["openalex"] = openalex_work.summary()

    paper.metadata_checks = [c.as_dict() for c in report.checks]
    paper.verification_status = report.status
    # verified 这个布尔值继续保留给界面用：只有全部对得上才为真
    paper.verified = report.status is VerificationStatus.VERIFIED
    paper.verified_at = checked_at if paper.verified else ""
    repo.save(paper)

    log.info("核查 %s：%s", paper.id, report.describe())
    return report

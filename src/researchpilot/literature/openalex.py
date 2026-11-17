"""OpenAlex API：论文记录、参考文献、施引论文、相关文献。

只调用官方公开接口 `https://api.openalex.org`。
解析函数是纯函数，可用固定样例离线回归。

注意：OpenAlex 的引用数与 Crossref 的 `is-referenced-by-count` 来自不同统计口径，
两者不一致是正常的，不属于"元数据冲突"，不得据此判为异常。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .net import CachedResponse, HttpClient
from .normalize import normalize_doi

log = logging.getLogger(__name__)

PROVIDER = "openalex"
API_BASE = "https://api.openalex.org/works"
SELECT_FIELDS = (
    "id,doi,title,publication_year,authorships,primary_location,"
    "cited_by_count,type,referenced_works,related_works,"
    "abstract_inverted_index,open_access,best_oa_location,locations"
)


@dataclass
class OpenAlexWork:
    """规范化后的 OpenAlex 记录。"""

    openalex_id: str = ""
    doi: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    type: str = ""
    cited_by_count: int = 0
    referenced_works: list[str] = field(default_factory=list)
    related_works: list[str] = field(default_factory=list)
    abstract: str = ""
    is_oa: bool = False
    oa_status: str = ""
    pdf_url: str = ""
    landing_url: str = ""
    license: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    def summary(self) -> dict[str, Any]:
        return {
            "openalex_id": self.openalex_id,
            "doi": self.doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "type": self.type,
            "cited_by_count": self.cited_by_count,
            "referenced_works": self.referenced_works,
            "related_works": self.related_works,
            "abstract": self.abstract,
            "is_oa": self.is_oa,
            "oa_status": self.oa_status,
            "pdf_url": self.pdf_url,
            "landing_url": self.landing_url,
            "license": self.license,
        }


def short_id(value: str | None) -> str:
    """`https://openalex.org/W2139713088` → `W2139713088`。"""
    if not value:
        return ""
    text = str(value).rstrip("/")
    return text.rsplit("/", 1)[-1]


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def abstract_from_inverted_index(value: Any) -> str:
    """Rebuild OpenAlex's word -> positions representation without inventing text."""
    if not isinstance(value, dict) or not value:
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                index = int(position)
            except (TypeError, ValueError):
                continue
            if index >= 0 and str(word):
                positioned.append((index, str(word)))
    if not positioned:
        return ""
    size = max(index for index, _ in positioned) + 1
    words = [""] * size
    for index, word in positioned:
        words[index] = word
    return " ".join(word for word in words if word).strip()


def _oa_links(payload: dict[str, Any]) -> tuple[bool, str, str, str, str]:
    """Return (is_oa, oa_status, pdf_url, landing_url, license)."""
    open_access = payload.get("open_access") or {}
    is_oa = bool(open_access.get("is_oa")) if isinstance(open_access, dict) else False
    oa_status = str(open_access.get("oa_status") or "") if isinstance(open_access, dict) else ""
    best = payload.get("best_oa_location") or {}
    primary = payload.get("primary_location") or {}
    pdf_url = ""
    landing_url = ""
    license_name = ""
    for location in (best, primary, *(payload.get("locations") or [])):
        if not isinstance(location, dict):
            continue
        if location.get("is_oa"):
            is_oa = True
        if not landing_url:
            landing_url = str(location.get("landing_page_url") or "")
        if not pdf_url and location.get("pdf_url"):
            pdf_url = str(location.get("pdf_url"))
        if not license_name:
            license_name = str(location.get("license") or "")
        if pdf_url and landing_url:
            break
    if not pdf_url and isinstance(open_access, dict):
        oa_url = str(open_access.get("oa_url") or "")
        if oa_url.lower().split("?", 1)[0].endswith(".pdf"):
            pdf_url = oa_url
    return is_oa, oa_status, pdf_url, landing_url, license_name


def parse_work(payload: dict[str, Any]) -> OpenAlexWork:
    """把一条 OpenAlex work 对象转成规范化记录。纯函数。"""
    if not isinstance(payload, dict):
        raise ValueError("OpenAlex 记录不是对象")

    authors: list[str] = []
    for item in payload.get("authorships", []) or []:
        name = ((item or {}).get("author") or {}).get("display_name") or ""
        if name:
            authors.append(str(name).strip())

    source = (((payload.get("primary_location") or {}).get("source")) or {})
    is_oa, oa_status, pdf_url, landing_url, license_name = _oa_links(payload)
    return OpenAlexWork(
        openalex_id=short_id(payload.get("id")),
        doi=normalize_doi(payload.get("doi")),
        title=str(payload.get("title") or payload.get("display_name") or ""),
        authors=authors,
        year=str(payload.get("publication_year") or ""),
        venue=str(source.get("display_name") or ""),
        type=str(payload.get("type") or ""),
        cited_by_count=int(payload.get("cited_by_count") or 0),
        referenced_works=[short_id(item) for item in payload.get("referenced_works") or []],
        related_works=[short_id(item) for item in payload.get("related_works") or []],
        abstract=abstract_from_inverted_index(payload.get("abstract_inverted_index")),
        is_oa=is_oa,
        oa_status=oa_status,
        pdf_url=pdf_url,
        landing_url=landing_url,
        license=license_name,
        raw=payload,
    )


def parse_work_list(payload: dict[str, Any]) -> list[OpenAlexWork]:
    """解析 `{"results": [...]}` 形式的列表响应。"""
    results = (payload or {}).get("results")
    if results is None:
        raise ValueError("OpenAlex 列表响应缺少 results")
    return [parse_work(item) for item in results]


def fetch_by_doi(doi: str, client: HttpClient) -> tuple[OpenAlexWork | None, CachedResponse]:
    """按 DOI 取工作记录。返回 (记录或 None, 原始响应)。"""
    normalized = normalize_doi(doi)
    if not normalized:
        raise ValueError(f"不是合法 DOI：{doi!r}")

    response = client.get_json(
        PROVIDER,
        f"{API_BASE}/doi:{normalized}",
        params={"select": SELECT_FIELDS},
    )
    if response.status_code in (404, 410) or not response.payload:
        log.info("OpenAlex 没有这条 DOI：%s", normalized)
        return None, response
    return parse_work(response.payload), response


def fetch_by_id(
    openalex_id: str, client: HttpClient
) -> tuple[OpenAlexWork | None, CachedResponse]:
    """按 OpenAlex ID 取工作记录（用于只有 ID、没有 DOI 的文献）。"""
    wanted = short_id(openalex_id)
    if not wanted:
        raise ValueError(f"不是合法的 OpenAlex ID：{openalex_id!r}")

    response = client.get_json(
        PROVIDER, f"{API_BASE}/{wanted}", params={"select": SELECT_FIELDS}
    )
    if response.status_code in (404, 410) or not response.payload:
        log.info("OpenAlex 没有这条记录：%s", wanted)
        return None, response
    return parse_work(response.payload), response


def fetch_cited_by(
    openalex_id: str, client: HttpClient, limit: int = 25
) -> tuple[list[OpenAlexWork], CachedResponse]:
    """施引论文：引用了我这篇的后续工作。按引用数从高到低。"""
    response = client.get_json(
        PROVIDER,
        API_BASE,
        params={
            "filter": f"cites:{short_id(openalex_id)}",
            "per-page": str(limit),
            "sort": "cited_by_count:desc",
            "select": SELECT_FIELDS,
        },
    )
    if not response.payload:
        return [], response
    return parse_work_list(response.payload), response


def fetch_by_ids(
    ids: list[str], client: HttpClient, limit: int = 25
) -> tuple[list[OpenAlexWork], CachedResponse]:
    """按 OpenAlex ID 批量取记录（用于参考文献与相关文献）。"""
    wanted = [short_id(item) for item in ids if short_id(item)][:limit]
    if not wanted:
        raise ValueError("没有可查询的 OpenAlex ID")
    response = client.get_json(
        PROVIDER,
        API_BASE,
        params={
            "filter": "openalex_id:" + "|".join(wanted),
            "per-page": str(len(wanted)),
            "select": SELECT_FIELDS,
        },
    )
    if not response.payload:
        return [], response
    return parse_work_list(response.payload), response

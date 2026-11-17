"""Crossref REST API：DOI → 权威元数据。

只调用官方公开接口 `https://api.crossref.org/works/{doi}`。
解析函数是纯函数（输入 API 原始响应，输出规范化记录），因此可以拿固定样例离线回归。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .net import CachedResponse, HttpClient
from .normalize import normalize_doi

log = logging.getLogger(__name__)

PROVIDER = "crossref"
API_BASE = "https://api.crossref.org/works/"


@dataclass
class CrossrefWork:
    """规范化后的 Crossref 记录。取不到的字段留空，不填默认值。"""

    doi: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    type: str = ""
    url: str = ""
    publisher: str = ""
    reference_count: int = 0
    cited_by_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    def summary(self) -> dict[str, Any]:
        """写进 Paper.external_metadata 的部分（不含 raw，避免把整包响应塞进数据库）。"""
        return {
            "doi": self.doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "type": self.type,
            "url": self.url,
            "publisher": self.publisher,
            "reference_count": self.reference_count,
            "cited_by_count": self.cited_by_count,
        }


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _year_from_date_parts(value: Any) -> str:
    parts = (value or {}).get("date-parts") if isinstance(value, dict) else None
    if not parts or not parts[0]:
        return ""
    return str(parts[0][0])


def parse_work(message: dict[str, Any]) -> CrossrefWork:
    """把一个 Crossref `message` 对象转成规范化记录。纯函数，便于离线测试。"""
    if not isinstance(message, dict):
        raise ValueError("Crossref 响应不是对象")

    authors: list[str] = []
    for item in message.get("author", []) or []:
        family = (item.get("family") or "").strip()
        given = (item.get("given") or "").strip()
        name = (item.get("name") or "").strip()  # 机构作者
        if family:
            authors.append(f"{family}, {given}".strip().rstrip(","))
        elif name:
            authors.append(name)

    return CrossrefWork(
        doi=normalize_doi(message.get("DOI")),
        title=_first(message.get("title")),
        authors=authors,
        year=_year_from_date_parts(message.get("issued"))
        or _year_from_date_parts(message.get("published-print"))
        or _year_from_date_parts(message.get("created")),
        venue=_first(message.get("container-title")),
        type=str(message.get("type") or ""),
        url=str(message.get("URL") or ""),
        publisher=str(message.get("publisher") or ""),
        reference_count=len(message.get("reference") or []),
        cited_by_count=int(message.get("is-referenced-by-count") or 0),
        raw=message,
    )


def fetch_doi(doi: str, client: HttpClient) -> tuple[CrossrefWork | None, CachedResponse]:
    """按 DOI 取元数据。返回 (记录或 None, 原始响应)。404 时记录为 None。"""
    normalized = normalize_doi(doi)
    if not normalized:
        raise ValueError(f"不是合法 DOI：{doi!r}")

    response = client.get_json(PROVIDER, API_BASE + normalized)
    if response.status_code in (404, 410) or not response.payload:
        log.info("Crossref 没有这条 DOI：%s", normalized)
        return None, response

    message = response.payload.get("message") if isinstance(response.payload, dict) else None
    if not isinstance(message, dict):
        raise ValueError("Crossref 响应缺少 message 字段")
    return parse_work(message), response

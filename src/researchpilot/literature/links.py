"""Safe external links for papers that do not have a local PDF."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from .normalize import normalize_doi


def safe_http_url(value: Any) -> str:
    """Return a usable HTTP(S) URL, or an empty string."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def doi_url(value: Any) -> str:
    doi = normalize_doi(str(value or ""))
    return f"https://doi.org/{doi}" if doi else ""


def openalex_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return safe_http_url(text)
    short = text.rsplit("/", 1)[-1]
    return f"https://openalex.org/{short}" if short else ""


def _nested_urls(value: Any) -> Iterable[str]:
    """Yield URL-looking strings from metadata without trusting arbitrary values."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "url", "doi", "openalex", "openalex_id", "landing_page_url",
                "pdf_url", "oa_url", "source_url", "source_urls",
            }:
                yield from _nested_urls(item)
            elif isinstance(item, (Mapping, list, tuple)):
                yield from _nested_urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _nested_urls(item)
    elif isinstance(value, str):
        yield value


def item_external_url(item: Mapping[str, Any]) -> str:
    """Best external landing link for an AI-recommended or imported work."""
    url = doi_url(item.get("doi"))
    if url:
        return url

    external_ids = item.get("external_ids") or {}
    if isinstance(external_ids, Mapping):
        url = openalex_url(external_ids.get("openalex"))
        if url:
            return url
        url = safe_http_url(external_ids.get("url"))
        if url:
            return url

    for candidate in _nested_urls(item.get("external_metadata") or {}):
        url = safe_http_url(candidate)
        if url:
            return url
    for candidate in item.get("source_urls") or []:
        url = safe_http_url(candidate)
        if url:
            return url
    return ""


def paper_external_url(paper: Any) -> str:
    """Best external landing link for a local/imported Paper record."""
    url = doi_url(getattr(paper, "doi", ""))
    if url:
        return url

    external_ids = getattr(paper, "external_ids", {}) or {}
    if isinstance(external_ids, Mapping):
        url = openalex_url(external_ids.get("openalex"))
        if url:
            return url
        url = safe_http_url(external_ids.get("url"))
        if url:
            return url

    for candidate in getattr(paper, "source_urls", []) or []:
        url = safe_http_url(candidate)
        if url:
            return url
    for candidate in _nested_urls(getattr(paper, "external_metadata", {}) or {}):
        url = safe_http_url(candidate)
        if url:
            return url
    return ""

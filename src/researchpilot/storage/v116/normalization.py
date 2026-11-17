"""Canonical normalization and duplicate grouping for v1.16 documents."""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

_DOI_PREFIX_RE = re.compile(r"^\s*(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2}|21\d{2})\b")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    doi = _DOI_PREFIX_RE.sub("", str(value).strip()).strip().rstrip(".,;)").lower()
    return doi if doi.startswith("10.") else ""


def normalize_title(value: str | None) -> str:
    """NFKC/NFKD, accent removal, punctuation folding, and whitespace collapse."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    text = text.casefold()
    text = re.sub(r"[\u2010-\u2015\u2212]", " ", text)
    text = re.sub(r"['\u2018\u2019\u02bc`]", "", text)
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def title_quality(value: str | None) -> bool:
    normalized = normalize_title(value)
    if len(_CJK_RE.findall(normalized)) >= 8:
        return True
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return len(tokens) >= 4 and len(normalized) >= 20


def title_key(value: str | None) -> str:
    normalized = normalize_title(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def extract_year(value: str | None) -> str:
    if value is None:
        return ""
    match = _YEAR_RE.search(str(value))
    return match.group(0) if match else ""


def _author_family(author: Any) -> str:
    if hasattr(author, "model_dump"):
        author = author.model_dump()
    if isinstance(author, dict):
        family = str(author.get("family") or "").strip()
        if family:
            return normalize_title(family)
        literal = str(author.get("literal") or author.get("given") or "").strip()
        return normalize_title(
            literal.split()[-1] if literal.split() else literal
        )
    text = str(author or "").strip()
    if not text:
        return ""
    if "," in text:
        text = text.split(",", 1)[0]
    return normalize_title(text.split()[-1] if text.split() else text)


def first_author_family(authors: list[Any] | str | None) -> str:
    if not authors:
        return ""
    if isinstance(authors, str):
        first = re.split(r"\s*[;；]\s*|\s+and\s+", authors, maxsplit=1)[0]
        return _author_family(first)
    return _author_family(authors[0])


def first_author_compatible(left: list[Any] | str | None, right: list[Any] | str | None) -> bool:
    a, b = first_author_family(left), first_author_family(right)
    return not a or not b or a == b


def year_compatible(left: str | None, right: str | None) -> bool:
    a, b = extract_year(left), extract_year(right)
    return not a or not b or a == b


def title_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def document_identity_key(
    *,
    doi: str | None,
    title: str | None,
    sha256: str | None = None,
    fallback_id: str | None = None,
) -> str:
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"doi:{normalized_doi}"
    normalized_title = title_key(title)
    if normalized_title and title_quality(title):
        return f"title:{normalized_title}"
    if sha256:
        return f"sha256:{sha256.lower()}"
    if fallback_id:
        return f"legacy:{fallback_id}"
    raise ValueError("document identity needs DOI, usable title, sha256, or fallback id")


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class DocumentCandidate:
    legacy_id: str
    project_id: str
    title: str = ""
    doi: str = ""
    authors: list[Any] | str = field(default_factory=list)
    year: str = ""
    sha256: str = ""

    @property
    def normalized_doi(self) -> str:
        return normalize_doi(self.doi)

    @property
    def normalized_title_key(self) -> str:
        return title_key(self.title)


@dataclass
class DocumentGroup:
    candidates: list[DocumentCandidate]
    basis: str

    @property
    def identity_key(self) -> str:
        doi = next((item.normalized_doi for item in self.candidates if item.normalized_doi), "")
        if doi:
            return f"doi:{doi}"
        for item in self.candidates:
            if title_quality(item.title):
                return f"title:{item.normalized_title_key}"
        for item in self.candidates:
            if item.sha256:
                return f"sha256:{item.sha256.lower()}"
        return f"legacy:{self.candidates[0].legacy_id}"


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            nxt = self.parent[value]
            self.parent[value] = root
            value = nxt
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def group_document_candidates(
    candidates: Iterable[DocumentCandidate],
) -> tuple[list[DocumentGroup], list[dict[str, Any]]]:
    """Group exact DOI/title matches without fuzzy auto-merge.

    Returns groups and conflict descriptions.
    """
    rows = list(candidates)
    if not rows:
        return [], []
    uf = _UnionFind(item.legacy_id for item in rows)
    by_id = {item.legacy_id: item for item in rows}
    conflicts: list[dict[str, Any]] = []

    by_doi: dict[str, list[DocumentCandidate]] = {}
    by_title: dict[str, list[DocumentCandidate]] = {}
    for item in rows:
        if item.normalized_doi:
            by_doi.setdefault(item.normalized_doi, []).append(item)
        if title_quality(item.title):
            by_title.setdefault(item.normalized_title_key, []).append(item)

    for doi, items in by_doi.items():
        usable_titles = {
            item.normalized_title_key
            for item in items
            if title_quality(item.title)
        }
        if len(usable_titles) > 1:
            conflicts.append(
                {
                    "kind": "metadata_conflict",
                    "legacy_ids": [item.legacy_id for item in items],
                    "doi_key": doi,
                    "title_keys": sorted(usable_titles),
                }
            )
        for item in items[1:]:
            uf.union(items[0].legacy_id, item.legacy_id)

    for key, items in by_title.items():
        dois = {item.normalized_doi for item in items if item.normalized_doi}
        if len(dois) > 1:
            conflicts.append(
                {
                    "kind": "doi_title_mismatch",
                    "legacy_ids": [item.legacy_id for item in items],
                    "doi_keys": sorted(dois),
                }
            )
            continue
        for left_index, left in enumerate(items):
            for right in items[left_index + 1:]:
                if year_compatible(left.year, right.year) and first_author_compatible(
                    left.authors, right.authors
                ):
                    uf.union(left.legacy_id, right.legacy_id)
                else:
                    conflicts.append(
                        {
                            "kind": "ambiguous_title_match",
                            "legacy_ids": [left.legacy_id, right.legacy_id],
                            "title_key": key,
                        }
                    )

    groups_map: dict[str, list[DocumentCandidate]] = {}
    for item in rows:
        groups_map.setdefault(uf.find(item.legacy_id), []).append(item)

    groups: list[DocumentGroup] = []
    for items in groups_map.values():
        basis = "legacy_id"
        if any(item.normalized_doi for item in items):
            basis = "doi"
        elif len(items) > 1 or title_quality(items[0].title):
            basis = "title"
        elif any(item.sha256 for item in items):
            basis = "sha256"
        groups.append(DocumentGroup(candidates=sorted(items, key=lambda x: x.legacy_id), basis=basis))
    groups.sort(key=lambda group: group.identity_key)
    return groups, conflicts

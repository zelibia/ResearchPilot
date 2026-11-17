"""Adapters for optional pre-v1.16 annotation payloads.

The current application has no durable annotation table. These adapters only
read data that is actually present; temporary PDF search highlights are never
treated as user annotations.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LegacyAnnotationRow:
    source_table: str
    legacy_id: str
    paper_id: str
    type: str
    quote: str = ""
    comment: str = ""
    color: str = ""
    page_index: int | None = None
    page_label: str = ""
    rects: list[Any] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    author_id: str = ""
    source: str = "legacy"
    read_only: bool = False
    external_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    page_basis: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalize_row(
    raw: dict[str, Any],
    *,
    source_table: str,
    fallback_id: str,
    fallback_paper_id: str = "",
) -> LegacyAnnotationRow:
    paper_id = str(
        raw.get("paper_id")
        or raw.get("legacy_paper_id")
        or raw.get("document_id")
        or fallback_paper_id
        or ""
    )
    page_index: int | None = None
    page_basis = str(raw.get("page_basis") or "")
    if raw.get("page_index") is not None:
        page_index = int(raw["page_index"])
        page_basis = page_basis or "zero_based_field"
    elif raw.get("page") is not None:
        page_number = int(raw["page"])
        page_index = page_number - 1 if page_number > 0 else 0
        page_basis = page_basis or "one_based_legacy_page"
    annotation_type = str(
        raw.get("type") or raw.get("annotation_type") or "highlight"
    ).lower()
    source = str(raw.get("source") or "legacy")
    external = source in {"external_pdf", "pdf_embedded"}
    if external:
        source = "external_pdf"
    return LegacyAnnotationRow(
        source_table=source_table,
        legacy_id=str(raw.get("id") or fallback_id),
        paper_id=paper_id,
        type=annotation_type,
        quote=str(
            raw.get("quote")
            or raw.get("text")
            or raw.get("selected_text")
            or ""
        ),
        comment=str(
            raw.get("comment_markdown")
            or raw.get("comment")
            or raw.get("note")
            or ""
        ),
        color=str(raw.get("color") or ""),
        page_index=page_index,
        page_label=str(raw.get("page_label") or ""),
        rects=_as_list(raw.get("rects") or raw.get("position")),
        tags=[str(item) for item in _as_list(raw.get("tags"))],
        author_id=str(raw.get("author_id") or raw.get("creator") or ""),
        source=source,
        read_only=bool(raw.get("read_only")) or external,
        external_id=str(raw.get("external_id") or ""),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        page_basis=page_basis,
        payload=raw,
    )


def discover_legacy_annotations(
    db_path: str | Path,
    papers: list[dict[str, Any]],
    projects: list[dict[str, Any]],
) -> list[LegacyAnnotationRow]:
    rows: list[LegacyAnnotationRow] = []
    seen: set[tuple[str, str]] = set()

    def append(row: LegacyAnnotationRow) -> None:
        key = (row.source_table, row.legacy_id)
        if key not in seen:
            seen.add(key)
            rows.append(row)

    for paper in papers:
        paper_id = str(paper.get("id") or "")
        summary = paper.get("summary")
        candidates: list[Any] = []
        if isinstance(summary, dict):
            candidates.extend(_as_list(summary.get("annotations")))
            reading_guide = summary.get("reading_guide")
            if isinstance(reading_guide, dict):
                candidates.extend(_as_list(reading_guide.get("annotations")))
        candidates.extend(_as_list(paper.get("annotations")))
        for index, raw in enumerate(candidates):
            if not isinstance(raw, dict):
                continue
            append(
                _normalize_row(
                    raw,
                    source_table="paper.summary.annotations",
                    fallback_id=f"{paper_id}:annotation:{index}",
                    fallback_paper_id=paper_id,
                )
            )

    for project in projects:
        for key in ("annotations", "paper_annotations"):
            for index, raw in enumerate(_as_list(project.get(key))):
                if not isinstance(raw, dict):
                    continue
                append(
                    _normalize_row(
                        raw,
                        source_table=f"projects.payload.{key}",
                        fallback_id=f"{project.get('id', '')}:{key}:{index}",
                    )
                )

    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_annotations'"
        ).fetchone()
        if table:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(pdf_annotations)").fetchall()
            }
            for index, raw_row in enumerate(
                conn.execute("SELECT * FROM pdf_annotations").fetchall()
            ):
                raw = dict(raw_row)
                payload = raw.get("payload")
                if payload:
                    try:
                        decoded = json.loads(payload)
                        if isinstance(decoded, dict):
                            raw = {**decoded, **raw}
                    except (TypeError, json.JSONDecodeError):
                        pass
                if "id" not in raw:
                    raw["id"] = f"pdf_annotation:{index}"
                if "project_id" in columns and "paper_id" not in raw:
                    raw["paper_id"] = ""
                append(
                    _normalize_row(
                        raw,
                        source_table="pdf_annotations",
                        fallback_id=f"pdf_annotation:{index}",
                    )
                )
    finally:
        conn.close()
    return rows

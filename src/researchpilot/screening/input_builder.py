"""Build a bounded, traceable screening input from quarantine content."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..literature.papers import parse_pdf
from .contracts import (
    CandidateMetadata,
    FulltextSection,
    ProjectContext,
    ScreeningCandidateInput,
)
from .extraction import OCRPort, TesseractOCR
from .orm import CandidateFile, ImportCandidate
from .storage import QuarantineStore

_HEADING_RE = re.compile(
    r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s*)?"
    r"(abstract|introduction|background|related work|data|methods?|methodology|"
    r"results?|discussion|conclusions?|references)\s*$"
)


@dataclass
class BuiltScreeningInput:
    input: ScreeningCandidateInput
    input_scope: dict[str, Any]
    input_preview_markdown: str
    input_hash: str
    page_texts: dict[int, str]


def project_context(session: Session, project_id: str) -> ProjectContext:
    row = session.execute(
        sa.text("SELECT payload FROM projects WHERE id = :id"),
        {"id": project_id},
    ).first()
    payload: dict[str, Any] = {}
    if row and row[0]:
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            payload = {}

    active_goal = ""
    success: list[str] = []
    existing_tables = set(sa.inspect(session.bind).get_table_names())
    goal_rows = (
        session.execute(
            sa.text(
                "SELECT payload FROM goals WHERE project_id = :project_id "
                "ORDER BY created_at DESC LIMIT 5"
            ),
            {"project_id": project_id},
        ).all()
        if "goals" in existing_tables
        else []
    )
    for goal_row in goal_rows:
        try:
            goal = json.loads(goal_row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        active_goal = active_goal or str(goal.get("statement") or "")
        if goal.get("acceptance"):
            success.append(str(goal["acceptance"]))

    methods: list[str] = []
    method_rows = (
        session.execute(
            sa.text("SELECT payload FROM methods WHERE project_id = :id LIMIT 30"),
            {"id": project_id},
        ).all()
        if "methods" in existing_tables
        else []
    )
    for method_row in method_rows:
        try:
            method = json.loads(method_row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if method.get("name"):
            methods.append(str(method["name"]))

    titles = [
        str(item)
        for item in session.execute(
            sa.text(
                "SELECT d.title FROM project_documents p "
                "JOIN library_documents d ON d.id = p.document_id "
                "WHERE p.project_id = :id AND p.deleted_at IS NULL "
                "ORDER BY p.updated_at DESC LIMIT 80"
            ),
            {"id": project_id},
        ).scalars()
    ]
    return ProjectContext(
        project_id=project_id,
        title=str(payload.get("title") or ""),
        domain=str(payload.get("domain") or ""),
        description=str(payload.get("description") or ""),
        active_goal=active_goal,
        success_criteria=success,
        target_method_names=methods,
        existing_document_titles=titles,
    )


def _ocr_text_pages(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    pages = text.split("\f")
    return pages if pages else [text]


def load_candidate_pages(
    candidate: ImportCandidate,
    files: list[CandidateFile],
    store: QuarantineStore,
    *,
    ocr: OCRPort | None = None,
) -> list[str]:
    ocr_file = next((item for item in files if item.role == "ocr_text"), None)
    if ocr_file:
        return _ocr_text_pages(store.path_for(ocr_file.storage_key))
    source = next((item for item in files if item.role == "source_file"), None)
    if source is None:
        return []
    path = store.path_for(source.storage_key)
    if candidate.source_kind == "pdf":
        return parse_pdf(path, max_pages=60).pages
    if candidate.source_kind == "image":
        engine = ocr or TesseractOCR()
        if not engine.available:
            return []
        return [engine.extract_image(path)]
    return []


def _section_rows(pages: list[str]) -> list[FulltextSection]:
    if not pages:
        return []
    starts: list[tuple[int, str]] = [(0, "Front matter")]
    for index, page in enumerate(pages):
        match = _HEADING_RE.search((page or "")[:1200])
        if match:
            title = match.group(1).strip().title()
            if index == 0 and title == "Front matter":
                continue
            starts.append((index, title))
    starts = sorted(dict(starts).items())
    rows: list[FulltextSection] = []
    for position, (start, title) in enumerate(starts):
        end = (starts[position + 1][0] - 1) if position + 1 < len(starts) else len(pages) - 1
        text = "\n".join(pages[start : end + 1]).strip()
        if text:
            rows.append(FulltextSection(title=title, page_start=start, page_end=end, text=text))
    return rows


def _select_sections(rows: list[FulltextSection], limit: int = 18000) -> tuple[list[FulltextSection], bool]:
    if not rows:
        return [], False
    priority = {
        "abstract": 0,
        "introduction": 1,
        "background": 1,
        "methods": 2,
        "method": 2,
        "methodology": 2,
        "data": 2,
        "results": 3,
        "discussion": 4,
        "conclusions": 5,
        "conclusion": 5,
    }
    ordered = sorted(
        rows,
        key=lambda item: (priority.get(item.title.lower(), 8), item.page_start),
    )
    selected: list[FulltextSection] = []
    used = 0
    truncated = False
    for row in ordered:
        room = limit - used
        if room <= 0:
            truncated = True
            break
        text = row.text
        if len(text) > room:
            text = text[:room]
            truncated = True
        if text:
            selected.append(row.model_copy(update={"text": text}))
            used += len(text)
    return sorted(selected, key=lambda item: item.page_start), truncated


def build_screening_input(
    *,
    candidate: ImportCandidate,
    files: list[CandidateFile],
    store: QuarantineStore,
    project: ProjectContext,
    ocr: OCRPort | None = None,
) -> BuiltScreeningInput:
    metadata = CandidateMetadata.model_validate(candidate.metadata_json or {})
    pages = load_candidate_pages(candidate, files, store, ocr=ocr)
    sections = _section_rows(pages)
    fulltext, truncated = _select_sections(sections)
    chars = sum(len(re.sub(r"\s+", "", item.text)) for item in fulltext)
    if chars >= 500:
        coverage = "fulltext"
    elif metadata.abstract.strip():
        coverage = "abstract"
        fulltext = []
    else:
        coverage = "metadata_only"
        fulltext = []

    candidate_input = ScreeningCandidateInput(
        candidate_id=candidate.id,
        project_context=project,
        metadata=metadata,
        abstract=metadata.abstract,
        fulltext_sections=fulltext,
        input_coverage=coverage,
    )
    selected_pages = sorted(
        {page for section in fulltext for page in range(section.page_start, section.page_end + 1)}
    )
    scope = {
        "usedFields": [
            "project.title",
            "project.domain",
            "project.description",
            "candidate.title",
            "candidate.authors",
            "candidate.abstract",
            "candidate.fulltext",
        ],
        "selectedPages": selected_pages,
        "fulltextChars": chars,
        "truncated": truncated,
        "inputCoverage": coverage,
    }
    preview = _preview(candidate_input, scope)
    canonical = json.dumps(
        candidate_input.model_dump(by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BuiltScreeningInput(
        input=candidate_input,
        input_scope=scope,
        input_preview_markdown=preview,
        input_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        page_texts={index: text for index, text in enumerate(pages)},
    )


def _preview(candidate: ScreeningCandidateInput, scope: dict[str, Any]) -> str:
    project = candidate.project_context
    metadata = candidate.metadata
    lines = [
        "# 项目",
        f"- 标题：{project.title}",
        f"- 领域：{project.domain}",
        f"- 当前目标：{project.active_goal}",
        "",
        "# 候选元数据",
        f"- 标题：{metadata.title}",
        f"- 作者：{'; '.join(metadata.authors)}",
        f"- 年份：{metadata.year}",
        f"- 期刊：{metadata.venue}",
        f"- DOI：{metadata.doi}",
        f"- 摘要：{candidate.abstract[:5000]}",
        "",
        f"# 正文节选（{scope['inputCoverage']}，{scope['fulltextChars']} 字符）",
    ]
    for section in candidate.fulltext_sections:
        lines.extend(
            [
                f"## [{section.page_start + 1}-{section.page_end + 1}] {section.title}",
                section.text,
                "",
            ]
        )
    return "\n".join(lines)[:24000]

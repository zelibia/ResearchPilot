"""P0-01 legacy-to-v1.16 migration engine.

The migration is side-by-side: legacy ``projects``/``papers``/payload tables are
never updated or deleted. New tables are deterministic and idempotent, so a
failed migration can be retried without duplicating canonical documents.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...config import PATHS
from .contracts import (
    AnnotationCreateInput,
    DocumentCreateInput,
    FileAssetCreateInput,
    MetadataValueInput,
    OutlineEvidenceInput,
    OutlineNodeInput,
    OutlineVersionCreateInput,
    ProjectDocumentCreateInput,
    SummarySectionEvidenceInput,
    SummarySectionInput,
    SummaryVersionCreateInput,
)
from .db import (
    SCHEMA_VERSION,
    current_schema_versions,
    deploy_schema,
    engine_for,
    get_model_version,
    legacy_payload_digest,
    session_factory,
    set_model_version,
)
from .normalization import (
    DocumentCandidate,
    document_identity_key,
    group_document_candidates,
    normalize_doi,
    normalize_title,
    stable_id,
)
from .legacy_annotations import discover_legacy_annotations
from .orm import (
    Annotation,
    AnnotationTag,
    DocumentRelation,
    FileAsset,
    LibraryDocument,
    MigrationConflict,
    MigrationIdMap,
    MigrationRun,
    MigrationUnmappedRecord,
    OutlineNode,
    OutlineNodeEvidence,
    OutlineVersion,
    ProjectDocument,
    SummarySection,
    SummarySectionEvidence,
    SummaryVersion,
    SummaryVersionDocument,
    Tag,
    now_iso,
)

LEGACY_TABLES = (
    "projects",
    "papers",
    "paper_claims",
    "paper_relations",
    "source_records",
    "pdf_annotations",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _backup_sqlite(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}_{time.time_ns()}"
    target = backup_dir / f"researchpilot_pre_v116_{stamp}.db"
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA foreign_keys=ON")
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    finally:
        dst.close()
        src.close()
    return target


def _load_payloads(db_path: Path, table: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "payload" not in columns:
            return []
        project_expr = (
            "project_id AS project_id" if "project_id" in columns else "NULL AS project_id"
        )
        created_expr = (
            "created_at AS created_at" if "created_at" in columns else "NULL AS created_at"
        )
        rows = conn.execute(
            f"SELECT id, {project_expr}, {created_expr}, payload FROM {table}"
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload.setdefault("id", row["id"])
            if row["project_id"] and not payload.get("project_id"):
                payload["project_id"] = row["project_id"]
            if row["created_at"] and not payload.get("created_at"):
                payload["created_at"] = row["created_at"]
            result.append(payload)
        return result
    finally:
        conn.close()


def _candidate(paper: dict[str, Any]) -> DocumentCandidate:
    return DocumentCandidate(
        legacy_id=str(paper["id"]),
        project_id=str(paper.get("project_id") or ""),
        title=str(paper.get("title") or paper.get("title_raw") or ""),
        doi=str(paper.get("doi") or (paper.get("external_ids") or {}).get("doi") or ""),
        authors=paper.get("authors") or "",
        year=str(paper.get("year") or ""),
        sha256=str(paper.get("sha256") or ""),
    )


def _is_target_annotation_table(db_path: Path) -> bool | None:
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='annotations'"
        ).fetchone()
        if not exists:
            return None
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(annotations)").fetchall()
        }
        required = {
            "document_id",
            "file_id",
            "rects_json",
            "selector_json",
            "legacy_payload_json",
            "revision",
        }
        return required.issubset(columns)
    finally:
        conn.close()


def _source_rank(paper: dict[str, Any]) -> int:
    title_source = str(paper.get("title_source") or "")
    if paper.get("title_confirmed_at") or title_source == "manual":
        return 100
    if paper.get("verified") or paper.get("verification_status") == "verified":
        return 90
    return {
        "embedded": 80,
        "first_page": 70,
        "filename": 60,
        "body": 50,
        "external:crossref": 85,
        "external:openalex": 85,
    }.get(title_source, 40)


def _project_origin(paper: dict[str, Any]) -> str:
    origin = str(paper.get("origin") or "legacy")
    return {
        "upload": "upload",
        "screenshot": "screenshot",
        "doi": "doi",
        "url": "url",
        "library": "library",
        "openalex": "citation_candidate",
        "citation_candidate": "citation_candidate",
        "legacy": "legacy",
    }.get(origin, "legacy")


def _choose_metadata(papers: list[dict[str, Any]]) -> tuple[dict[str, Any], list[MetadataValueInput]]:
    ordered = sorted(
        papers,
        key=lambda item: (
            -_source_rank(item),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
    )
    winner = ordered[0]
    values: list[MetadataValueInput] = []
    seen: set[tuple[str, str, str]] = set()
    for paper in ordered:
        raw_source = str(paper.get("title_source") or "legacy")
        source = {
            "embedded": "pdf_embedded",
            "first_page": "first_page",
            "filename": "filename",
            "body": "body",
            "external:crossref": "crossref",
            "crossref": "crossref",
            "external:openalex": "openalex",
            "openalex": "openalex",
            "ocr": "ocr",
            "manual": "manual",
            "legacy": "legacy",
        }.get(raw_source, "legacy")
        for field, value in (
            ("title", paper.get("title")),
            ("title_raw", paper.get("title_raw")),
            ("authors", paper.get("authors")),
            ("year", paper.get("year")),
            ("venue", paper.get("venue")),
            ("doi", paper.get("doi")),
        ):
            if value is None or str(value).strip() == "":
                continue
            key = (field, str(value), source)
            if key in seen:
                continue
            seen.add(key)
            values.append(
                MetadataValueInput(
                    field_name=field,
                    value=value,
                    source=source if source else "legacy",
                    is_current=paper is winner,
                )
            )
    return winner, values


def _guide_nodes(guide: dict[str, Any], document_id: str, paper_id: str) -> tuple[list[OutlineNodeInput], str]:
    dimensions = guide.get("dimensions")
    nodes: list[OutlineNodeInput] = []
    markdown_lines: list[str] = []
    if isinstance(dimensions, dict) and dimensions:
        for index, (key, dim) in enumerate(dimensions.items()):
            if key == "structure":
                continue
            if not isinstance(dim, dict):
                continue
            title = str(dim.get("title") or key)
            body_parts = [str(dim.get("summary") or "")]
            body_parts.extend(str(item) for item in dim.get("points") or [] if str(item).strip())
            body = "\n\n".join(item for item in body_parts if item)
            nodes.append(
                OutlineNodeInput(
                    id=stable_id("outn", document_id, paper_id, str(index), title),
                    order_index=index,
                    heading_level=1,
                    title=title,
                    body_markdown=body,
                    origin="ai",
                    legacy_source={"key": key},
                )
            )
            markdown_lines.extend([f"# {title}", "", body, ""])
            for evidence in dim.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                page = evidence.get("page")
                annotation_id = str(evidence.get("annotation_id") or "")
                nodes[-1].evidence.append(
                    OutlineEvidenceInput(
                        kind=(
                            "annotation"
                            if annotation_id
                            else "page"
                            if page
                            else "document"
                        ),
                        document_id=document_id,
                        annotation_id=annotation_id or None,
                        page_index=(int(page) - 1) if page and int(page) > 0 else None,
                        quote=str(evidence.get("quote") or ""),
                        match_status=(
                            "exact"
                            if evidence.get("verification") == "exact"
                            else "partial"
                            if page or annotation_id
                            else "none"
                        ),
                    )
                )
    else:
        for index, section in enumerate(guide.get("sections") or []):
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or f"章节 {index + 1}")
            body_parts = [str(section.get("summary") or "")]
            body_parts.extend(
                str((item or {}).get("text") or "")
                for item in section.get("points") or []
                if isinstance(item, dict)
            )
            body = "\n\n".join(part for part in body_parts if part)
            nodes.append(
                OutlineNodeInput(
                    id=stable_id("outn", document_id, paper_id, str(index), title),
                    order_index=index,
                    heading_level=min(max(int(section.get("level") or 1), 1), 4),
                    title=title,
                    body_markdown=body,
                    origin="ai",
                    legacy_source=section,
                )
            )
            markdown_lines.extend([f"# {title}", "", body, ""])
    return nodes, "\n".join(markdown_lines).strip()


def _structure_nodes(
    guide: dict[str, Any], document_id: str, paper_id: str
) -> tuple[list[OutlineNodeInput], str]:
    dimensions = guide.get("dimensions")
    structure = dimensions.get("structure") if isinstance(dimensions, dict) else None
    if isinstance(structure, dict):
        items = structure.get("items") or structure.get("children") or []
    else:
        items = guide.get("structure") or []
    nodes: list[OutlineNodeInput] = []
    counters: dict[str | None, int] = {}
    markdown_lines: list[str] = []

    def walk(rows: list[Any], parent_id: str | None, depth: int, path: str) -> None:
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            title = str(
                item.get("title")
                or item.get("heading")
                or item.get("name")
                or f"章节 {path}.{index + 1}"
            )
            order_index = counters.get(parent_id, 0)
            counters[parent_id] = order_index + 1
            level = min(max(int(item.get("level") or depth), 1), 4)
            node_id = stable_id("outn", document_id, paper_id, path, str(index), title)
            node = OutlineNodeInput(
                id=node_id,
                parent_id=parent_id,
                order_index=order_index,
                heading_level=level,
                title=title,
                body_markdown=str(item.get("summary") or item.get("body") or ""),
                origin="ai",
                legacy_source=item,
            )
            page_start = item.get("page_start") or item.get("page")
            page_end = item.get("page_end")
            if page_start or page_end:
                start_index = (
                    int(page_start) - 1 if page_start and int(page_start) > 0 else None
                )
                node.evidence.append(
                    OutlineEvidenceInput(
                        kind="page" if start_index is not None else "document",
                        document_id=document_id,
                        page_index=start_index,
                        quote="",
                        match_status="partial",
                    )
                )
            nodes.append(node)
            markdown_lines.extend(
                [f"{'#' * level} {title}", "", node.body_markdown, ""]
            )
            children = item.get("items") or item.get("children") or []
            if isinstance(children, list):
                walk(children, node_id, level + 1, f"{path}.{index + 1}")

    if isinstance(items, list):
        walk(items, None, 1, "root")
    return nodes, "\n".join(markdown_lines).strip()


@dataclass
class MigrationEngine:
    db_path: str | Path
    backup_dir: str | Path

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.backup_dir = Path(self.backup_dir).expanduser().resolve()
        self.engine = engine_for(self.db_path)
        self.factory = session_factory(self.engine)

    def plan(self) -> dict[str, Any]:
        papers = _load_payloads(self.db_path, "papers")
        projects = _load_payloads(self.db_path, "projects")
        groups, conflicts = group_document_candidates([_candidate(item) for item in papers])
        sha_groups: dict[str, set[str]] = {}
        for group in groups:
            for item in group.candidates:
                if item.sha256:
                    sha_groups.setdefault(item.sha256, set()).add(group.identity_key)
        for sha, identities in sha_groups.items():
            if len(identities) > 1:
                conflicts.append(
                    {
                        "kind": "same_file_different_doi",
                        "sha256": sha,
                        "identity_keys": sorted(identities),
                        "paper_ids": sorted(
                            item.legacy_id
                            for group in groups
                            if group.identity_key in identities
                            for item in group.candidates
                            if item.sha256 == sha
                        ),
                    }
                )
        guides = [paper for paper in papers if isinstance(paper.get("summary"), dict) and paper["summary"].get("reading_guide")]
        summaries = [paper for paper in papers if paper.get("summary")]
        files = sum(1 for paper in papers if paper.get("local_path") or paper.get("sha256"))
        return {
            "db_path": str(self.db_path),
            "schema_versions": current_schema_versions(self.engine),
            "model_version": get_model_version(self.engine)
            if "v116_runtime_settings" in sa.inspect(self.engine).get_table_names()
            else "v15",
            "paper_count": len(papers),
            "project_count": len(projects),
            "document_group_count": len(groups),
            "groups": [
                {
                    "identity_key": group.identity_key,
                    "basis": group.basis,
                    "paper_ids": [item.legacy_id for item in group.candidates],
                }
                for group in groups
            ],
            "conflicts": conflicts,
            "file_candidates": files,
            "reading_guide_count": len(guides),
            "paper_summary_count": len(summaries),
            "annotation_candidates": len(
                discover_legacy_annotations(self.db_path, papers, projects)
            ),
        }

    def _create_run(self, source_counts: dict[str, Any], backup: Path, backup_sha: str) -> str:
        run_id = stable_id("mig", str(self.db_path), str(time.time_ns()))
        with self.factory() as session:
            session.add(
                MigrationRun(
                    id=run_id,
                    schema_from=",".join(current_schema_versions(self.engine)),
                    schema_to=SCHEMA_VERSION,
                    status="backed_up",
                    started_at=now_iso(),
                    backup_path=str(backup),
                    backup_sha256=backup_sha,
                    source_counts_json=source_counts,
                    result_counts_json={},
                    error_json={},
                    note="P0-01 side-by-side migration",
                )
            )
            session.commit()
        return run_id

    def _set_run(self, run_id: str, status: str, **updates: Any) -> None:
        with self.factory() as session:
            run = session.get(MigrationRun, run_id)
            if run is None:
                raise RuntimeError(f"migration run missing: {run_id}")
            run.status = status
            for key, value in updates.items():
                setattr(run, key, value)
            session.commit()

    def apply(self) -> dict[str, Any]:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        preflight = sqlite3.connect(self.db_path)
        try:
            integrity = preflight.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"legacy DB integrity check failed: {integrity}")
            tables = {
                row[0]
                for row in preflight.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted({"projects", "papers"} - tables)
            if missing:
                raise RuntimeError(f"legacy tables missing: {missing}")
        finally:
            preflight.close()
        annotation_table_state = _is_target_annotation_table(self.db_path)
        if annotation_table_state is False:
            raise RuntimeError(
                "pre-v1.16 annotations table conflicts with the target table name; "
                "export it to pdf_annotations or a JSON annotation adapter before migration"
            )

        before_digest = legacy_payload_digest(self.engine, LEGACY_TABLES)
        backup = _backup_sqlite(self.db_path, self.backup_dir)
        backup_sha = _sha256(backup)
        source_counts = {table: len(_load_payloads(self.db_path, table)) for table in LEGACY_TABLES}
        deploy_report = deploy_schema(self.engine, note="P0-01 migration deploy")
        run_id = self._create_run(source_counts, backup, backup_sha)

        try:
            self._set_run(run_id, "schema_created", result_counts_json=deploy_report)
            papers = _load_payloads(self.db_path, "papers")
            projects = _load_payloads(self.db_path, "projects")
            candidates = [_candidate(item) for item in papers]
            groups, conflicts = group_document_candidates(candidates)
            sha_groups: dict[str, set[str]] = {}
            for group in groups:
                for item in group.candidates:
                    if item.sha256:
                        sha_groups.setdefault(item.sha256, set()).add(group.identity_key)
            for sha, identities in sha_groups.items():
                if len(identities) > 1:
                    conflicts.append(
                        {
                            "kind": "same_file_different_doi",
                            "sha256": sha,
                            "identity_keys": sorted(identities),
                            "paper_ids": sorted(
                                item.legacy_id
                                for group in groups
                                if group.identity_key in identities
                                for item in group.candidates
                                if item.sha256 == sha
                            ),
                        }
                    )
            paper_by_id = {str(item["id"]): item for item in papers}
            project_ids = {str(item["id"]) for item in projects}

            with self.factory() as session:
                document_by_paper: dict[str, str] = {}
                membership_by_project_document: dict[tuple[str, str], ProjectDocument] = {}
                for group in groups:
                    source_papers = [paper_by_id[item.legacy_id] for item in group.candidates]
                    winner, metadata_values = _choose_metadata(source_papers)
                    document_id = stable_id("doc", group.identity_key)
                    existing = session.get(LibraryDocument, document_id)
                    idd = DocumentCreateInput(
                        id=document_id,
                        title=str(winner.get("title") or ""),
                        title_raw=str(winner.get("title_raw") or winner.get("title") or ""),
                        title_source=str(winner.get("title_source") or ""),
                        authors_raw=str(winner.get("authors") or ""),
                        year=str(winner.get("year") or ""),
                        venue=str(winner.get("venue") or ""),
                        doi=str(winner.get("doi") or ""),
                        entry_type=str(winner.get("entry_type") or "other"),
                        source_urls=list(winner.get("source_urls") or []),
                        external_ids=dict(winner.get("external_ids") or {}),
                        external_metadata=dict(winner.get("external_metadata") or {}),
                        metadata_checks=list(winner.get("metadata_checks") or []),
                        guess_sources=dict(winner.get("guess_sources") or {}),
                        parse_warnings=list(winner.get("parse_warnings") or []),
                        verification_status=str(winner.get("verification_status") or "unverified"),
                        verified_at=str(winner.get("verified_at") or ""),
                        legacy_created_at=str(winner.get("created_at") or ""),
                        fallback_legacy_id=str(winner["id"]),
                        sha256=str(winner.get("sha256") or ""),
                        metadata_values=metadata_values,
                    )
                    persisted_document_id = document_id
                    if existing is None:
                        from .services import DocumentService

                        created = DocumentService(self.factory).create_or_merge_in_session(
                            session, idd
                        )
                        persisted_document_id = str(created["id"])
                    for paper in source_papers:
                        legacy_id = str(paper["id"])
                        document_by_paper[legacy_id] = persisted_document_id
                        self._map(session, run_id, "library_document", "papers", legacy_id,
                                  persisted_document_id,
                                  "embedded" if not group.basis else group.basis)
                    project_rows: dict[str, list[dict[str, Any]]] = {}
                    for paper in source_papers:
                        project_id = str(paper.get("project_id") or "")
                        if project_id in project_ids:
                            project_rows.setdefault(project_id, []).append(paper)
                    for project_id, items in project_rows.items():
                        representative = min(items, key=lambda item: str(item.get("created_at") or ""))
                        pd_payload = ProjectDocumentCreateInput(
                            project_id=project_id,
                            document_id=persisted_document_id,
                            origin=_project_origin(representative),
                            reading_state="outline_draft" if any(
                                isinstance(item.get("summary"), dict)
                                and item["summary"].get("reading_guide")
                                for item in items
                            ) else "not_planned",
                            project_note="",
                            added_by="migration",
                            legacy_paper_id=str(representative["id"]),
                            legacy_paper_ids=sorted(str(item["id"]) for item in items),
                            legacy_payload=representative,
                        )
                        from .services import DocumentService

                        pd = DocumentService(self.factory).attach_to_project_in_session(
                            session, pd_payload
                        )
                        membership_by_project_document[(project_id, persisted_document_id)] = session.get(
                            ProjectDocument, pd["id"]
                        )
                        for item in items:
                            self._map(
                                session, run_id, "project_document", "papers",
                                str(item["id"]), pd["id"], "legacy_id",
                            )
                session.commit()
            self._set_run(run_id, "documents_built")
            self._set_run(run_id, "memberships_built")
            self._record_plan_conflicts(run_id, conflicts, document_by_paper)

            file_counts = self._migrate_files(run_id, papers, document_by_paper)
            self._set_run(run_id, "files_built", result_counts_json=file_counts)
            annotation_counts = self._migrate_annotations(
                run_id, papers, document_by_paper
            )
            outline_counts = self._migrate_outlines(run_id, papers, document_by_paper)
            summary_counts = self._migrate_summaries(run_id, papers, document_by_paper)
            relation_counts = self._migrate_relations(run_id)
            self._set_run(
                run_id,
                "derived_backfilled",
                result_counts_json={
                    **file_counts,
                    **annotation_counts,
                    **outline_counts,
                    **summary_counts,
                    **relation_counts,
                },
            )

            verification = self.verify()
            after_digest = legacy_payload_digest(self.engine, LEGACY_TABLES)
            if before_digest != after_digest:
                raise RuntimeError("legacy payload digest changed during migration")
            verification["legacy_payload_digest"] = before_digest
            verification["backup_path"] = str(backup)
            verification["backup_sha256"] = backup_sha
            self._set_run(
                run_id,
                "validated",
                finished_at=now_iso(),
                result_counts_json=verification,
            )
            return {
                "run_id": run_id,
                "backup_path": str(backup),
                "backup_sha256": backup_sha,
                "verification": verification,
                "planned_conflicts": conflicts,
            }
        except Exception as exc:
            self._set_run(
                run_id,
                "failed",
                finished_at=now_iso(),
                error_json={"type": type(exc).__name__, "message": str(exc)},
            )
            raise

    @staticmethod
    def _map(
        session: Session,
        run_id: str,
        entity_type: str,
        legacy_table: str,
        legacy_id: str,
        new_id: str,
        match_basis: str,
    ) -> None:
        if match_basis not in {"sha256", "doi", "title", "legacy_id", "embedded", "manual_override"}:
            match_basis = "legacy_id"
        key = (run_id, entity_type, legacy_table, legacy_id)
        if session.get(MigrationIdMap, key) is None:
            session.add(
                MigrationIdMap(
                    run_id=run_id,
                    entity_type=entity_type,
                    legacy_table=legacy_table,
                    legacy_id=legacy_id,
                    new_id=new_id,
                    match_basis=match_basis,
                    created_at=now_iso(),
                )
            )

    def _migrate_files(
        self, run_id: str, papers: list[dict[str, Any]], document_by_paper: dict[str, str]
    ) -> dict[str, int]:
        from .services import DocumentService

        assets = 0
        locations = 0
        missing = 0
        with self.factory() as session:
            service = DocumentService(self.factory)
            main_sha_by_document: dict[str, str] = {}
            for paper in sorted(
                papers,
                key=lambda item: (
                    not Path(str(item.get("local_path") or "")).is_file(),
                    str(item.get("created_at") or ""),
                    str(item.get("id") or ""),
                ),
            ):
                paper_id = str(paper["id"])
                document_id = document_by_paper.get(paper_id)
                if not document_id:
                    continue
                path_text = str(paper.get("local_path") or "")
                if not path_text:
                    continue
                path = Path(path_text)
                digest = str(paper.get("sha256") or "")
                if not _valid_sha256(digest) and path.is_file():
                    digest = _sha256(path)
                if not _valid_sha256(digest):
                    digest = hashlib.sha256(path_text.encode()).hexdigest()
                main_sha = main_sha_by_document.get(document_id)
                if main_sha is None or main_sha == digest:
                    role = "main_pdf"
                    main_sha_by_document[document_id] = digest
                else:
                    role = "supplement"
                payload = FileAssetCreateInput(
                    document_id=document_id,
                    role=role,
                    file_name=path.name or str(paper.get("title") or "document.pdf"),
                    storage_key=f"legacy/{digest}/{path.name or 'document.pdf'}",
                    mime_type="application/pdf",
                    sha256=digest,
                    size_bytes=path.stat().st_size if path.is_file() else 0,
                    page_count=paper.get("page_count"),
                    text_layer="available" if path.with_suffix(".pages.txt").is_file() else "missing",
                    legacy_payload=paper,
                    legacy_paths=[path_text],
                    available_at_migration=path.is_file(),
                )
                out = service.add_file_in_session(session, payload)
                assets += 1
                locations += 1
                if not path.is_file():
                    missing += 1
                    self._conflict(
                        session, run_id, "missing_file", [paper_id], [document_id],
                        {"path": path_text},
                    )
            session.commit()
        return {"file_assets": assets, "file_locations": locations, "missing_files": missing}

    def _migrate_annotations(
        self,
        run_id: str,
        papers: list[dict[str, Any]],
        document_by_paper: dict[str, str],
    ) -> dict[str, int]:
        from .services import AnnotationService

        rows = discover_legacy_annotations(
            self.db_path,
            papers,
            _load_payloads(self.db_path, "projects"),
        )
        created = 0
        skipped = 0
        unmapped = 0
        with self.factory() as session:
            service = AnnotationService(self.factory)
            for row in rows:
                document_id = document_by_paper.get(row.paper_id)
                if not document_id:
                    self._unmapped(
                        session,
                        run_id,
                        row.source_table,
                        row.legacy_id,
                        row.payload,
                        "legacy annotation has no mapped Paper",
                    )
                    unmapped += 1
                    continue
                files = session.scalars(
                    sa.select(FileAsset).where(
                        FileAsset.document_id == document_id,
                        FileAsset.deleted_at.is_(None),
                    )
                ).all()
                if not files:
                    self._unmapped(
                        session,
                        run_id,
                        row.source_table,
                        row.legacy_id,
                        row.payload,
                        "legacy annotation has no file asset",
                    )
                    unmapped += 1
                    continue
                main_files = [item for item in files if item.role == "main_pdf"]
                if len(main_files) == 1:
                    file = main_files[0]
                elif len(files) == 1:
                    file = files[0]
                else:
                    self._conflict(
                        session,
                        run_id,
                        "invalid_annotation",
                        [row.paper_id, row.legacy_id],
                        [document_id],
                        {
                            "reason": "multiple files and no unique main_pdf",
                            "legacy_annotation": row.payload,
                        },
                    )
                    unmapped += 1
                    continue
                if row.page_index is None:
                    self._conflict(
                        session,
                        run_id,
                        "invalid_annotation",
                        [row.paper_id, row.legacy_id],
                        [document_id],
                        {"reason": "page basis is unknown", "legacy_annotation": row.payload},
                    )
                    unmapped += 1
                    continue
                annotation_type = {
                    "text": "highlight",
                    "comment": "note",
                    "strikethrough": "strikeout",
                }.get(row.type, row.type)
                if annotation_type not in {
                    "highlight",
                    "underline",
                    "strikeout",
                    "note",
                    "region",
                }:
                    self._conflict(
                        session,
                        run_id,
                        "invalid_annotation",
                        [row.paper_id, row.legacy_id],
                        [document_id],
                        {"reason": f"unsupported annotation type: {row.type}"},
                    )
                    unmapped += 1
                    continue
                color = row.color
                if color and not (
                    len(color) == 7
                    and color.startswith("#")
                    and all(char in "0123456789abcdefABCDEF" for char in color[1:])
                ):
                    color = ""
                annotation_id = stable_id(
                    "ann",
                    document_id,
                    file.id,
                    str(row.page_index),
                    row.legacy_id,
                )
                existing = session.get(Annotation, annotation_id)
                if existing is None:
                    rects: list[dict[str, float]] = []
                    for rect in row.rects:
                        if isinstance(rect, dict):
                            rects.append(rect)
                    payload = AnnotationCreateInput(
                        id=annotation_id,
                        document_id=document_id,
                        file_id=file.id,
                        type=annotation_type,
                        quote=row.quote,
                        comment_markdown=row.comment,
                        color=color,
                        page_index=row.page_index,
                        page_label=row.page_label,
                        text_source="pdf_text",
                        rects=rects,
                        selector={"legacyPageBasis": row.page_basis},
                        sort_index=stable_id("sort", str(row.page_index), row.legacy_id),
                        anchor_status="exact" if row.quote else "none",
                        author_id=row.author_id,
                        source=row.source if row.source in {
                            "user", "external_pdf", "ai_suggested", "legacy"
                        } else "legacy",
                        read_only=row.read_only,
                        external_id=row.external_id,
                        legacy_payload=row.payload,
                    )
                    service.create_in_session(session, payload)
                    created += 1
                else:
                    skipped += 1
                self._map(
                    session,
                    run_id,
                    "annotation",
                    row.source_table,
                    row.legacy_id,
                    annotation_id,
                    "legacy_id",
                )
                for tag_name in row.tags:
                    tag_key = normalize_title(tag_name)
                    if not tag_key:
                        continue
                    tag = session.scalar(sa.select(Tag).where(Tag.tag_key == tag_key))
                    if tag is None:
                        tag = Tag(
                            id=stable_id("tag", tag_key),
                            tag_key=tag_key,
                            display_name=tag_name.strip(),
                            created_at=now_iso(),
                        )
                        session.add(tag)
                        session.flush()
                    if session.get(AnnotationTag, (annotation_id, tag.id)) is None:
                        session.add(
                            AnnotationTag(
                                annotation_id=annotation_id,
                                tag_id=tag.id,
                                created_at=now_iso(),
                            )
                        )
            session.commit()
        return {
            "annotations": created,
            "annotations_reused": skipped,
            "annotations_unmapped": unmapped,
        }

    def _migrate_outlines(
        self, run_id: str, papers: list[dict[str, Any]], document_by_paper: dict[str, str]
    ) -> dict[str, int]:
        from .services import OutlineService

        counts = {"outline_versions": 0, "outline_nodes": 0}
        ordered_papers = sorted(
            papers,
            key=lambda item: str(
                (
                    ((item.get("summary") or {}).get("reading_guide") or {}).get(
                        "generated_at"
                    )
                    if isinstance(item.get("summary"), dict)
                    else ""
                )
                or item.get("created_at")
                or ""
            ),
        )
        with self.factory() as session:
            service = OutlineService(self.factory)
            for paper in ordered_papers:
                summary = paper.get("summary")
                if not isinstance(summary, dict):
                    continue
                guide = summary.get("reading_guide")
                if not isinstance(guide, dict):
                    continue
                document_id = document_by_paper.get(str(paper["id"]))
                if not document_id:
                    continue
                reading_nodes, reading_markdown = _guide_nodes(
                    guide, document_id, str(paper["id"])
                )
                structure_nodes, structure_markdown = _structure_nodes(
                    guide, document_id, str(paper["id"])
                )
                for kind, nodes, markdown in (
                    ("reading_outline", reading_nodes, reading_markdown),
                    ("paper_structure", structure_nodes, structure_markdown),
                ):
                    if not nodes:
                        continue
                    source_run_id = f"legacy:{paper['id']}:{kind}"
                    if session.scalar(
                        sa.select(OutlineVersion).where(
                            OutlineVersion.document_id == document_id,
                            OutlineVersion.kind == kind,
                            OutlineVersion.source_run_id == source_run_id,
                        )
                    ):
                        continue
                    existing_versions = session.scalars(
                        sa.select(OutlineVersion).where(
                            OutlineVersion.document_id == document_id,
                            OutlineVersion.kind == kind,
                        )
                    ).all()
                    version_no = max(
                        (item.version for item in existing_versions), default=0
                    ) + 1
                    for item in existing_versions:
                        if item.is_current:
                            item.is_current = False
                            item.status = "archived"
                    session.flush()
                    for node in nodes:
                        for evidence in node.evidence:
                            if not evidence.annotation_id:
                                continue
                            mapped = self._mapped_annotation(
                                session, evidence.annotation_id, document_id
                            )
                            if mapped:
                                evidence.annotation_id = mapped
                                evidence.kind = "annotation"
                            else:
                                evidence.annotation_id = None
                                if evidence.page_index is not None:
                                    evidence.kind = "page"
                                else:
                                    evidence.kind = "document"
                                    evidence.match_status = "none"
                    payload = OutlineVersionCreateInput(
                        document_id=document_id,
                        kind=kind,
                        version=version_no,
                        status="ai_draft",
                        is_current=True,
                        markdown=markdown,
                        source_run_id=source_run_id,
                        created_by="legacy",
                        legacy_source=guide,
                        nodes=nodes,
                    )
                    service.create_version_in_session(session, payload)
                    counts["outline_versions"] += 1
                    counts["outline_nodes"] += len(nodes)
            session.commit()
        return counts

    def _migrate_summaries(
        self, run_id: str, papers: list[dict[str, Any]], document_by_paper: dict[str, str]
    ) -> dict[str, int]:
        from .services import SummaryService

        grouped: dict[str, list[dict[str, Any]]] = {}
        for paper in papers:
            if paper.get("summary"):
                grouped.setdefault(str(paper.get("project_id") or ""), []).append(paper)
        counts = {"summary_versions": 0, "summary_sections": 0, "summary_evidence": 0}
        with self.factory() as session:
            for project_id, items in grouped.items():
                if not project_id:
                    continue
                sections: list[SummarySectionInput] = []
                documents: list[str] = []
                for paper_index, paper in enumerate(items):
                    document_id = document_by_paper.get(str(paper["id"]))
                    if not document_id:
                        continue
                    documents.append(document_id)
                    guide = (paper.get("summary") or {}).get("reading_guide") or {}
                    dimensions = guide.get("dimensions") if isinstance(guide, dict) else None
                    body_parts: list[str] = []
                    evidence_rows: list[SummarySectionEvidenceInput] = []
                    if isinstance(dimensions, dict):
                        for dim in dimensions.values():
                            if not isinstance(dim, dict):
                                continue
                            if dim.get("title"):
                                body_parts.append(f"### {dim['title']}")
                            if dim.get("summary"):
                                body_parts.append(str(dim["summary"]))
                            for point in dim.get("points") or []:
                                body_parts.append(f"- {point}")
                            for evidence in dim.get("evidence") or []:
                                if not isinstance(evidence, dict):
                                    continue
                                page = evidence.get("page")
                                evidence_rows.append(
                                    SummarySectionEvidenceInput(
                                        kind="page" if page else "document",
                                        document_id=document_id,
                                        page_index=(
                                            (int(page) - 1)
                                            if page and int(page) > 0
                                            else None
                                        ),
                                        quote=str(evidence.get("quote") or ""),
                                        match_status=(
                                            "exact"
                                            if evidence.get("verification") == "exact"
                                            else "partial"
                                        ),
                                    )
                                )
                    elif guide.get("sections"):
                        for section in guide["sections"]:
                            if not isinstance(section, dict):
                                continue
                            if section.get("title"):
                                body_parts.append(f"### {section['title']}")
                            if section.get("summary"):
                                body_parts.append(str(section["summary"]))
                    else:
                        analysis = (paper.get("summary") or {}).get("ai_analysis") or {}
                        for kept in analysis.get("kept") or []:
                            if isinstance(kept, dict) and kept.get("text"):
                                body_parts.append(f"- {kept['text']}")
                    title = str(paper.get("title") or paper["id"])
                    sections.append(
                        SummarySectionInput(
                            id=stable_id("sumsec", project_id, str(paper["id"])),
                            stable_key=f"legacy.{paper['id']}",
                            order_index=paper_index,
                            title=title,
                            body_markdown=(
                                "\n\n".join(body_parts)
                                or "**原始摘要仅保存在 legacy_source_json。**"
                            ),
                            evidence=evidence_rows,
                        )
                    )
                if not sections:
                    continue
                legacy_source = {
                    "papers": {
                        str(paper["id"]): paper.get("summary") or {}
                        for paper in items
                    }
                }
                legacy_source_hash = _payload_hash(legacy_source)
                existing_legacy = session.scalar(
                    sa.select(SummaryVersion).where(
                        SummaryVersion.project_id == project_id,
                        SummaryVersion.legacy_source_hash == legacy_source_hash,
                    )
                )
                if existing_legacy is not None:
                    continue
                max_version = session.scalar(
                    sa.select(sa.func.max(SummaryVersion.version)).where(
                        SummaryVersion.project_id == project_id
                    )
                ) or 0
                payload = SummaryVersionCreateInput(
                    project_id=project_id,
                    version=max_version + 1,
                    mode="independent",
                    status="final",
                    title="旧版阶段总结",
                    markdown="# 旧版阶段总结\n\n" + "\n\n".join(
                        f"## {item.title}\n\n{item.body_markdown}" for item in sections
                    ),
                    run_id=f"legacy-summary-{project_id}-{legacy_source_hash[:16]}",
                    created_by="legacy",
                    finalized_at=now_iso(),
                    legacy_source=legacy_source,
                    legacy_source_hash=legacy_source_hash,
                    section_evidence=sections,
                    documents=sorted(set(documents)),
                )
                SummaryService(self.factory).create_version_in_session(session, payload)
                counts["summary_versions"] += 1
                counts["summary_sections"] += len(sections)
                counts["summary_evidence"] += sum(len(item.evidence) for item in sections)
            session.commit()
        return counts

    def _migrate_relations(self, run_id: str) -> dict[str, int]:
        mapped = 0
        conflicts = 0
        with self.factory() as session:
            for relation in _load_payloads(self.db_path, "paper_relations"):
                from_id = self._mapped_doc(session, str(relation.get("from_paper_id") or ""))
                to_id = self._mapped_doc(session, str(relation.get("to_paper_id") or ""))
                project_id = str(relation.get("project_id") or "")
                project_exists = session.execute(
                    sa.text("SELECT 1 FROM projects WHERE id = :id"),
                    {"id": project_id},
                ).first()
                if not from_id or not to_id or from_id == to_id or not project_exists:
                    session.add(
                        MigrationConflict(
                            id=stable_id("mc", run_id, "relation", str(relation.get("id"))),
                            run_id=run_id,
                            kind="unknown_legacy_shape",
                            legacy_ids_json=[str(relation.get("id"))],
                            document_ids_json=[item for item in (from_id, to_id) if item],
                            detail_json=relation,
                            created_at=now_iso(),
                        )
                    )
                    conflicts += 1
                    continue
                fingerprint = _payload_hash(
                    [
                        relation.get("project_id"),
                        from_id,
                        to_id,
                        relation.get("relation_type"),
                        relation.get("source"),
                        relation.get("external_id"),
                    ]
                )
                if session.scalar(
                    sa.select(DocumentRelation).where(
                        DocumentRelation.project_id == project_id,
                        DocumentRelation.fingerprint == fingerprint,
                    )
                ):
                    continue
                session.add(
                    DocumentRelation(
                        id=stable_id("drel", fingerprint),
                        project_id=project_id,
                        from_document_id=from_id,
                        to_document_id=to_id,
                        relation_type=str(relation.get("relation_type") or "cites"),
                        source=str(relation.get("source") or "user"),
                        provider=str(relation.get("provider") or ""),
                        external_id=str(relation.get("external_id") or ""),
                        locator=str(relation.get("locator") or ""),
                        source_record_id=str(relation.get("source_record_id") or ""),
                        certainty=str(relation.get("certainty") or "inferred"),
                        verified=bool(relation.get("verified")),
                        review_status=str(relation.get("review_status") or "pending"),
                        review_note=str(relation.get("review_note") or ""),
                        fingerprint=fingerprint,
                        created_at=str(relation.get("created_at") or now_iso()),
                        updated_at=now_iso(),
                    )
                )
                mapped += 1
            session.commit()
        return {"document_relations": mapped, "relation_conflicts": conflicts}

    @staticmethod
    def _mapped_doc(session: Session, paper_id: str) -> str | None:
        if not paper_id:
            return None
        row = session.scalar(
            sa.select(MigrationIdMap)
            .where(
                MigrationIdMap.entity_type == "library_document",
                MigrationIdMap.legacy_id == paper_id,
            )
            .order_by(MigrationIdMap.created_at.desc())
        )
        return row.new_id if row else None

    @staticmethod
    def _mapped_annotation(
        session: Session, legacy_annotation_id: str, document_id: str
    ) -> str | None:
        if not legacy_annotation_id:
            return None
        mapping = session.scalar(
            sa.select(MigrationIdMap)
            .where(
                MigrationIdMap.entity_type == "annotation",
                MigrationIdMap.legacy_id == legacy_annotation_id,
            )
            .order_by(MigrationIdMap.created_at.desc())
        )
        if mapping is None:
            return None
        annotation = session.get(Annotation, mapping.new_id)
        return (
            annotation.id
            if annotation is not None and annotation.document_id == document_id
            else None
        )

    @staticmethod
    def _conflict(
        session: Session,
        run_id: str,
        kind: str,
        legacy_ids: list[str],
        document_ids: list[str],
        detail: dict[str, Any],
    ) -> None:
        session.add(
            MigrationConflict(
                id=stable_id("mc", run_id, kind, *legacy_ids),
                run_id=run_id,
                kind=kind,
                legacy_ids_json=legacy_ids,
                document_ids_json=document_ids,
                detail_json=detail,
                created_at=now_iso(),
            )
        )

    @staticmethod
    def _unmapped(
        session: Session,
        run_id: str,
        legacy_table: str,
        legacy_id: str,
        payload: dict[str, Any],
        reason: str,
    ) -> None:
        record_id = stable_id("mur", run_id, legacy_table, legacy_id)
        if session.get(MigrationUnmappedRecord, record_id) is None:
            session.add(
                MigrationUnmappedRecord(
                    id=record_id,
                    run_id=run_id,
                    legacy_table=legacy_table,
                    legacy_id=legacy_id,
                    payload_json=payload,
                    reason=reason,
                    created_at=now_iso(),
                )
            )

    def _record_plan_conflicts(
        self,
        run_id: str,
        conflicts: list[dict[str, Any]],
        document_by_paper: dict[str, str],
    ) -> None:
        """Persist preflight dedup conflicts with resolved canonical IDs."""
        if not conflicts:
            return
        with self.factory() as session:
            for index, conflict in enumerate(conflicts):
                kind = str(conflict.get("kind") or "unknown_legacy_shape")
                if kind not in {
                    "doi_title_mismatch",
                    "same_file_different_doi",
                    "title_collision",
                    "ambiguous_title_match",
                    "metadata_conflict",
                    "missing_file",
                    "invalid_outline",
                    "invalid_annotation",
                    "unknown_legacy_shape",
                }:
                    kind = "unknown_legacy_shape"
                legacy_ids = [
                    str(value)
                    for value in conflict.get("legacy_ids", conflict.get("paper_ids", []))
                ]
                document_ids = sorted(
                    {
                        document_by_paper[legacy_id]
                        for legacy_id in legacy_ids
                        if legacy_id in document_by_paper
                    }
                )
                conflict_id = stable_id("mc", run_id, kind, str(index))
                if session.get(MigrationConflict, conflict_id) is None:
                    session.add(
                        MigrationConflict(
                            id=conflict_id,
                            run_id=run_id,
                            kind=kind,
                            legacy_ids_json=legacy_ids,
                            document_ids_json=document_ids,
                            detail_json=conflict,
                            resolution="pending",
                            created_at=now_iso(),
                        )
                    )
            session.commit()

    def verify(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            foreign_keys = [tuple(row) for row in conn.exec_driver_sql("PRAGMA foreign_key_check")]
            integrity_row = conn.exec_driver_sql("PRAGMA integrity_check").fetchone()
            if foreign_keys:
                raise RuntimeError(f"foreign key check failed: {foreign_keys[:5]}")
            if not integrity_row or integrity_row[0] != "ok":
                raise RuntimeError(f"integrity check failed: {integrity_row}")
            counts = {}
            for table in (
                "library_documents",
                "project_documents",
                "file_assets",
                "file_asset_locations",
                "annotations",
                "outline_versions",
                "outline_nodes",
                "summary_versions",
                "summary_sections",
                "summary_section_evidence",
                "document_relations",
            ):
                counts[table] = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")
                ).scalar_one()
            duplicate_doi = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM ("
                    "SELECT doi_key FROM library_documents "
                    "WHERE doi_key IS NOT NULL AND doi_key <> '' "
                    "GROUP BY doi_key HAVING COUNT(*) > 1)"
                )
            ).scalar_one()
            if duplicate_doi:
                raise RuntimeError("duplicate DOI keys found")
            paper_count = conn.execute(sa.text("SELECT COUNT(*) FROM papers")).scalar_one()
            mapped_count = conn.execute(
                sa.text(
                    "SELECT COUNT(DISTINCT legacy_id) FROM migration_id_map "
                    "WHERE entity_type='project_document' AND legacy_table='papers'"
                )
            ).scalar_one()
            if mapped_count != paper_count:
                raise RuntimeError(
                    f"paper mapping incomplete: {mapped_count}/{paper_count}"
                )
            orphan_docs = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM library_documents d "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM project_documents p WHERE p.document_id=d.id)"
                )
            ).scalar_one()
            if orphan_docs:
                raise RuntimeError(f"orphan canonical documents: {orphan_docs}")
            return {
                "ok": True,
                "counts": counts,
                "paper_count": paper_count,
                "mapped_papers": mapped_count,
                "foreign_key_errors": 0,
                "integrity": "ok",
            }

    def shadow_check(self) -> dict[str, Any]:
        plan = self.plan()
        verification = self.verify()
        actual_docs = verification["counts"]["library_documents"]
        ok = actual_docs == plan["document_group_count"]
        return {
            "ok": ok,
            "expected_documents": plan["document_group_count"],
            "actual_documents": actual_docs,
            "verification": verification,
        }

    def cutover_shadow(self) -> None:
        report = self.shadow_check()
        if not report["ok"]:
            raise RuntimeError(f"shadow parity failed: {report}")
        set_model_version(self.engine, "v116_shadow")
        self._set_latest_run_status("cutover_ready")

    def cutover_on(self) -> None:
        report = self.shadow_check()
        if not report["ok"]:
            raise RuntimeError(f"cutover validation failed: {report}")
        set_model_version(self.engine, "v116")
        self._set_latest_run_status("committed", finished_at=now_iso())

    def _set_latest_run_status(self, status: str, **updates: Any) -> None:
        with self.factory() as session:
            run = session.scalar(
                sa.select(MigrationRun)
                .where(MigrationRun.status.in_(["validated", "cutover_ready"]))
                .order_by(MigrationRun.started_at.desc())
            )
            if run is None:
                raise RuntimeError("no validated migration run found")
            run.status = status
            for key, value in updates.items():
                setattr(run, key, value)
            session.commit()

    def rollback(self, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("rollback requires confirm=True")
        with self.factory() as session:
            run = session.scalar(
                sa.select(MigrationRun)
                .where(
                    MigrationRun.status.in_(
                        ["validated", "cutover_ready", "committed"]
                    )
                )
                .order_by(MigrationRun.started_at.desc())
            )
            if run is None or not run.backup_path:
                raise RuntimeError("no validated migration backup found")
            backup = Path(run.backup_path)
            expected = run.backup_sha256
        if not backup.is_file():
            raise FileNotFoundError(backup)
        if _sha256(backup) != expected:
            raise RuntimeError("backup SHA-256 mismatch")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        failed_snapshot = (
            self.backup_dir
            / f"failed_release_{time.strftime('%Y%m%dT%H%M%S')}_{time.time_ns()}.db"
        )
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(failed_snapshot)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        self.engine.dispose()
        shutil.copy2(backup, self.db_path)
        self.engine = engine_for(self.db_path)
        self.factory = session_factory(self.engine)
        return {
            "ok": True,
            "restored_from": str(backup),
            "failed_release_snapshot": str(failed_snapshot),
        }

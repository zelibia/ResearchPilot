"""P0-06 summary revisions, generation, evidence and official pointer service."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..storage.v116.db import session_scope
from ..agent.client import LLMClient
from ..storage.v116.orm import (
    Annotation,
    FileAsset,
    LibraryDocument,
    OutlineRevision,
    OutlineRevisionNode,
    ProjectDocument,
    ProjectSummary,
    SummaryAIRunRecord,
    SummaryEvidenceRecord,
    SummaryMutationReceipt,
    SummaryOperationLog,
    SummaryRevisionDocumentRecord,
    SummaryRevisionRecord,
    SummaryRevisionSection,
    SummarySectionEntity,
    SummaryVersion,
    SummarySection as LegacySummarySection,
    SummaryEventOutbox,
)
from .contracts import (
    CompleteGenerationRequest,
    ConfirmRevisionCommand,
    GenerateSummaryCommand,
    RejectRevisionCommand,
    RestoreSummaryRevisionCommand,
    SaveMarkdownCommand,
    SummaryAIResult,
    UpdateSectionCommand,
)
from .errors import SummaryError
from .events import emit, hash_json, now, out


def _revision_out(row: SummaryRevisionRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "summaryId": row.summary_id,
        "revisionNo": row.revision_no,
        "mode": row.mode,
        "baseRevisionId": row.base_revision_id,
        "baseRevisionNo": row.base_revision_no,
        "status": row.status,
        "source": row.source,
        "scope": row.scope_json,
        "title": row.title,
        "markdown": row.markdown,
        "structured": row.structured_json,
        "changeSummary": row.change_summary,
        "contentHash": row.content_hash,
        "createdBy": row.created_by,
        "createdAt": row.created_at,
        "confirmedBy": row.confirmed_by,
        "confirmedAt": row.confirmed_at,
        "aiRunId": row.ai_run_id,
        "legacySource": row.legacy_source_json,
        "documents": [],
    }


def _run_out(row: SummaryAIRunRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "summaryId": row.summary_id,
        "mode": row.mode,
        "baseRevisionId": row.base_revision_id,
        "baseRevisionNo": row.base_revision_no,
        "scopeType": row.scope_type,
        "scope": row.scope_json,
        "status": row.status,
        "provider": row.provider,
        "model": row.model,
        "promptVersion": row.prompt_version,
        "inputHash": row.input_hash,
        "inputScope": row.input_scope_json,
        "result": row.result_json or None,
        "errorCode": row.error_code,
        "errorMessage": row.error_message,
        "requestedBy": row.requested_by,
        "startedAt": row.started_at,
        "finishedAt": row.finished_at,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


class SummaryApplicationService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        llm_client: LLMClient | None = None,
    ) -> None:
        self.factory = factory
        self.llm_client = llm_client

    def generate_live(self, command: GenerateSummaryCommand) -> dict[str, Any]:
        """Queue then execute a configured LLMClient run.

        Offline deployments leave ``llm_client`` empty and use the
        ``/ai-runs/{runId}/complete`` worker endpoint. The service layer is the
        same for both paths, so evidence validation and candidate persistence
        cannot diverge.
        """

        if self.llm_client is None:
            raise SummaryError("AI_NOT_CONNECTED", "未配置 LLMClient")
        run = self.generate(command)
        if run.get("status") != "queued":
            return run
        with session_scope(self.factory) as session:
            catalog = self._evidence_catalog(
                session, run["scope"].get("documentIds", [])
            )
        prompt = {
            "mode": command.mode,
            "scope": run["scope"],
            "evidenceSources": list(catalog.values()),
            "instruction": (
                "Only cite evidence tokens from evidenceSources. Return exactly "
                '{"schemaVersion":"summary.v1","title":"...","overviewMarkdown":"...",'
                '"sections":[{"stableKey":"method","title":"...","bodyMarkdown":"...",'
                '"evidenceTokens":["A:..."],"changeStatus":"changed"}],'
                '"unresolvedQuestions":[],"warnings":[]}.'
            ),
        }
        raw, _response = self.llm_client.complete_json(
            "You write evidence-grounded research project summaries.",
            json.dumps(prompt, ensure_ascii=False),
            json_schema={
                "type": "object",
                "required": ["schemaVersion", "title", "overviewMarkdown", "sections"],
                "properties": {
                    "schemaVersion": {"const": "summary.v1"},
                    "title": {"type": "string"},
                    "overviewMarkdown": {"type": "string"},
                    "sections": {"type": "array", "items": {"type": "object"}},
                },
            },
        )
        result = SummaryAIResult.model_validate(raw)
        aliases = {
            "new": "added",
            "modified": "changed",
            "same": "unchanged",
            "deleted": "removed",
            "insufficient": "insufficient_evidence",
        }
        for section in result.sections:
            status = section.get("changeStatus")
            section["changeStatus"] = aliases.get(status, status)
        return self.complete_generation(
            run["summaryId"],
            run["id"],
            CompleteGenerationRequest(result=result, actor=command.actor),
        )

    # ------------------------------------------------------------------
    # Query and history
    # ------------------------------------------------------------------

    def get_project_summary(self, project_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            summary = self._ensure_summary(session, project_id)
            revisions = session.scalars(
                sa.select(SummaryRevisionRecord)
                .where(SummaryRevisionRecord.summary_id == summary.id)
                .order_by(SummaryRevisionRecord.revision_no.desc())
            ).all()
            return {
                "summary": {
                    "id": summary.id,
                    "projectId": summary.project_id,
                    "currentOfficialRevisionId": summary.current_official_revision_id,
                    "currentRevisionNo": summary.current_revision_no,
                    "createdAt": summary.created_at,
                    "updatedAt": summary.updated_at,
                },
                "revisions": [
                    {
                        "revisionNo": row.revision_no,
                        "status": row.status,
                        "mode": row.mode,
                        "baseRevisionNo": row.base_revision_no,
                        "createdAt": row.created_at,
                        "createdBy": row.created_by,
                        "changeSummary": row.change_summary,
                    }
                    for row in revisions
                ],
            }

    def list_revisions(self, summary_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            self._require_summary(session, summary_id)
            rows = session.scalars(
                sa.select(SummaryRevisionRecord)
                .where(SummaryRevisionRecord.summary_id == summary_id)
                .order_by(SummaryRevisionRecord.revision_no.desc())
            ).all()
            return {"items": [self._revision_full(session, row) for row in rows]}

    def get_revision(self, summary_id: str, revision_no: int) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = self._revision(session, summary_id, revision_no)
            return self._revision_full(session, row)

    def _revision_full(
        self, session: Session, row: SummaryRevisionRecord
    ) -> dict[str, Any]:
        payload = _revision_out(row)
        payload["documents"] = [
            {
                "documentId": item.document_id,
                "inclusionStatus": item.inclusion_status,
                "exclusionReason": item.exclusion_reason,
                "fileSha256": item.file_sha256,
                "outlineRevisionId": item.outline_revision_id,
                "outlineRevisionNo": item.outline_revision_no,
                "annotationCount": item.annotation_count,
                "sourceUpdatedAt": item.source_updated_at,
            }
            for item in session.scalars(
                sa.select(SummaryRevisionDocumentRecord).where(
                    SummaryRevisionDocumentRecord.revision_id == row.id
                )
            ).all()
        ]
        return payload

    def _ensure_summary(self, session: Session, project_id: str) -> ProjectSummary:
        summary = session.scalar(
            sa.select(ProjectSummary).where(ProjectSummary.project_id == project_id)
        )
        if summary:
            return summary
        summary = ProjectSummary(
            id=f"psum_{uuid.uuid4().hex}",
            project_id=project_id,
            current_official_revision_id=None,
            current_revision_no=0,
            created_at=now(),
            updated_at=now(),
        )
        session.add(summary)
        session.flush()
        self._migrate_legacy(session, summary)
        return summary

    def _migrate_legacy(self, session: Session, summary: ProjectSummary) -> None:
        versions = session.scalars(
            sa.select(SummaryVersion)
            .where(SummaryVersion.project_id == summary.project_id)
            .order_by(SummaryVersion.version)
        ).all()
        if not versions:
            return
        sections: list[dict[str, Any]] = []
        legacy: dict[str, Any] = {}
        for version in versions:
            legacy[version.id] = version.legacy_source_json
            old_sections = session.scalars(
                sa.select(LegacySummarySection)
                .where(LegacySummarySection.summary_version_id == version.id)
                .order_by(LegacySummarySection.order_index)
            ).all()
            for item in old_sections:
                sections.append(
                    {
                        "id": f"sect_{uuid.uuid4().hex}",
                        "stableKey": item.stable_key,
                        "parentSectionId": None,
                        "orderIndex": item.order_index,
                        "title": item.title,
                        "bodyMarkdown": item.body_markdown,
                        "changeStatus": None,
                        "contentHash": hash_json([item.title, item.body_markdown]),
                        "evidence": [],
                        "createdAt": now(),
                        "updatedAt": now(),
                    }
                )
        if not sections:
            sections = [
                {
                    "id": f"sect_{uuid.uuid4().hex}",
                    "stableKey": "legacy",
                    "parentSectionId": None,
                    "orderIndex": 0,
                    "title": "旧版总结",
                    "bodyMarkdown": versions[-1].markdown,
                    "changeStatus": None,
                    "contentHash": hash_json(versions[-1].markdown),
                    "evidence": [],
                    "createdAt": now(),
                    "updatedAt": now(),
                }
            ]
        revision = self._store_revision(
            session,
            summary,
            mode="independent",
            base=None,
            status="official",
            source="legacy",
            scope={"type": "project", "documentIds": []},
            title="旧版阶段总结",
            markdown="\n\n".join(item["bodyMarkdown"] for item in sections),
            sections=sections,
            change_summary="迁移旧项目总结",
            created_by="legacy",
            ai_run_id=None,
            legacy_source=legacy,
            legacy_hash=hash_json(legacy),
            documents=[],
        )
        revision.confirmed_by = "legacy"
        revision.confirmed_at = now()
        summary.current_official_revision_id = revision.id
        summary.current_revision_no = revision.revision_no
        session.flush()

    def _require_summary(self, session: Session, summary_id: str) -> ProjectSummary:
        row = session.get(ProjectSummary, summary_id)
        if not row:
            raise SummaryError("SUMMARY_NOT_FOUND", summary_id)
        return row

    def _revision(
        self, session: Session, summary_id: str, revision_no: int
    ) -> SummaryRevisionRecord:
        row = session.scalar(
            sa.select(SummaryRevisionRecord).where(
                SummaryRevisionRecord.summary_id == summary_id,
                SummaryRevisionRecord.revision_no == revision_no,
            )
        )
        if not row:
            raise SummaryError("REVISION_NOT_FOUND", str(revision_no))
        return row

    # ------------------------------------------------------------------
    # Scope and generation
    # ------------------------------------------------------------------

    def generate(self, command: GenerateSummaryCommand) -> dict[str, Any]:
        request_hash = hash_json(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replay = self._receipt(
                session, command.client_mutation_id, "generate", request_hash
            )
            if replay:
                return replay
            summary = self._ensure_summary(session, command.project_id)
            base = None
            if command.mode == "incremental":
                if not command.base_revision_no:
                    raise SummaryError(
                        "INCREMENTAL_BASE_REQUIRED", "增量生成缺少 baseRevisionNo"
                    )
                base = self._revision(
                    session, summary.id, command.base_revision_no
                )
                if base.status != "official":
                    raise SummaryError(
                        "BASE_REVISION_NOT_OFFICIAL", "增量基线必须是正式版本"
                    )
            scope = self._resolve_scope(session, command.project_id, command.scope)
            run = SummaryAIRunRecord(
                id=f"srun_{uuid.uuid4().hex}",
                summary_id=summary.id,
                mode=command.mode,
                base_revision_id=base.id if base else None,
                base_revision_no=base.revision_no if base else None,
                scope_type=scope["type"],
                scope_json=scope,
                status="queued",
                provider=command.provider or "deepseek",
                model=command.model or "deepseek-flash",
                prompt_version=command.prompt_version,
                input_hash=hash_json({"scope": scope, "base": base.content_hash if base else None}),
                input_scope_json={"includedDocumentIds": scope["documentIds"]},
                result_json={},
                error_code="",
                error_message="",
                requested_by=command.actor.actor_id,
                created_at=now(),
                updated_at=now(),
            )
            session.add(run)
            session.flush()
            events = [
                emit(
                    session,
                    event_type="generate_progress",
                    project_id=summary.project_id,
                    summary_id=summary.id,
                    payload={
                        "runId": run.id,
                        "summaryId": summary.id,
                        "status": "queued",
                        "stage": "scope_resolved",
                        "completedUnits": 0,
                        "totalUnits": len(scope["documentIds"]),
                        "message": "生成范围已冻结",
                    },
                )
            ]
            result = _run_out(run)
            result["events"] = events
            self._save_receipt(
                session, command.client_mutation_id, "generate", request_hash, summary.id, result
            )
            return result

    def _resolve_scope(
        self, session: Session, project_id: str, scope: Any
    ) -> dict[str, Any]:
        if scope.type == "project":
            ids = session.scalars(
                sa.select(ProjectDocument.document_id).where(
                    ProjectDocument.project_id == project_id,
                    ProjectDocument.deleted_at.is_(None),
                )
            ).all()
        else:
            ids = list(dict.fromkeys(scope.document_ids))
        if not ids:
            raise SummaryError("SCOPE_EMPTY", "生成范围没有文献")
        valid = set(
            session.scalars(
                sa.select(ProjectDocument.document_id).where(
                    ProjectDocument.project_id == project_id,
                    ProjectDocument.document_id.in_(ids),
                    ProjectDocument.deleted_at.is_(None),
                )
            ).all()
        )
        invalid = [item for item in ids if item not in valid]
        if invalid:
            raise SummaryError(
                "SCOPE_INVALID_DOCUMENT",
                "文献不属于当前项目：" + ", ".join(invalid),
            )
        return {
            "type": scope.type,
            "documentIds": ids,
            "day": scope.day,
            "excludedDocuments": scope.excluded_documents,
        }

    def complete_generation(
        self, summary_id: str, run_id: str, request: CompleteGenerationRequest
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            summary = self._require_summary(session, summary_id)
            run = session.get(SummaryAIRunRecord, run_id)
            if not run or run.summary_id != summary_id:
                raise SummaryError("REVISION_NOT_FOUND", run_id)
            if run.status not in {"queued", "running", "partial"}:
                raise SummaryError("CANDIDATE_REQUIRED", "AI run 已结束")
            scope = run.scope_json
            documents = self._snapshot_documents(session, scope)
            allowed = self._evidence_catalog(
                session, scope["documentIds"], scope=scope
            )
            sections = self._sections_from_ai(
                session,
                summary,
                request.result,
                allowed,
                run.base_revision_no,
            )
            change_statuses = {
                item["stableKey"]: item["changeStatus"] for item in sections
            }
            if run.mode == "incremental" and run.base_revision_no:
                base = self._revision(session, summary.id, run.base_revision_no)
                self._mark_incremental(sections, base)
            revision = self._store_revision(
                session,
                summary,
                mode=run.mode,
                base=(
                    self._revision(session, summary.id, run.base_revision_no)
                    if run.base_revision_no
                    else None
                ),
                status="candidate",
                source="ai",
                scope=scope,
                title=request.result.title,
                markdown=self._sections_markdown(request.result, sections),
                sections=sections,
                change_summary=request.result.overview_markdown or "AI 阶段总结候选",
                created_by=request.actor.actor_id,
                ai_run_id=run.id,
                legacy_source={},
                legacy_hash="",
                documents=documents,
            )
            run.status = "succeeded"
            run.result_json = request.result.model_dump(by_alias=True)
            run.started_at = run.started_at or now()
            run.finished_at = now()
            run.updated_at = run.finished_at
            events = [
                emit(
                    session,
                    event_type="summary_generated",
                    project_id=summary.project_id,
                    summary_id=summary.id,
                    payload={
                        "runId": run.id,
                        "revisionId": revision.id,
                        "revisionNo": revision.revision_no,
                        "mode": revision.mode,
                        "status": "candidate",
                    },
                ),
                emit(
                    session,
                    event_type="generate_progress",
                    project_id=summary.project_id,
                    summary_id=summary.id,
                    payload={
                        "runId": run.id,
                        "summaryId": summary.id,
                        "status": "succeeded",
                        "stage": "candidate_saved",
                        "completedUnits": len(scope["documentIds"]),
                        "totalUnits": len(scope["documentIds"]),
                        "message": "候选版本已保存，等待确认",
                    },
                ),
            ]
            result = {"run": _run_out(run), "revision": self._revision_full(session, revision), "events": events}
            session.flush()
            return result

    def _snapshot_documents(
        self, session: Session, scope: dict[str, Any]
    ) -> list[dict[str, Any]]:
        excluded = {
            item["documentId"]: item.get("reason", "")
            for item in scope.get("excludedDocuments", [])
        }
        output = []
        for document_id in scope["documentIds"]:
            document = session.get(LibraryDocument, document_id)
            if not document:
                continue
            file = session.scalar(
                sa.select(FileAsset)
                .where(FileAsset.document_id == document_id)
                .order_by(FileAsset.created_at.desc())
            )
            outline_row = session.execute(
                sa.text(
                    """
                    SELECT rr.id, rr.revision_no
                    FROM reading_outlines ro
                    JOIN outline_revisions rr ON rr.id = ro.current_revision_id
                    WHERE ro.document_id = :doc
                    ORDER BY ro.updated_at DESC
                    LIMIT 1
                    """
                ),
                {"doc": document_id},
            ).first()
            annotation_count = session.scalar(
                sa.select(sa.func.count()).select_from(Annotation).where(
                    Annotation.document_id == document_id,
                    Annotation.deleted_at.is_(None),
                )
            )
            output.append(
                {
                    "documentId": document_id,
                    "inclusionStatus": "excluded" if document_id in excluded else "included",
                    "exclusionReason": excluded.get(document_id, ""),
                    "fileSha256": file.sha256 if file else "",
                    "outlineRevisionId": outline_row[0] if outline_row else None,
                    "outlineRevisionNo": outline_row[1] if outline_row else None,
                    "annotationCount": int(annotation_count or 0),
                    "sourceUpdatedAt": document.updated_at,
                }
            )
        return output

    def _evidence_catalog(
        self,
        session: Session,
        document_ids: list[str],
        *,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        start_iso = end_iso = None
        if scope and scope.get("type") == "day" and scope.get("day"):
            zone = ZoneInfo("Asia/Shanghai")
            start = datetime.combine(
                datetime.fromisoformat(scope["day"]).date(),
                time.min,
                tzinfo=zone,
            ).astimezone(timezone.utc)
            end = start + timedelta(days=1)
            start_iso = start.isoformat().replace("+00:00", "Z")
            end_iso = end.isoformat().replace("+00:00", "Z")
        catalog: dict[str, dict[str, Any]] = {}
        annotation_stmt = sa.select(Annotation).where(
                Annotation.document_id.in_(document_ids),
                Annotation.deleted_at.is_(None),
            )
        if start_iso and end_iso:
            annotation_stmt = annotation_stmt.where(
                Annotation.created_at >= start_iso,
                Annotation.created_at < end_iso,
            )
        for annotation in session.scalars(annotation_stmt).all():
            token = f"A:{annotation.id}"
            catalog[token] = {
                "documentId": annotation.document_id,
                "outlineNodeId": None,
                "annotationId": annotation.id,
                "claimId": None,
                "anchor": annotation.selector_json or None,
                "quote": annotation.quote,
                "sourceLabel": annotation.page_label or f"第 {annotation.page_index + 1} 页",
                "evidenceStatus": annotation.anchor_status
                if annotation.anchor_status in {"exact", "partial", "none"}
                else "insufficient",
            }
        node_sql = """
                SELECT n.node_id, n.title, n.anchor_json, rr.outline_id
                FROM outline_revision_nodes n
                JOIN outline_revisions rr ON rr.id = n.revision_id
                JOIN reading_outlines ro ON ro.current_revision_id = rr.id
                WHERE ro.document_id IN :docs
                """
        params: dict[str, Any] = {"docs": document_ids}
        if start_iso and end_iso:
            node_sql += " AND rr.created_at >= :start_iso AND rr.created_at < :end_iso"
            params.update(start_iso=start_iso, end_iso=end_iso)
        for node in session.execute(
            sa.text(node_sql).bindparams(sa.bindparam("docs", expanding=True)),
            params,
        ):
            token = f"O:{node.node_id}"
            catalog[token] = {
                "documentId": None,
                "outlineNodeId": node.node_id,
                "annotationId": None,
                "claimId": None,
                "anchor": node.anchor_json or None,
                "quote": node.title,
                "sourceLabel": node.title,
                "evidenceStatus": "exact" if node.anchor_json else "insufficient",
            }
        # Fill document for outline tokens from their reading outline.
        for key, value in catalog.items():
            if value["documentId"] is None and value["outlineNodeId"]:
                doc = session.scalar(
                    sa.text(
                        """
                        SELECT ro.document_id
                        FROM outline_node_entities ne
                        JOIN reading_outlines ro ON ro.id = ne.outline_id
                        WHERE ne.id=:id
                        """
                    ).bindparams(id=value["outlineNodeId"])
                )
                if doc:
                    value["documentId"] = doc
        return {
            key: value
            for key, value in catalog.items()
            if value["documentId"] in document_ids
        }

    def _sections_from_ai(
        self,
        session: Session,
        summary: ProjectSummary,
        result: SummaryAIResult,
        allowed: dict[str, dict[str, Any]],
        base_no: int | None,
    ) -> list[dict[str, Any]]:
        sections = []
        for order, item in enumerate(result.sections):
            stable_key = str(item.get("stableKey") or item.get("title") or order)
            entity = session.scalar(
                sa.select(SummarySectionEntity).where(
                    SummarySectionEntity.summary_id == summary.id,
                    SummarySectionEntity.stable_key == stable_key,
                )
            )
            if not entity:
                entity = SummarySectionEntity(
                    id=f"sect_{uuid.uuid4().hex}",
                    summary_id=summary.id,
                    stable_key=stable_key,
                    created_at=now(),
                )
                session.add(entity)
                session.flush()
            tokens = list(dict.fromkeys(item.get("evidenceTokens", [])))
            valid = [token for token in tokens if token in allowed]
            evidence = [
                {
                    "id": f"sev_{uuid.uuid4().hex}",
                    "evidenceToken": token,
                    **allowed[token],
                }
                for token in valid
            ]
            status = item.get("changeStatus")
            if not evidence and str(item.get("bodyMarkdown", "")).strip():
                status = "insufficient_evidence"
            sections.append(
                {
                    "id": entity.id,
                    "stableKey": stable_key,
                    "parentSectionId": None,
                    "orderIndex": int(item.get("orderIndex", order)),
                    "title": str(item.get("title") or stable_key),
                    "bodyMarkdown": str(item.get("bodyMarkdown", "")),
                    "changeStatus": status,
                    "contentHash": hash_json(
                        [item.get("title"), item.get("bodyMarkdown"), valid]
                    ),
                    "evidence": evidence,
                    "createdAt": now(),
                    "updatedAt": now(),
                }
            )
        return sections

    def _mark_incremental(
        self, sections: list[dict[str, Any]], base: SummaryRevisionRecord
    ) -> None:
        base_map = {
            item.get("stableKey"): item
            for item in (base.structured_json or {}).get("sections", [])
        }
        current_keys = set()
        for section in sections:
            current_keys.add(section["stableKey"])
            old = base_map.get(section["stableKey"])
            if not old:
                section["changeStatus"] = section.get("changeStatus") or "added"
            elif not section.get("evidence") and section["bodyMarkdown"].strip():
                section["changeStatus"] = "insufficient_evidence"
            elif old.get("contentHash") == section["contentHash"]:
                section["changeStatus"] = "unchanged"
            else:
                section["changeStatus"] = section.get("changeStatus") or "changed"
        for key, old in base_map.items():
            if key not in current_keys:
                sections.append(
                    {
                        **old,
                        "changeStatus": "insufficient_evidence",
                        "evidence": old.get("evidence", []),
                    }
                )

    def _sections_markdown(
        self, result: SummaryAIResult, sections: list[dict[str, Any]]
    ) -> str:
        lines = [f"# {result.title}", "", result.overview_markdown, ""]
        for section in sorted(sections, key=lambda item: item["orderIndex"]):
            lines.extend(
                [
                    f"## {section['title']}",
                    "",
                    section["bodyMarkdown"].strip(),
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    def _store_revision(
        self,
        session: Session,
        summary: ProjectSummary,
        *,
        mode: str,
        base: SummaryRevisionRecord | None,
        status: str,
        source: str,
        scope: dict[str, Any],
        title: str,
        markdown: str,
        sections: list[dict[str, Any]],
        change_summary: str,
        created_by: str,
        ai_run_id: str | None,
        legacy_source: dict[str, Any],
        legacy_hash: str,
        documents: list[dict[str, Any]],
    ) -> SummaryRevisionRecord:
        revision_no = (
            session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(SummaryRevisionRecord.revision_no), 0)).where(
                    SummaryRevisionRecord.summary_id == summary.id
                )
            )
            or 0
        ) + 1
        hash_value = hash_json({"title": title, "markdown": markdown, "sections": sections})
        row = SummaryRevisionRecord(
            id=f"srev_{uuid.uuid4().hex}",
            summary_id=summary.id,
            revision_no=revision_no,
            mode=mode,
            base_revision_id=base.id if base else None,
            base_revision_no=base.revision_no if base else None,
            status=status,
            source=source,
            scope_type=scope["type"],
            scope_json=scope,
            title=title,
            markdown=markdown,
            structured_json={"sections": sections},
            change_summary=change_summary,
            content_hash=hash_value,
            created_by=created_by,
            created_at=now(),
            confirmed_by="",
            confirmed_at=None,
            ai_run_id=ai_run_id,
            legacy_source_json=legacy_source,
            legacy_source_hash=legacy_hash,
        )
        session.add(row)
        session.flush()
        for section in sections:
            entity = session.get(SummarySectionEntity, section["id"])
            if entity is None:
                entity = session.scalar(
                    sa.select(SummarySectionEntity).where(
                        SummarySectionEntity.summary_id == summary.id,
                        SummarySectionEntity.stable_key == section["stableKey"],
                    )
                )
            if entity is None:
                entity = SummarySectionEntity(
                    id=section["id"],
                    summary_id=summary.id,
                    stable_key=section["stableKey"],
                    created_at=now(),
                )
                session.add(entity)
                session.flush()
            section["id"] = entity.id
            session.add(
                SummaryRevisionSection(
                    revision_id=row.id,
                    section_id=section["id"],
                    parent_section_id=section.get("parentSectionId"),
                    stable_key=section["stableKey"],
                    order_index=section["orderIndex"],
                    title=section["title"],
                    body_markdown=section["bodyMarkdown"],
                    change_status=section.get("changeStatus"),
                    base_section_id=None,
                    content_hash=section["contentHash"],
                    created_at=section["createdAt"],
                    updated_at=section["updatedAt"],
                )
            )
        session.flush()
        for section in sections:
            for evidence in section.get("evidence", []):
                evidence_id = evidence["id"]
                if session.get(SummaryEvidenceRecord, evidence_id) is not None:
                    evidence_id = f"sev_{uuid.uuid4().hex}"
                    evidence["id"] = evidence_id
                session.add(
                    SummaryEvidenceRecord(
                        id=evidence_id,
                        revision_id=row.id,
                        section_id=section["id"],
                        evidence_token=evidence["evidenceToken"],
                        document_id=evidence["documentId"],
                        outline_node_id=evidence.get("outlineNodeId"),
                        annotation_id=evidence.get("annotationId"),
                        claim_id=evidence.get("claimId"),
                        anchor_json=evidence.get("anchor") or {},
                        quote=evidence.get("quote", ""),
                        source_label=evidence.get("sourceLabel", ""),
                        evidence_status=evidence.get("evidenceStatus", "insufficient"),
                        created_at=now(),
                    )
                )
        for document in documents:
            session.add(
                SummaryRevisionDocumentRecord(
                    revision_id=row.id,
                    document_id=document["documentId"],
                    inclusion_status=document["inclusionStatus"],
                    exclusion_reason=document["exclusionReason"],
                    file_sha256=document["fileSha256"],
                    outline_revision_id=document["outlineRevisionId"],
                    outline_revision_no=document["outlineRevisionNo"],
                    annotation_count=document["annotationCount"],
                    source_updated_at=document["sourceUpdatedAt"],
                )
            )
        session.add(
            SummaryOperationLog(
                id=f"slog_{uuid.uuid4().hex}",
                summary_id=summary.id,
                revision_id=row.id,
                action="generated" if source == "ai" else "edited",
                actor_id=created_by,
                detail_json={"source": source, "status": status},
                created_at=now(),
            )
        )
        session.flush()
        return row

    # ------------------------------------------------------------------
    # Editing / confirmation / restore
    # ------------------------------------------------------------------

    def update_section(self, command: UpdateSectionCommand) -> dict[str, Any]:
        return self._edit_revision(
            command,
            action="section_update",
            updater=lambda sections: self._update_section_data(sections, command),
        )

    def save_markdown(self, command: SaveMarkdownCommand) -> dict[str, Any]:
        def update(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
            matches = re.finditer(r"^##\s+(.+)$", command.markdown, re.M)
            headings = list(matches)
            output = []
            for index, match in enumerate(headings):
                title = match.group(1).strip()
                end = headings[index + 1].start() if index + 1 < len(headings) else len(command.markdown)
                body = command.markdown[match.end():end].strip()
                stable = re.sub(r"\s+", "-", title.lower())
                old = next((item for item in sections if item["stableKey"] == stable or item["title"] == title), None)
                output.append(
                    {
                        "id": old["id"] if old else f"sect_{uuid.uuid4().hex}",
                        "stableKey": old["stableKey"] if old else stable,
                        "parentSectionId": None,
                        "orderIndex": index,
                        "title": title,
                        "bodyMarkdown": body,
                        "changeStatus": "changed" if old else "added",
                        "contentHash": hash_json([title, body]),
                        "evidence": old.get("evidence", []) if old else [],
                        "createdAt": old["createdAt"] if old else now(),
                        "updatedAt": now(),
                    }
                )
            if not output:
                raise SummaryError("AI_SCHEMA_INVALID", "Markdown 至少需要一个二级标题")
            return output

        return self._edit_revision(command, action="markdown", updater=update)

    def _update_section_data(
        self, sections: list[dict[str, Any]], command: UpdateSectionCommand
    ) -> list[dict[str, Any]]:
        for section in sections:
            if section["id"] == command.section_id:
                if command.title is not None:
                    section["title"] = command.title
                if command.body_markdown is not None:
                    section["bodyMarkdown"] = command.body_markdown
                if command.evidence_ids is not None:
                    section["evidence"] = [
                        item
                        for item in section.get("evidence", [])
                        if item["id"] in command.evidence_ids
                    ]
                section["updatedAt"] = now()
                section["contentHash"] = hash_json(
                    [section["title"], section["bodyMarkdown"], command.evidence_ids]
                )
                section["changeStatus"] = "changed"
                return sections
        raise SummaryError("REVISION_NOT_FOUND", command.section_id)

    def _edit_revision(self, command: Any, action: str, updater: Any) -> dict[str, Any]:
        request_hash = hash_json(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replay = self._receipt(session, command.client_mutation_id, action, request_hash)
            if replay:
                return replay
            summary = self._require_summary(session, command.summary_id)
            self._check_base(summary, command.base_revision_no)
            source = session.get(SummaryRevisionRecord, command.revision_id)
            if not source or source.summary_id != summary.id:
                raise SummaryError("REVISION_NOT_FOUND", command.revision_id)
            sections = copy.deepcopy((source.structured_json or {}).get("sections", []))
            sections = updater(sections)
            markdown = command.markdown if hasattr(command, "markdown") else self._markdown_from_sections(source.title, sections)
            revision = self._store_revision(
                session,
                summary,
                mode=source.mode,
                base=self._revision(session, summary.id, summary.current_revision_no),
                status="candidate",
                source="manual",
                scope=source.scope_json,
                title=source.title,
                markdown=markdown,
                sections=sections,
                change_summary=command.change_summary or "人工编辑总结",
                created_by=command.actor.actor_id,
                ai_run_id=None,
                legacy_source={},
                legacy_hash="",
                documents=[],
            )
            event = emit(
                session,
                event_type="summary_updated",
                project_id=summary.project_id,
                summary_id=summary.id,
                payload={
                    "revisionId": revision.id,
                    "revisionNo": revision.revision_no,
                    "baseRevisionNo": summary.current_revision_no,
                    "source": "manual",
                    "changedSectionIds": [item["id"] for item in sections],
                },
            )
            result = {"revision": self._revision_full(session, revision), "candidate": True, "events": [event]}
            self._save_receipt(session, command.client_mutation_id, action, request_hash, summary.id, result)
            return result

    def _markdown_from_sections(self, title: str, sections: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            [f"# {title}"]
            + [f"## {item['title']}\n\n{item['bodyMarkdown']}" for item in sections]
        )

    def confirm(self, command: ConfirmRevisionCommand) -> dict[str, Any]:
        request_hash = hash_json(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replay = self._receipt(session, command.client_mutation_id, "confirm", request_hash)
            if replay:
                return replay
            summary = self._require_summary(session, command.summary_id)
            self._check_base(summary, command.base_revision_no)
            revision = session.get(SummaryRevisionRecord, command.revision_id)
            if not revision or revision.status != "candidate":
                raise SummaryError("CANDIDATE_REQUIRED", "只能确认候选版本")
            revision.status = "official"
            revision.confirmed_by = command.actor.actor_id
            revision.confirmed_at = now()
            session.flush()
            summary.current_official_revision_id = revision.id
            summary.current_revision_no = revision.revision_no
            summary.updated_at = now()
            event = emit(
                session,
                event_type="summary_confirmed",
                project_id=summary.project_id,
                summary_id=summary.id,
                payload={
                    "revisionId": revision.id,
                    "revisionNo": revision.revision_no,
                    "confirmedBy": command.actor.actor_id,
                },
            )
            result = {"revision": self._revision_full(session, revision), "candidate": False, "events": [event]}
            self._save_receipt(session, command.client_mutation_id, "confirm", request_hash, summary.id, result)
            return result

    def reject(self, command: RejectRevisionCommand) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            summary = self._require_summary(session, command.summary_id)
            self._check_base(summary, command.base_revision_no)
            revision = session.get(SummaryRevisionRecord, command.revision_id)
            if not revision or revision.status != "candidate":
                raise SummaryError("CANDIDATE_REQUIRED", "只能拒绝候选版本")
            revision.status = "rejected"
            session.add(
                SummaryOperationLog(
                    id=f"slog_{uuid.uuid4().hex}",
                    summary_id=summary.id,
                    revision_id=revision.id,
                    action="rejected",
                    actor_id=command.actor.actor_id,
                    detail_json={},
                    created_at=now(),
                )
            )
            return {"revision": self._revision_full(session, revision), "candidate": False, "events": []}

    def restore(self, command: RestoreSummaryRevisionCommand) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            summary = self._require_summary(session, command.summary_id)
            self._check_base(summary, command.base_revision_no)
            source = self._revision(session, summary.id, command.source_revision_no)
            sections = copy.deepcopy(source.structured_json["sections"])
            revision = self._store_revision(
                session,
                summary,
                mode=source.mode,
                base=self._revision(session, summary.id, summary.current_revision_no),
                status="candidate",
                source="rollback",
                scope=source.scope_json,
                title=source.title,
                markdown=source.markdown,
                sections=sections,
                change_summary=command.change_summary or f"回滚到 v{source.revision_no}",
                created_by=command.actor.actor_id,
                ai_run_id=None,
                legacy_source={},
                legacy_hash="",
                documents=[],
            )
            return {"revision": self._revision_full(session, revision), "candidate": True, "events": []}

    def compare(self, summary_id: str, left_no: int, right_no: int) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            left = self._revision(session, summary_id, left_no)
            right = self._revision(session, summary_id, right_no)
            left_map = {item["stableKey"]: item for item in left.structured_json["sections"]}
            right_map = {item["stableKey"]: item for item in right.structured_json["sections"]}
            counts = {key: 0 for key in ("added", "changed", "unchanged", "removed", "insufficient_evidence")}
            diffs = []
            for key in sorted(set(left_map) | set(right_map)):
                before, after = left_map.get(key), right_map.get(key)
                if not before:
                    status = "added"
                elif not after:
                    status = "removed"
                elif before["contentHash"] != after["contentHash"]:
                    status = after.get("changeStatus") or "changed"
                else:
                    status = after.get("changeStatus") or "unchanged"
                if status not in counts:
                    status = "changed"
                counts[status] += 1
                diffs.append(
                    {
                        "stableKey": key,
                        "title": (after or before)["title"],
                        "status": status,
                        "before": before,
                        "after": after,
                        "markdownDiff": "",
                        "evidenceAdded": [],
                        "evidenceRemoved": [],
                    }
                )
            return {
                "summaryId": summary_id,
                "leftRevisionNo": left_no,
                "rightRevisionNo": right_no,
                "sectionDiffs": diffs,
                "counts": counts,
            }

    def filter_sections(
        self, summary_id: str, revision_no: int, query: str = "", status: str = ""
    ) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            revision = self._revision(session, summary_id, revision_no)
            sections = revision.structured_json["sections"]
            result = []
            for section in sections:
                if status and section.get("changeStatus") != status:
                    continue
                if query and query.lower() not in (
                    section["title"] + "\n" + section["bodyMarkdown"]
                ).lower():
                    continue
                result.append(section)
            return result

    def get_run(self, summary_id: str, run_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            run = session.get(SummaryAIRunRecord, run_id)
            if not run or run.summary_id != summary_id:
                raise SummaryError("REVISION_NOT_FOUND", run_id)
            return _run_out(run)

    def generation_state(self, run_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            run = session.get(SummaryAIRunRecord, run_id)
            if not run:
                raise SummaryError("REVISION_NOT_FOUND", run_id)
            return {
                "runId": run.id,
                "summaryId": run.summary_id,
                "status": run.status,
                "stage": "candidate_saved" if run.status == "succeeded" else "scope_resolved",
                "completedUnits": len(run.scope_json.get("documentIds", [])),
                "totalUnits": len(run.scope_json.get("documentIds", [])),
                "message": run.error_message or ("候选版本已保存" if run.status == "succeeded" else "等待生成"),
            }

    def cancel_run(self, summary_id: str, run_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            run = session.get(SummaryAIRunRecord, run_id)
            if not run or run.summary_id != summary_id:
                raise SummaryError("REVISION_NOT_FOUND", run_id)
            if run.status in {"queued", "running", "partial"}:
                run.status = "cancelled"
                run.finished_at = now()
                run.updated_at = now()
            return _run_out(run)

    def events_after(self, after: int = 0, project_id: str | None = None) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            stmt = sa.select(SummaryEventOutbox).where(SummaryEventOutbox.sequence > after)
            if project_id:
                stmt = stmt.where(SummaryEventOutbox.project_id == project_id)
            return [
                out(row)
                for row in session.scalars(
                    stmt.order_by(SummaryEventOutbox.sequence).limit(500)
                ).all()
            ]

    def evidence_clicked(self, summary_id: str, revision_no: int, section_id: str, evidence_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            summary = self._require_summary(session, summary_id)
            revision = self._revision(session, summary_id, revision_no)
            evidence = session.get(SummaryEvidenceRecord, evidence_id)
            if not evidence or evidence.revision_id != revision.id:
                raise SummaryError("REVISION_NOT_FOUND", evidence_id)
            event = emit(
                session,
                event_type="summary_evidence_clicked",
                project_id=summary.project_id,
                summary_id=summary.id,
                payload={
                    "summaryId": summary.id,
                    "revisionNo": revision.revision_no,
                    "sectionId": section_id,
                    "evidence": {
                        "id": evidence.id,
                        "evidenceToken": evidence.evidence_token,
                        "documentId": evidence.document_id,
                        "outlineNodeId": evidence.outline_node_id,
                        "annotationId": evidence.annotation_id,
                        "claimId": evidence.claim_id,
                        "anchor": evidence.anchor_json or None,
                        "quote": evidence.quote,
                        "sourceLabel": evidence.source_label,
                        "evidenceStatus": evidence.evidence_status,
                    },
                },
            )
            return event

    @staticmethod
    def _check_base(summary: ProjectSummary, base: int) -> None:
        if summary.current_revision_no != base:
            raise SummaryError(
                "REVISION_CONFLICT",
                "总结已在其他窗口更新。",
                current={
                    "currentRevisionNo": summary.current_revision_no,
                    "currentOfficialRevisionId": summary.current_official_revision_id,
                },
            )

    def _receipt(self, session: Session, mutation_id: str, action: str, request_hash: str) -> dict[str, Any] | None:
        row = session.get(SummaryMutationReceipt, mutation_id)
        if not row:
            return None
        if row.action != action or row.request_hash != request_hash:
            raise SummaryError("REVISION_CONFLICT", "clientMutationId 已被不同请求使用")
        return copy.deepcopy(row.response_json)

    @staticmethod
    def _save_receipt(session: Session, mutation_id: str, action: str, request_hash: str, summary_id: str, result: dict[str, Any]) -> None:
        session.add(
            SummaryMutationReceipt(
                client_mutation_id=mutation_id,
                action=action,
                request_hash=request_hash,
                summary_id=summary_id,
                response_json=copy.deepcopy(result),
                created_at=now(),
            )
        )
        session.flush()

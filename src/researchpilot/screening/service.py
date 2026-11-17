"""Core P0-02 screening application service."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..config import PATHS
from ..storage.v116.contracts import (
    AuthorInput,
    DocumentCreateInput,
    FileAssetCreateInput,
    MetadataValueInput,
    ProjectDocumentCreateInput,
)
from ..storage.v116.normalization import stable_id
from ..storage.v116.orm import (
    LibraryDocument,
    ProjectDocument,
    now_iso,
)
from ..storage.v116.services import DocumentService, DuplicateMatch
from .ai_service import CandidateScreeningAI, SCREENING_MODEL
from .contracts import (
    BulkCandidateDecisionCommand,
    CandidateListQuery,
    CancelScreeningCommand,
    CandidateMetadata,
    CreateCandidateCommand,
    RequestScreeningCommand,
    RestoreCandidateCommand,
    SubmitCandidateDecisionCommand,
    UpdateCandidateMetadataCommand,
    ai_run_out,
    candidate_file_out,
    candidate_out,
    decision_out,
    duplicate_out,
    evaluation_out,
    operation_log_out,
)
from .duplicates import DuplicateResolver
from .errors import (
    AIError,
    CandidateNotFound,
    CandidateRevisionConflict,
    CandidateStateError,
    CandidateValidationError,
    DuplicateConflict,
    ScreeningError,
    UploadRejected,
)
from .evaluation import compute_evaluation
from .extraction import ExtractionFailure, OCRPort, TesseractOCR, extract_document
from .input_builder import build_screening_input, project_context
from .orm import (
    CandidateAIEvaluation,
    CandidateAIRun,
    CandidateDecision,
    CandidateDuplicateMatch,
    CandidateEvaluationReason,
    CandidateFile,
    CandidateOperationLog,
    ImportBatch,
    ImportCandidate,
)
from .prompts import PROMPT_VERSION
from .storage import QuarantineStore, StoredUpload


def _future(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(payload["updatedAt"]), str(payload["id"])
    except Exception as exc:
        raise CandidateValidationError("无效的列表游标") from exc


def _encode_cursor(candidate: ImportCandidate) -> str:
    raw = json.dumps(
        {"updatedAt": candidate.updated_at, "id": candidate.id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode()


class CandidateService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        quarantine_root: str | Path | None = None,
        ocr: OCRPort | None = None,
        ai: CandidateScreeningAI | None = None,
        lease_seconds: int = 90,
    ) -> None:
        self.factory = factory
        self.store = QuarantineStore(
            quarantine_root or (PATHS.outputs / "quarantine")
        )
        self.ocr = ocr or TesseractOCR()
        self.ai = ai or CandidateScreeningAI()
        self.documents = DocumentService(factory)
        self.duplicates = DuplicateResolver(factory)
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------------ upload

    def create_batch(
        self,
        *,
        project_id: str,
        batch_id: str,
        source_surface: str = "upload",
        created_by: str = "",
        note: str = "",
    ) -> ImportBatch:
        if source_surface not in {"upload", "drop", "clipboard", "api"}:
            raise CandidateValidationError(f"未知上传入口：{source_surface}")
        with self.factory() as session:
            if session.execute(
                sa.text("SELECT 1 FROM projects WHERE id=:id"), {"id": project_id}
            ).first() is None:
                raise CandidateNotFound(f"项目不存在：{project_id}")
            batch = session.get(ImportBatch, batch_id)
            if batch is None:
                batch = ImportBatch(
                    id=batch_id,
                    project_id=project_id,
                    source_surface=source_surface,
                    status="open",
                    created_by=created_by,
                    note=note,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
                session.add(batch)
                session.commit()
            elif batch.project_id != project_id:
                raise CandidateValidationError("批次不属于当前项目")
            session.refresh(batch)
            return batch

    def upload_candidate(
        self,
        *,
        project_id: str,
        batch_id: str,
        file_name: str,
        mime_type: str,
        chunks: Iterable[bytes],
        created_by: str = "",
        client_mutation_id: str,
        source_surface: str = "upload",
    ) -> dict[str, Any]:
        QuarantineStore.classify(file_name, mime_type)
        candidate_id = stable_id("cand", project_id, batch_id, client_mutation_id)
        existing = self._candidate_by_id(candidate_id, required=False)
        if existing is not None:
            return self._create_result(existing.id, auto_screening_queued=False)

        self.create_batch(
            project_id=project_id,
            batch_id=batch_id,
            source_surface=source_surface,
            created_by=created_by,
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            if session.get(ImportCandidate, candidate_id) is not None:
                session.rollback()
                return self._create_result(
                    candidate_id, auto_screening_queued=False
                )
            candidate = ImportCandidate(
                id=candidate_id,
                batch_id=batch_id,
                project_id=project_id,
                status="uploading",
                source_kind="pdf" if Path(file_name).suffix.lower() == ".pdf" else "image",
                display_title=Path(file_name).stem,
                original_filename=Path(file_name).name,
                mime_type=mime_type,
                metadata_json={},
                metadata_provenance_json={},
                metadata_warnings_json=[],
                created_at=now_iso(),
                updated_at=now_iso(),
                revision=1,
            )
            session.add(candidate)
            session.flush()
            batch = session.get(ImportBatch, batch_id)
            if batch is not None:
                batch.status = "processing"
                batch.updated_at = now_iso()
            self._log(
                session,
                candidate,
                "candidate_created",
                None,
                "uploading",
                created_by,
                {"clientMutationId": client_mutation_id},
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)

        try:
            stored = self.store.save_stream(
                project_id=project_id,
                candidate_id=candidate_id,
                file_name=file_name,
                mime_type=mime_type,
                chunks=chunks,
            )
        except UploadRejected as exc:
            self._fail_upload(candidate_id, exc.code, str(exc))
            raise
        except Exception as exc:
            self._fail_upload(candidate_id, "UPLOAD_STORAGE_FAILED", str(exc))
            raise UploadRejected(
                f"文件写入失败：{exc}", code="UPLOAD_STORAGE_FAILED"
            ) from exc

        self._register_source_file(candidate_id, stored, created_by)
        self._precheck_sha(candidate_id, stored.sha256)
        self._process_extraction(candidate_id, created_by)
        return self._create_result(candidate_id, auto_screening_queued=False)

    def create_candidate(self, command: CreateCandidateCommand) -> dict[str, Any]:
        """Register a file already written by an UploadQuarantinePort."""
        candidate_id = stable_id(
            "cand", command.project_id, command.batch_id, command.client_mutation_id
        )
        existing = self._candidate_by_id(candidate_id, required=False)
        if existing is not None:
            return self._create_result(candidate_id, auto_screening_queued=False)
        storage_key = command.upload.storage_key
        path = self.store.path_for(storage_key)
        if not path.is_file():
            raise CandidateValidationError("quarantine 文件不存在")
        digest = _sha256(path)
        size = path.stat().st_size
        if digest != command.upload.sha256:
            raise CandidateValidationError("上传文件 SHA-256 与声明不一致")
        if size != command.upload.size_bytes:
            raise CandidateValidationError("上传文件大小与声明不一致")
        source_kind, mime, _limit = self.store.classify(
            command.file_name, command.mime_type
        )
        self.create_batch(
            project_id=command.project_id,
            batch_id=command.batch_id,
            created_by=command.created_by,
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            if session.get(ImportCandidate, candidate_id) is not None:
                session.rollback()
                return self._create_result(
                    candidate_id, auto_screening_queued=False
                )
            candidate = ImportCandidate(
                id=candidate_id,
                batch_id=command.batch_id,
                project_id=command.project_id,
                status="uploaded",
                source_kind=source_kind,
                display_title=Path(command.file_name).stem,
                original_filename=Path(command.file_name).name,
                mime_type=mime,
                metadata_json={},
                metadata_provenance_json={},
                metadata_warnings_json=[],
                created_at=now_iso(),
                updated_at=now_iso(),
                revision=1,
            )
            session.add(candidate)
            session.flush()
            batch = session.get(ImportBatch, command.batch_id)
            if batch is not None:
                batch.status = "processing"
                batch.updated_at = now_iso()
            session.add(
                CandidateFile(
                    id=stable_id("cf", candidate_id, "source_file", digest),
                    candidate_id=candidate_id,
                    role="source_file",
                    file_name=Path(command.file_name).name,
                    storage_key=storage_key,
                    mime_type=mime,
                    sha256=digest,
                    size_bytes=size,
                    text_layer_status="missing",
                    extraction_status="pending",
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
            )
            self._log(
                session,
                candidate,
                "candidate_created",
                None,
                "uploaded",
                command.created_by,
                {"clientMutationId": command.client_mutation_id},
            )
            session.flush()
            session.commit()
        self._precheck_sha(candidate_id, digest)
        self._process_extraction(candidate_id, command.created_by)
        return self._create_result(candidate_id, auto_screening_queued=False)

    def _register_source_file(
        self, candidate_id: str, stored: StoredUpload, actor_id: str
    ) -> None:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            file_id = stable_id("cf", candidate_id, "source_file", stored.sha256)
            if session.get(CandidateFile, file_id) is None:
                session.add(
                    CandidateFile(
                        id=file_id,
                        candidate_id=candidate_id,
                        role="source_file",
                        file_name=stored.file_name,
                        storage_key=stored.storage_key,
                        mime_type=stored.mime_type,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        text_layer_status="missing",
                        extraction_status="pending",
                        created_at=now_iso(),
                        updated_at=now_iso(),
                    )
                )
            candidate.source_kind = stored.source_kind
            candidate.mime_type = stored.mime_type
            self._transition(
                session, candidate, "uploaded", "upload_completed", actor_id, {}
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)

    def _precheck_sha(self, candidate_id: str, sha256: str) -> None:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            matches = self.documents.find_duplicates_in_session(
                session, sha256=sha256
            )
            if not matches:
                return
            session.execute(
                sa.delete(CandidateDuplicateMatch).where(
                    CandidateDuplicateMatch.candidate_id == candidate_id,
                    CandidateDuplicateMatch.match_basis == "sha256",
                )
            )
            for match in matches:
                if match.match_basis != "sha256":
                    continue
                session.add(
                    CandidateDuplicateMatch(
                        id=stable_id("cdm", candidate_id, match.document.id, "sha256"),
                        candidate_id=candidate_id,
                        document_id=match.document.id,
                        match_basis="sha256",
                        match_status="exact",
                        detail_json={
                            "title": match.document.title,
                            "doi": match.document.doi_key or "",
                        },
                        created_at=now_iso(),
                    )
                )
            candidate.duplicate_status = (
                "conflict" if len(matches) > 1 else "exact_sha256"
            )
            candidate.duplicate_document_id = matches[0].document.id
            candidate.duplicate_basis = "sha256"
            session.commit()

    def _process_extraction(self, candidate_id: str, actor_id: str) -> None:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            files = self._files(session, candidate_id)
            source = next((item for item in files if item.role == "source_file"), None)
            if source is None:
                raise CandidateStateError("候选缺少源文件")
            source.extraction_status = "running"
            source.updated_at = now_iso()
            self._transition(
                session, candidate, "extracting", "extraction_started", actor_id, {}
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)
            path = self.store.path_for(source.storage_key)
            source_kind = candidate.source_kind
            file_name = source.file_name

        try:
            extracted = extract_document(
                path,
                file_name=file_name,
                source_kind=source_kind,
                ocr=self.ocr,
            )
        except ExtractionFailure as exc:
            self._fail_extraction(candidate_id, exc.code, str(exc))
            return
        except Exception as exc:
            self._fail_extraction(
                candidate_id, "INTERNAL_ERROR", f"文本提取异常：{exc}"
            )
            return

        ocr_stored: StoredUpload | None = None
        if extracted.ocr_used and extracted.ocr_pages:
            try:
                ocr_stored = self.store.save_text(
                    project_id=self._candidate_by_id(candidate_id).project_id,
                    candidate_id=candidate_id,
                    file_name=f"{Path(file_name).stem}.ocr.txt",
                    text="\f".join(extracted.ocr_pages),
                )
            except Exception:
                ocr_stored = None

        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            source = next(
                item
                for item in self._files(session, candidate_id)
                if item.role == "source_file"
            )
            source.page_count = extracted.page_count
            source.text_layer_status = extracted.text_layer_status
            source.extraction_status = (
                "done"
                if extracted.text_layer_status == "available"
                else "partial"
                if extracted.text_layer_status == "partial"
                else "failed"
            )
            source.extraction_error = (
                "" if extracted.text_layer_status != "missing" else "PDF_NO_TEXT_LAYER"
            )
            source.updated_at = now_iso()
            candidate.page_count = extracted.page_count
            candidate.language = extracted.language
            candidate.text_layer_status = extracted.text_layer_status
            candidate.metadata_json = extracted.metadata.model_dump(by_alias=True)
            candidate.metadata_provenance_json = extracted.provenance
            candidate.metadata_warnings_json = extracted.warnings
            candidate.fulltext_char_count = extracted.fulltext_char_count
            candidate.display_title = extracted.metadata.title or candidate.display_title
            if extracted.text_layer_status == "missing" and not extracted.ocr_used:
                candidate.error_code = "PDF_NO_TEXT_LAYER"
                candidate.error_message = "未检测到文本层，且 OCR 不可用；当前仅可做元数据级评估"
            if ocr_stored is not None:
                ocr_id = stable_id("cf", candidate_id, "ocr_text", ocr_stored.sha256)
                if session.get(CandidateFile, ocr_id) is None:
                    session.add(
                        CandidateFile(
                            id=ocr_id,
                            candidate_id=candidate_id,
                            role="ocr_text",
                            file_name=ocr_stored.file_name,
                            storage_key=ocr_stored.storage_key,
                            mime_type="text/plain",
                            sha256=ocr_stored.sha256,
                            size_bytes=ocr_stored.size_bytes,
                            page_count=extracted.page_count,
                            text_layer_status="available",
                            extraction_status="done",
                            created_at=now_iso(),
                            updated_at=now_iso(),
                        )
                    )
            self.duplicates.refresh(
                session,
                candidate,
                extracted.metadata,
                sha256=source.sha256,
            )
            self._transition(
                session,
                candidate,
                "ready",
                "extraction_completed",
                actor_id,
                {
                    "textLayerStatus": extracted.text_layer_status,
                    "fulltextCharCount": extracted.fulltext_char_count,
                    "duplicateStatus": candidate.duplicate_status,
                },
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)

    def _refresh_batch_status(self, batch_id: str) -> None:
        with self.factory() as session:
            batch = session.get(ImportBatch, batch_id)
            if batch is None:
                return
            statuses = session.scalars(
                sa.select(ImportCandidate.status).where(
                    ImportCandidate.batch_id == batch_id
                )
            ).all()
            if not statuses:
                return
            if any(
                item in {"uploading", "uploaded", "extracting"}
                for item in statuses
            ):
                batch.status = "processing"
            elif all(item == "extraction_failed" for item in statuses):
                batch.status = "failed"
            else:
                batch.status = "completed"
            batch.updated_at = now_iso()
            session.commit()

    def _fail_upload(self, candidate_id: str, code: str, message: str) -> None:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            candidate.error_code = code
            candidate.error_message = message[:2000]
            self._transition(
                session,
                candidate,
                "extraction_failed",
                "upload_failed",
                "system",
                {"errorCode": code},
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)

    def _fail_extraction(self, candidate_id: str, code: str, message: str) -> None:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            candidate.error_code = code
            candidate.error_message = message[:2000]
            for file in self._files(session, candidate_id):
                if file.role == "source_file":
                    file.extraction_status = "failed"
                    file.extraction_error = message[:1000]
                    file.updated_at = now_iso()
            self._transition(
                session,
                candidate,
                "extraction_failed",
                "extraction_failed",
                "system",
                {"errorCode": code, "message": message[:1000]},
            )
            session.commit()
            self._refresh_batch_status(candidate.batch_id)

    # ------------------------------------------------------------------- read

    def list(self, query: CandidateListQuery) -> dict[str, Any]:
        cursor = _decode_cursor(query.cursor)
        with self.factory() as session:
            conditions = [ImportCandidate.project_id == query.project_id]
            if query.statuses:
                conditions.append(ImportCandidate.status.in_(query.statuses))
            if query.search.strip():
                value = f"%{query.search.strip()}%"
                conditions.append(
                    sa.or_(
                        ImportCandidate.display_title.ilike(value),
                        sa.cast(ImportCandidate.metadata_json, sa.Text).ilike(value),
                    )
                )
            if query.duplicate_only:
                conditions.append(ImportCandidate.duplicate_status != "none")
            if cursor:
                conditions.append(
                    sa.or_(
                        ImportCandidate.updated_at < cursor[0],
                        sa.and_(
                            ImportCandidate.updated_at == cursor[0],
                            ImportCandidate.id < cursor[1],
                        ),
                    )
                )
            priority = sa.case(
                (ImportCandidate.status.in_(["screening_failed", "ready"]), 0),
                (ImportCandidate.status == "screened", 1),
                (ImportCandidate.status == "extraction_failed", 2),
                (
                    ImportCandidate.status.in_(["confirmed", "skipped", "deleted", "expired"]),
                    3,
                ),
                else_=4,
            )
            rows = session.scalars(
                sa.select(ImportCandidate)
                .where(*conditions)
                .order_by(priority, ImportCandidate.updated_at.desc(), ImportCandidate.id.desc())
                .limit(query.limit + 1)
            ).all()
            page = rows[: query.limit]
            counts = dict(
                session.execute(
                    sa.select(ImportCandidate.status, sa.func.count())
                    .where(ImportCandidate.project_id == query.project_id)
                    .group_by(ImportCandidate.status)
                ).all()
            )
            return {
                "items": [candidate_out(item) for item in page],
                "nextCursor": _encode_cursor(page[-1]) if len(rows) > query.limit and page else None,
                "countsByStatus": counts,
            }

    def detail(self, candidate_id: str) -> dict[str, Any]:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            files = self._files(session, candidate_id)
            matches = session.scalars(
                sa.select(CandidateDuplicateMatch)
                .where(CandidateDuplicateMatch.candidate_id == candidate_id)
                .order_by(CandidateDuplicateMatch.created_at.desc())
            ).all()
            latest_run = session.scalar(
                sa.select(CandidateAIRun)
                .where(CandidateAIRun.candidate_id == candidate_id)
                .order_by(CandidateAIRun.attempt_no.desc())
                .limit(1)
            )
            latest_evaluation = session.scalar(
                sa.select(CandidateAIEvaluation)
                .where(CandidateAIEvaluation.candidate_id == candidate_id)
                .order_by(CandidateAIEvaluation.created_at.desc())
                .limit(1)
            )
            reasons = self._evaluation_reasons(session, latest_evaluation.id) if latest_evaluation else []
            latest_decision = session.scalar(
                sa.select(CandidateDecision)
                .where(CandidateDecision.candidate_id == candidate_id)
                .order_by(CandidateDecision.created_at.desc())
                .limit(1)
            )
            logs = session.scalars(
                sa.select(CandidateOperationLog)
                .where(CandidateOperationLog.candidate_id == candidate_id)
                .order_by(CandidateOperationLog.created_at, CandidateOperationLog.id)
            ).all()
            return {
                "candidate": candidate_out(candidate),
                "files": [candidate_file_out(item) for item in files],
                "duplicateMatches": [duplicate_out(item) for item in matches],
                "latestRun": ai_run_out(latest_run) if latest_run else None,
                "latestEvaluation": evaluation_out(latest_evaluation, reasons)
                if latest_evaluation
                else None,
                "latestDecision": decision_out(latest_decision)
                if latest_decision
                else None,
                "operationLogs": [operation_log_out(item) for item in logs],
            }

    # ---------------------------------------------------------------- screening

    def request_screening(self, command: RequestScreeningCommand) -> dict[str, Any]:
        mutation_log_id = stable_id(
            "clog", command.candidate_id, "screening_requested", command.client_mutation_id
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            if session.get(CandidateOperationLog, mutation_log_id) is not None:
                log_row = session.get(CandidateOperationLog, mutation_log_id)
                run_id = str((log_row.detail_json or {}).get("runId") or "")
                run = session.get(CandidateAIRun, run_id) if run_id else None
                if run is not None:
                    return ai_run_out(run)

            candidate = self._require_candidate(session, command.candidate_id)
            self._check_revision(candidate, command.base_revision_no)
            if candidate.status in {"screening_queued", "screening"}:
                raise CandidateStateError("候选正在评估，请等待当前任务完成")
            if candidate.status not in {"ready", "screened", "screening_failed"}:
                raise CandidateStateError(
                    f"当前状态 {candidate.status} 不能发起 AI 初筛"
                )
            if (
                candidate.duplicate_status in {"exact_sha256", "exact_doi"}
                and not command.force
            ):
                raise DuplicateConflict(
                    "已命中正式文献；请直接打开/加入已有文献，或 force=true 重新评估",
                    details={
                        "duplicateDocumentId": candidate.duplicate_document_id,
                        "duplicateStatus": candidate.duplicate_status,
                    },
                )
            files = self._files(session, candidate.id)
            built = build_screening_input(
                candidate=candidate,
                files=files,
                store=self.store,
                project=project_context(session, candidate.project_id),
                ocr=self.ocr,
            )
            attempt = (
                session.scalar(
                    sa.select(sa.func.max(CandidateAIRun.attempt_no)).where(
                        CandidateAIRun.candidate_id == candidate.id
                    )
                )
                or 0
            ) + 1
            run_id = stable_id("crun", candidate.id, str(attempt))
            run = CandidateAIRun(
                id=run_id,
                candidate_id=candidate.id,
                attempt_no=attempt,
                status="queued",
                provider="deepseek",
                model=SCREENING_MODEL,
                prompt_version=PROMPT_VERSION,
                input_hash=built.input_hash,
                input_scope_json=built.input_scope,
                project_context_json=built.input.project_context.model_dump(by_alias=True),
                input_preview_md=built.input_preview_markdown,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            session.add(run)
            session.flush()
            self._transition(
                session,
                candidate,
                "screening_queued",
                "screening_requested",
                command.actor_id,
                {"runId": run_id, "clientMutationId": command.client_mutation_id},
                log_id=mutation_log_id,
            )
            session.commit()
            candidate_input = built.input
            page_texts = built.page_texts

        return self._execute_screening(
            candidate_id=command.candidate_id,
            run_id=run_id,
            candidate_input=candidate_input,
            page_texts=page_texts,
            actor_id=command.actor_id,
        )

    def _execute_screening(
        self,
        *,
        candidate_id: str,
        run_id: str,
        candidate_input: Any,
        page_texts: dict[int, str],
        actor_id: str,
    ) -> dict[str, Any]:
        with self.factory() as session:
            run = session.get(CandidateAIRun, run_id)
            candidate = self._require_candidate(session, candidate_id)
            if run is None:
                raise CandidateNotFound(f"AI run 不存在：{run_id}")
            if run.status in {"cancelled", "succeeded", "failed", "timed_out"}:
                raise CandidateStateError("AI run 已取消")
            if run.status not in {"queued", "running"}:
                raise CandidateStateError(f"AI run 状态不能执行：{run.status}")
            run.status = "running"
            run.started_at = run.started_at or now_iso()
            run.lease_owner = actor_id or "inline-worker"
            run.lease_expires_at = (
                datetime.now() + timedelta(seconds=self.lease_seconds)
            ).isoformat(timespec="seconds")
            run.updated_at = now_iso()
            if candidate.status != "screening":
                self._transition(
                    session,
                    candidate,
                    "screening",
                    "screening_started",
                    actor_id,
                    {"runId": run_id},
                )
            session.commit()

        try:
            ai_response = self.ai.screen(candidate_input, actor_id=actor_id)
        except AIError as exc:
            return self._finish_screening_failure(
                candidate_id, run_id, exc.code, str(exc), actor_id
            )
        except Exception as exc:
            return self._finish_screening_failure(
                candidate_id,
                run_id,
                "INTERNAL_ERROR",
                f"AI 初筛内部异常：{exc}",
                actor_id,
            )

        computed = compute_evaluation(
            candidate_input=candidate_input,
            result=ai_response.result,
            pages=page_texts,
        )
        with self.factory() as session:
            run = session.get(CandidateAIRun, run_id)
            candidate = self._require_candidate(session, candidate_id)
            if run is None or run.status == "cancelled":
                raise CandidateStateError("AI run 已被取消")
            evaluation_id = stable_id("ceval", candidate_id, run_id)
            evaluation = CandidateAIEvaluation(
                id=evaluation_id,
                candidate_id=candidate_id,
                run_id=run_id,
                schema_version=ai_response.result.schema_version,
                topic_match_score=computed.topic_match,
                academic_value_score=computed.academic_value,
                citation_potential_score=computed.citation_potential,
                reading_priority_score=computed.reading_priority,
                read_recommendation=computed.read_recommendation,
                citation_recommendation=computed.citation_recommendation,
                confidence_raw=computed.confidence_raw,
                confidence_adjusted=computed.confidence_adjusted,
                confidence_label=computed.confidence_label,
                input_coverage=computed.input_coverage,
                evidence_status=computed.evidence_status,
                conclusion_summary=ai_response.result.conclusion_summary,
                result_json=computed.result,
                created_at=now_iso(),
            )
            session.add(evaluation)
            for reason in computed.reasons:
                session.add(
                    CandidateEvaluationReason(
                        id=stable_id(
                            "creason",
                            evaluation_id,
                            reason["dimension"],
                            reason["text"],
                        ),
                        evaluation_id=evaluation_id,
                        dimension=reason["dimension"],
                        polarity=reason["polarity"],
                        reason_text=reason["text"],
                        source_kind=reason["source_kind"],
                        evidence_page_index=reason["page_index"],
                        evidence_quote=reason["quote"],
                        match_status=reason["match_status"],
                        created_at=now_iso(),
                    )
                )
            run.status = "succeeded"
            run.provider = ai_response.provider
            run.model = ai_response.model
            run.prompt_version = ai_response.prompt_version
            run.finished_at = now_iso()
            run.elapsed_ms = ai_response.elapsed_ms
            run.usage_json = ai_response.usage
            run.raw_response_json = ai_response.raw_response
            run.updated_at = now_iso()
            candidate.current_evaluation_id = evaluation_id
            candidate.read_recommendation = computed.read_recommendation
            candidate.citation_recommendation = computed.citation_recommendation
            candidate.reading_priority_score = computed.reading_priority
            candidate.confidence_adjusted = computed.confidence_adjusted
            candidate.error_code = ""
            candidate.error_message = ""
            self._transition(
                session,
                candidate,
                "screened",
                "screening_completed",
                actor_id,
                {"runId": run_id, "evaluationId": evaluation_id},
            )
            session.commit()
            return ai_run_out(run)

    def execute_run(self, run_id: str, *, actor_id: str = "worker") -> dict[str, Any]:
        """Resume a claimed/queued run from durable database state."""
        with self.factory() as session:
            run = session.get(CandidateAIRun, run_id)
            if run is None:
                raise CandidateNotFound(f"AI run 不存在：{run_id}")
            candidate = self._require_candidate(session, run.candidate_id)
            if run.status not in {"queued", "running"}:
                return ai_run_out(run)
            files = self._files(session, candidate.id)
            built = build_screening_input(
                candidate=candidate,
                files=files,
                store=self.store,
                project=project_context(session, candidate.project_id),
                ocr=self.ocr,
            )
        return self._execute_screening(
            candidate_id=candidate.id,
            run_id=run_id,
            candidate_input=built.input,
            page_texts=built.page_texts,
            actor_id=actor_id,
        )

    def _finish_screening_failure(
        self,
        candidate_id: str,
        run_id: str,
        code: str,
        message: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self.factory() as session:
            run = session.get(CandidateAIRun, run_id)
            candidate = self._require_candidate(session, candidate_id)
            if run is None:
                raise CandidateNotFound(f"AI run 不存在：{run_id}")
            if run.status == "cancelled":
                return ai_run_out(run)
            run.status = "timed_out" if code == "AI_TIMEOUT" else "failed"
            run.finished_at = now_iso()
            run.error_code = code
            run.error_message = message[:2000]
            run.updated_at = now_iso()
            candidate.error_code = code
            candidate.error_message = message[:2000]
            if not candidate.current_evaluation_id:
                candidate.read_recommendation = "failed"
                candidate.citation_recommendation = "failed"
            # Keep current_evaluation_id and its scores untouched.
            self._transition(
                session,
                candidate,
                "screening_failed",
                "screening_failed",
                actor_id,
                {"runId": run_id, "errorCode": code},
            )
            session.commit()
            return ai_run_out(run)

    def cancel_screening(
        self,
        candidate_id: str,
        *,
        actor_id: str = "",
        base_revision_no: int | None = None,
        client_mutation_id: str = "",
    ) -> dict[str, Any]:
        mutation_log_id = stable_id(
            "clog", candidate_id, "screening_cancelled", client_mutation_id or "anonymous"
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            if client_mutation_id:
                previous = session.get(CandidateOperationLog, mutation_log_id)
                if previous is not None:
                    run_id = str((previous.detail_json or {}).get("runId") or "")
                    existing_run = session.get(CandidateAIRun, run_id) if run_id else None
                    if existing_run is not None:
                        return ai_run_out(existing_run)
            candidate = self._require_candidate(session, candidate_id)
            self._check_revision(candidate, base_revision_no)
            run = session.scalar(
                sa.select(CandidateAIRun)
                .where(
                    CandidateAIRun.candidate_id == candidate_id,
                    CandidateAIRun.status.in_(["queued", "running"]),
                )
                .order_by(CandidateAIRun.created_at.desc())
                .limit(1)
            )
            if run is None:
                raise CandidateStateError("没有可取消的 AI 任务")
            run.status = "cancelled"
            run.finished_at = now_iso()
            run.updated_at = now_iso()
            self._transition(
                session,
                candidate,
                "ready",
                "screening_cancelled",
                actor_id,
                {"runId": run.id, "clientMutationId": client_mutation_id},
                log_id=mutation_log_id if client_mutation_id else None,
            )
            session.commit()
            return ai_run_out(run)

    def cancel(self, command: CancelScreeningCommand) -> dict[str, Any]:
        return self.cancel_screening(
            command.candidate_id,
            actor_id=command.actor_id,
            base_revision_no=command.base_revision_no,
            client_mutation_id=command.client_mutation_id,
        )

    # ---------------------------------------------------------------- decisions

    def submit_decision(
        self,
        command: SubmitCandidateDecisionCommand,
    ) -> dict[str, Any]:
        expected = command.base_revision_no or command.expected_revision
        if command.base_revision_no and command.expected_revision and (
            command.base_revision_no != command.expected_revision
        ):
            raise CandidateValidationError("baseRevisionNo 与 expectedRevision 不一致")
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            existing = session.scalar(
                sa.select(CandidateDecision).where(
                    CandidateDecision.idempotency_key == command.idempotency_key
                )
            )
            if existing is not None:
                candidate = self._require_candidate(session, existing.candidate_id)
                return self._decision_result(session, candidate, existing)
            candidate = self._require_candidate(session, command.candidate_id)
            self._check_revision(candidate, expected)
            if candidate.status == "confirmed":
                raise CandidateStateError("候选已入库")
            if candidate.status not in {
                "ready",
                "screened",
                "screening_failed",
                "extraction_failed",
                "skipped",
                "deleted",
            }:
                raise CandidateStateError(f"当前状态 {candidate.status} 不能提交决策")
            evaluation_id = command.evaluation_id
            if evaluation_id:
                evaluation = session.get(CandidateAIEvaluation, evaluation_id)
                if evaluation is None or evaluation.candidate_id != candidate.id:
                    raise CandidateValidationError("评估不属于当前候选")
            else:
                evaluation_id = candidate.current_evaluation_id

            decision = CandidateDecision(
                id=stable_id("cdecision", candidate.id, command.idempotency_key),
                candidate_id=candidate.id,
                evaluation_id=evaluation_id,
                decision=command.decision,
                read_intent=command.decision == "confirmed_deep_read",
                cite_intent=command.cite_intent,
                note=command.note,
                actor_id=command.actor_id,
                idempotency_key=command.idempotency_key,
                created_at=now_iso(),
            )
            session.add(decision)
            session.flush()

            if command.decision in {"confirmed_store", "confirmed_deep_read"}:
                self._transition(
                    session,
                    candidate,
                    "confirming",
                    "confirmation_started",
                    decision.actor_id,
                    {"decisionId": decision.id},
                )
                try:
                    committed = self._commit_formal_document(
                        session,
                        candidate,
                        command,
                        command.decision,
                    )
                except ScreeningError:
                    raise
                except Exception as exc:
                    raise ScreeningError(
                        f"正式入库失败：{exc}", code="COMMIT_FAILED"
                    ) from exc
                decision.document_id = committed["documentId"]
                decision.project_document_id = committed["projectDocumentId"]
                candidate.confirmed_document_id = committed["documentId"]
                candidate.confirmed_project_document_id = committed["projectDocumentId"]
                to_status = "confirmed"
                route = (
                    f"?project={candidate.project_id}&view=literature&mode="
                    + ("read&document=" if command.decision == "confirmed_deep_read" else "browse&document=")
                    + committed["documentId"]
                )
            elif command.decision == "skipped":
                to_status = "skipped"
                candidate.skipped_until = _future(30)
                route = None
            else:
                to_status = "deleted"
                candidate.deleted_at = now_iso()
                candidate.purge_after = _future(7)
                route = None
            candidate.error_code = ""
            candidate.error_message = ""
            self._transition(
                session,
                candidate,
                to_status,
                "decision_submitted",
                decision.actor_id,
                {
                    "decisionId": decision.id,
                    "decision": command.decision,
                    "clientMutationId": command.client_mutation_id
                    or command.idempotency_key,
                },
            )
            session.commit()
            return self._decision_result(session, candidate, decision, route=route)

    def _commit_formal_document(
        self,
        session: Session,
        candidate: ImportCandidate,
        command: SubmitCandidateDecisionCommand,
        decision: str,
    ) -> dict[str, Any]:
        metadata = CandidateMetadata.model_validate(candidate.metadata_json or {})
        if not metadata.title:
            raise CandidateStateError("标题为空，不能正式入库")
        files = self._files(session, candidate.id)
        source = next((item for item in files if item.role == "source_file"), None)
        if source is None:
            raise CandidateStateError("候选缺少源文件，不能正式入库")
        matches = self.documents.find_duplicates_in_session(
            session,
            doi=metadata.doi,
            title=metadata.title,
            authors=metadata.authors,
            year=metadata.year,
            sha256=source.sha256,
        )
        stored_matches = session.scalars(
            sa.select(CandidateDuplicateMatch).where(
                CandidateDuplicateMatch.candidate_id == candidate.id
            )
        ).all()
        known = {(item.document.id, item.match_basis) for item in matches}
        for stored in stored_matches:
            if (stored.document_id, stored.match_basis) in known:
                continue
            document = session.get(LibraryDocument, stored.document_id)
            if document is not None:
                matches.append(
                    DuplicateMatch(
                        document=document,
                        match_basis=stored.match_basis,
                        match_status=stored.match_status,
                    )
                )
        exact = [
            item for item in matches if item.match_basis in {"doi", "sha256"}
        ]
        possible = [item for item in matches if item.match_basis == "title"]
        if candidate.duplicate_status == "conflict" and command.duplicate_action is None:
            raise DuplicateConflict(
                "候选存在 DOI/文件/标题冲突，请明确选择合并或保留独立",
                details={"matches": [item.as_dict() for item in matches]},
            )
        exact_doi = [item for item in exact if item.match_basis == "doi"]
        if command.duplicate_action == "keep_independent" and exact_doi:
            raise DuplicateConflict("精确重复不能保留为独立文献")
        if command.duplicate_document_id and not any(
            item.document.id == command.duplicate_document_id for item in matches
        ):
            raise CandidateValidationError("指定的重复文献不在本次查重结果中")

        selected = next(
            (
                item
                for item in matches
                if command.duplicate_document_id
                and item.document.id == command.duplicate_document_id
            ),
            exact[0] if exact else (possible[0] if possible else None),
        )
        if possible and not exact and command.duplicate_action is None:
            raise DuplicateConflict(
                "标题疑似重复，请选择合并、添加附件或保留独立",
                details={
                    "matches": [item.as_dict() for item in matches],
                },
            )

        author_models = [
            AuthorInput(literal=author)
            for author in metadata.authors
            if str(author).strip()
        ]
        provenance = candidate.metadata_provenance_json or {}
        metadata_values: list[MetadataValueInput] = []
        for field_name, value in (
            ("title", metadata.title),
            ("authors", metadata.authors_raw or "; ".join(metadata.authors)),
            ("year", metadata.year),
            ("venue", metadata.venue),
            ("doi", metadata.doi),
        ):
            if value:
                metadata_values.append(
                    MetadataValueInput(
                        field_name=field_name,
                        value=value,
                        source=str(
                            (provenance.get(field_name) or {}).get("source") or "legacy"
                        ),
                        confidence=(provenance.get(field_name) or {}).get("confidence"),
                    )
                )
        document_payload = DocumentCreateInput(
            title=metadata.title,
            title_raw=metadata.title_raw or metadata.title,
            title_source=str((provenance.get("title") or {}).get("source") or ""),
            authors=author_models,
            authors_raw=metadata.authors_raw or "; ".join(metadata.authors),
            year=metadata.year,
            venue=metadata.venue,
            abstract=metadata.abstract,
            language=metadata.language,
            entry_type=metadata.entry_type or "other",
            doi=metadata.doi,
            source_urls=metadata.source_urls,
            external_ids=metadata.external_ids,
            metadata_values=metadata_values,
            sha256=source.sha256,
            fallback_legacy_id=f"screening:{candidate.id}",
        )
        if selected is not None and command.duplicate_action != "keep_independent":
            if command.duplicate_action in {None, "merge"}:
                merged = self.documents.create_or_merge_in_session(
                    session, document_payload
                )
                document = session.get(LibraryDocument, merged["id"])
            else:
                document = session.get(LibraryDocument, selected.document.id)
            if document is None:
                raise CandidateStateError("重复文献记录已失效")
        elif command.duplicate_action == "keep_independent" and (possible or exact):
            result = self.documents.create_independent_in_session(
                session,
                document_payload,
                independent_key=candidate.id,
                allow_sha256_collision=bool(exact and not exact_doi),
            )
            document = session.get(LibraryDocument, result["id"])
        else:
            result = self.documents.create_or_merge_in_session(session, document_payload)
            document = session.get(LibraryDocument, result["id"])
        if document is None:
            raise CandidateStateError("正式文献创建/解析失败")

        from ..storage.v116.orm import FileAsset

        existing_main_row = session.scalar(
            sa.select(FileAsset).where(
                FileAsset.document_id == document.id,
                FileAsset.role == "main_pdf",
                FileAsset.deleted_at.is_(None),
            )
        )
        role = (
            "main_pdf"
            if existing_main_row is None or existing_main_row.sha256 == source.sha256
            else "supplement"
        )
        file_out = self.documents.add_file_in_session(
            session,
            FileAssetCreateInput(
                document_id=document.id,
                role=role,
                file_name=source.file_name,
                storage_key=source.storage_key,
                mime_type=source.mime_type,
                sha256=source.sha256,
                size_bytes=source.size_bytes,
                page_count=source.page_count,
                text_layer=source.text_layer_status,
                legacy_payload={
                    "candidateId": candidate.id,
                    "candidateFileId": source.id,
                },
                available_at_migration=True,
            ),
        )
        for auxiliary in files:
            if auxiliary.role == "source_file":
                continue
            aux_role = "ocr_text" if auxiliary.role == "ocr_text" else "screenshot"
            aux = self.documents.add_file_in_session(
                session,
                FileAssetCreateInput(
                    document_id=document.id,
                    role=aux_role,
                    file_name=auxiliary.file_name,
                    storage_key=auxiliary.storage_key,
                    mime_type=auxiliary.mime_type,
                    sha256=auxiliary.sha256,
                    size_bytes=auxiliary.size_bytes,
                    page_count=auxiliary.page_count,
                    text_layer="available"
                    if aux_role == "ocr_text"
                    else auxiliary.text_layer_status,
                    legacy_payload={
                        "candidateId": candidate.id,
                        "candidateFileId": auxiliary.id,
                    },
                ),
            )
            auxiliary.promoted_file_asset_id = aux["id"]
            auxiliary.updated_at = now_iso()
        source.promoted_file_asset_id = file_out["id"]
        source.updated_at = now_iso()

        membership = self.documents.attach_to_project_in_session(
            session,
            ProjectDocumentCreateInput(
                project_id=candidate.project_id,
                document_id=document.id,
                origin="upload" if candidate.source_kind == "pdf" else "screenshot",
                reading_state="to_read" if decision == "confirmed_deep_read" else "not_planned",
                cite_candidate=command.cite_intent,
                added_by=command.idempotency_key,
                legacy_paper_id="",
                legacy_paper_ids=[],
                legacy_payload={"candidateId": candidate.id},
            ),
        )
        project_document = session.get(ProjectDocument, membership["id"])
        if project_document is not None:
            project_document.reading_state = (
                "to_read" if decision == "confirmed_deep_read" else project_document.reading_state
            )
            project_document.cite_candidate = command.cite_intent
            project_document.updated_at = now_iso()
        return {
            "documentId": document.id,
            "projectDocumentId": membership["id"],
            "fileAssetIds": [file_out["id"]],
            "createdDocument": selected is None,
            "createdMembership": True,
            "duplicateMerged": selected is not None,
        }

    def submit_bulk(self, command: BulkCandidateDecisionCommand) -> dict[str, Any]:
        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for candidate_id in command.candidate_ids:
            try:
                current = self._candidate_by_id(candidate_id)
                expected = command.expected_revisions.get(candidate_id, current.revision)
                result = self.submit_decision(
                    SubmitCandidateDecisionCommand(
                        candidateId=candidate_id,
                        expectedRevision=expected,
                        decision=command.decision,
                        citeIntent=command.cite_intent,
                        idempotencyKey=f"{command.idempotency_prefix}:{candidate_id}",
                        duplicateAction=command.duplicate_action,
                        actorId=command.actor_id,
                    )
                )
                succeeded.append(result)
            except ScreeningError as exc:
                failed.append(
                    {"candidateId": candidate_id, "errorCode": exc.code, "message": str(exc)}
                )
        return {"succeeded": succeeded, "failed": failed}

    def restore(self, command: RestoreCandidateCommand) -> dict[str, Any]:
        mutation_log_id = stable_id(
            "clog",
            command.candidate_id,
            "candidate_restored",
            command.client_mutation_id,
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            if session.get(CandidateOperationLog, mutation_log_id) is not None:
                return candidate_out(self._require_candidate(session, command.candidate_id))
            candidate = self._require_candidate(session, command.candidate_id)
            self._check_revision(candidate, command.expected_revision)
            if candidate.status not in {"deleted", "skipped", "expired"}:
                raise CandidateStateError("只有已跳过、已删除或已过期候选可恢复")
            if candidate.status == "deleted" and candidate.purge_after:
                if candidate.purge_after < now_iso():
                    raise CandidateStateError("候选已超过恢复保留期")
            candidate.deleted_at = None
            candidate.skipped_until = None
            candidate.purge_after = None
            self._transition(
                session,
                candidate,
                "ready",
                "candidate_restored",
                command.actor_id,
                {"clientMutationId": command.client_mutation_id},
                log_id=mutation_log_id,
            )
            session.commit()
            return candidate_out(candidate)

    def update_metadata(
        self, command: UpdateCandidateMetadataCommand
    ) -> dict[str, Any]:
        mutation_log_id = stable_id(
            "clog",
            command.candidate_id,
            "metadata_updated",
            command.client_mutation_id,
        )
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            previous = session.get(CandidateOperationLog, mutation_log_id)
            if previous is not None:
                return candidate_out(self._require_candidate(session, command.candidate_id))
            candidate = self._require_candidate(session, command.candidate_id)
            self._check_revision(candidate, command.expected_revision)
            if candidate.status in {"confirmed", "deleted", "expired"}:
                raise CandidateStateError(
                    f"当前状态 {candidate.status} 不能修改元数据"
                )
            if candidate.status in {"screening_queued", "screening", "confirming"}:
                raise CandidateStateError("候选任务运行中，不能修改元数据")
            old = CandidateMetadata.model_validate(candidate.metadata_json or {})
            new = command.metadata
            if new.doi:
                from ..storage.v116.normalization import normalize_doi

                new = new.model_copy(update={"doi": normalize_doi(new.doi)})
            provenance = dict(candidate.metadata_provenance_json or {})
            manual_fields: list[str] = []
            for field_name in (
                "title",
                "authors",
                "year",
                "venue",
                "doi",
                "abstract",
                "language",
            ):
                if getattr(old, field_name) != getattr(new, field_name):
                    provenance[field_name] = {"source": "manual", "confidence": 1.0}
                    manual_fields.append(field_name)
            candidate.metadata_json = new.model_dump(by_alias=True)
            candidate.display_title = new.title or candidate.display_title
            candidate.language = new.language
            candidate.error_code = ""
            candidate.error_message = ""
            source = next(
                (
                    item
                    for item in self._files(session, candidate.id)
                    if item.role == "source_file"
                ),
                None,
            )
            if source is not None:
                self.duplicates.refresh(
                    session,
                    candidate,
                    new,
                    sha256=source.sha256,
                )
            candidate.current_evaluation_id = None
            candidate.read_recommendation = "pending"
            candidate.citation_recommendation = "pending"
            candidate.reading_priority_score = None
            candidate.confidence_adjusted = None
            self._transition(
                session,
                candidate,
                "ready" if new.title else "extraction_failed",
                "metadata_updated",
                command.actor_id,
                {
                    "clientMutationId": command.client_mutation_id,
                    "manualFields": manual_fields,
                },
                log_id=mutation_log_id,
            )
            session.commit()
            return candidate_out(candidate)

    def expire(self, *, now: str | None = None) -> int:
        current = now or now_iso()
        count = 0
        with self.factory() as session:
            rows = session.scalars(
                sa.select(ImportCandidate).where(
                    ImportCandidate.status == "skipped",
                    ImportCandidate.skipped_until.is_not(None),
                    ImportCandidate.skipped_until <= current,
                )
            ).all()
            for candidate in rows:
                self._transition(
                    session,
                    candidate,
                    "expired",
                    "candidate_expired",
                    "system",
                    {},
                )
                count += 1
            session.commit()
        return count

    # ----------------------------------------------------------------- helpers

    def _create_result(self, candidate_id: str, *, auto_screening_queued: bool) -> dict[str, Any]:
        with self.factory() as session:
            candidate = self._require_candidate(session, candidate_id)
            files = self._files(session, candidate_id)
            source = next((item for item in files if item.role == "source_file"), None)
            matches = session.scalars(
                sa.select(CandidateDuplicateMatch).where(
                    CandidateDuplicateMatch.candidate_id == candidate_id
                )
            ).all()
            if source is None:
                raise CandidateStateError("候选缺少源文件")
            return {
                "candidate": candidate_out(candidate),
                "file": candidate_file_out(source),
                "duplicateMatches": [duplicate_out(item) for item in matches],
                "autoScreeningQueued": auto_screening_queued,
            }

    def _decision_result(
        self,
        session: Session,
        candidate: ImportCandidate,
        decision: CandidateDecision,
        *,
        route: str | None = None,
    ) -> dict[str, Any]:
        reading_state = None
        if candidate.confirmed_project_document_id:
            membership = session.get(ProjectDocument, candidate.confirmed_project_document_id)
            reading_state = membership.reading_state if membership else None
        return {
            "candidate": candidate_out(candidate),
            "decision": decision_out(decision),
            "documentId": candidate.confirmed_document_id,
            "projectDocumentId": candidate.confirmed_project_document_id,
            "readingState": reading_state,
            "nextRoute": route,
        }

    def _candidate_by_id(
        self, candidate_id: str, *, required: bool = True
    ) -> ImportCandidate | None:
        with self.factory() as session:
            row = session.get(ImportCandidate, candidate_id)
            if row is None and required:
                raise CandidateNotFound(f"候选不存在：{candidate_id}")
            return row

    @staticmethod
    def _require_candidate(session: Session, candidate_id: str) -> ImportCandidate:
        row = session.get(ImportCandidate, candidate_id)
        if row is None:
            raise CandidateNotFound(f"候选不存在：{candidate_id}")
        return row

    @staticmethod
    def _files(session: Session, candidate_id: str) -> list[CandidateFile]:
        return session.scalars(
            sa.select(CandidateFile)
            .where(
                CandidateFile.candidate_id == candidate_id,
                CandidateFile.deleted_at.is_(None),
            )
            .order_by(CandidateFile.created_at, CandidateFile.id)
        ).all()

    @staticmethod
    def _evaluation_reasons(
        session: Session, evaluation_id: str
    ) -> list[CandidateEvaluationReason]:
        return session.scalars(
            sa.select(CandidateEvaluationReason)
            .where(CandidateEvaluationReason.evaluation_id == evaluation_id)
            .order_by(CandidateEvaluationReason.dimension, CandidateEvaluationReason.created_at)
        ).all()

    @staticmethod
    def _check_revision(candidate: ImportCandidate, expected: int | None) -> None:
        if expected is not None and candidate.revision != expected:
            raise CandidateRevisionConflict(candidate.revision)

    @staticmethod
    def _transition(
        session: Session,
        candidate: ImportCandidate,
        to_status: str,
        action: str,
        actor_id: str,
        detail: dict[str, Any],
        *,
        log_id: str | None = None,
    ) -> None:
        from_status = candidate.status
        candidate.status = to_status
        candidate.revision += 1
        candidate.updated_at = now_iso()
        session.add(
            CandidateOperationLog(
                id=log_id
                or stable_id(
                    "clog",
                    candidate.id,
                    action,
                    from_status or "",
                    to_status,
                    str(time.time_ns()),
                ),
                candidate_id=candidate.id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                detail_json=detail,
                created_at=now_iso(),
            )
        )
        session.flush()

    def _log(
        self,
        session: Session,
        candidate: ImportCandidate,
        action: str,
        from_status: str | None,
        to_status: str,
        actor_id: str,
        detail: dict[str, Any],
    ) -> None:
        session.add(
            CandidateOperationLog(
                id=stable_id(
                    "clog",
                    candidate.id,
                    action,
                    from_status or "",
                    to_status,
                    str(time.time_ns()),
                ),
                candidate_id=candidate.id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                detail_json=detail,
                created_at=now_iso(),
            )
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

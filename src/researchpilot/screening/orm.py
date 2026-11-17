"""SQLAlchemy 2.x models for the P0-02 screening inbox."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..storage.v116.orm import now_iso


def _enum(column: str, *values: str) -> sa.CheckConstraint:
    rendered = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({rendered})")


def _json_valid(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"json_valid({column})")


def _boolean(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN (0, 1)")


class ScreeningBase(DeclarativeBase):
    pass


# Existing P0-01/legacy tables. These stubs only let SQLAlchemy order foreign
# keys; create_all(checkfirst=True) never alters the real tables.
sa.Table("projects", ScreeningBase.metadata, sa.Column("id", sa.Text, primary_key=True))
sa.Table(
    "library_documents",
    ScreeningBase.metadata,
    sa.Column("id", sa.Text, primary_key=True),
)
sa.Table(
    "project_documents",
    ScreeningBase.metadata,
    sa.Column("id", sa.Text, primary_key=True),
)
sa.Table("file_assets", ScreeningBase.metadata, sa.Column("id", sa.Text, primary_key=True))


class ImportBatch(ScreeningBase):
    __tablename__ = "import_batches"
    __table_args__ = (
        sa.Index("ix_import_batches_project_created", "project_id", sa.text("created_at DESC")),
        _enum("source_surface", "upload", "drop", "clipboard", "api"),
        _enum("status", "open", "processing", "completed", "failed"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_surface: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="open")
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    note: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class ImportCandidate(ScreeningBase):
    __tablename__ = "import_candidates"
    __table_args__ = (
        sa.Index(
            "ix_import_candidates_queue",
            "project_id",
            "status",
            sa.text("updated_at DESC"),
        ),
        sa.Index("ix_import_candidates_batch", "batch_id", "created_at"),
        sa.Index(
            "ix_import_candidates_duplicate",
            "duplicate_document_id",
            sqlite_where=sa.text("duplicate_document_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_import_candidates_purge",
            "purge_after",
            sqlite_where=sa.text("deleted_at IS NOT NULL"),
        ),
        _enum(
            "status",
            "uploading",
            "uploaded",
            "extracting",
            "ready",
            "screening_queued",
            "screening",
            "screened",
            "screening_failed",
            "extraction_failed",
            "confirming",
            "confirmed",
            "skipped",
            "deleted",
            "expired",
        ),
        _enum("source_kind", "pdf", "image", "doi", "url", "manual"),
        sa.CheckConstraint("page_count IS NULL OR page_count >= 0"),
        _enum("text_layer_status", "available", "missing", "partial"),
        _json_valid("metadata_json"),
        _json_valid("metadata_provenance_json"),
        _json_valid("metadata_warnings_json"),
        sa.CheckConstraint("fulltext_char_count >= 0"),
        _enum(
            "duplicate_status",
            "none",
            "exact_sha256",
            "exact_doi",
            "possible_title",
            "conflict",
        ),
        _enum(
            "read_recommendation",
            "must_read",
            "recommended",
            "optional",
            "not_recommended",
            "insufficient_evidence",
            "pending",
            "failed",
        ),
        _enum(
            "citation_recommendation",
            "strong_candidate",
            "candidate",
            "optional",
            "not_candidate",
            "insufficient_evidence",
            "pending",
            "failed",
        ),
        sa.CheckConstraint(
            "reading_priority_score IS NULL OR reading_priority_score BETWEEN 0 AND 100"
        ),
        sa.CheckConstraint(
            "confidence_adjusted IS NULL OR confidence_adjusted BETWEEN 0 AND 1"
        ),
        sa.CheckConstraint("revision >= 1"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="uploading")
    source_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    display_title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    original_filename: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    mime_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    language: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    text_layer_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="missing"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    metadata_provenance_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    metadata_warnings_json: Mapped[list[Any]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    fulltext_char_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    duplicate_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="none"
    )
    duplicate_document_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="SET NULL")
    )
    duplicate_basis: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    read_recommendation: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="pending"
    )
    citation_recommendation: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="pending"
    )
    reading_priority_score: Mapped[int | None] = mapped_column(sa.Integer)
    confidence_adjusted: Mapped[float | None] = mapped_column(sa.Float)
    current_evaluation_id: Mapped[str | None] = mapped_column(sa.Text)
    confirmed_document_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="SET NULL")
    )
    confirmed_project_document_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("project_documents.id", ondelete="SET NULL")
    )
    error_code: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    skipped_until: Mapped[str | None] = mapped_column(sa.Text)
    purge_after: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)


class CandidateFile(ScreeningBase):
    __tablename__ = "candidate_files"
    __table_args__ = (
        sa.UniqueConstraint("candidate_id", "role", "sha256"),
        sa.Index("ix_candidate_files_sha", "sha256"),
        _enum("role", "source_file", "ocr_text", "thumbnail"),
        sa.CheckConstraint("size_bytes >= 0"),
        sa.CheckConstraint("page_count IS NULL OR page_count >= 0"),
        _enum("text_layer_status", "available", "missing", "partial"),
        _enum(
            "extraction_status",
            "pending",
            "running",
            "done",
            "partial",
            "failed",
            "not_applicable",
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    file_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    text_layer_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="missing"
    )
    extraction_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="pending"
    )
    extraction_error: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    promoted_file_asset_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("file_assets.id", ondelete="SET NULL")
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)


class CandidateDuplicateMatch(ScreeningBase):
    __tablename__ = "candidate_duplicate_matches"
    __table_args__ = (
        sa.UniqueConstraint("candidate_id", "document_id", "match_basis"),
        sa.Index(
            "ix_candidate_duplicate_matches_candidate",
            "candidate_id",
            "match_status",
        ),
        _enum("match_basis", "sha256", "doi", "title", "manual"),
        _enum("match_status", "exact", "possible", "conflict"),
        sa.CheckConstraint(
            "title_similarity IS NULL OR title_similarity BETWEEN 0 AND 1"
        ),
        _json_valid("detail_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    match_basis: Mapped[str] = mapped_column(sa.Text, nullable=False)
    match_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title_similarity: Mapped[float | None] = mapped_column(sa.Float)
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CandidateAIRun(ScreeningBase):
    __tablename__ = "candidate_ai_runs"
    __table_args__ = (
        sa.UniqueConstraint("candidate_id", "attempt_no"),
        sa.Index("ix_candidate_ai_runs_queue", "status", "created_at"),
        sa.Index("ix_candidate_ai_runs_lease", "status", "lease_expires_at"),
        sa.Index("ix_candidate_ai_runs_candidate", "candidate_id", sa.text("created_at DESC")),
        sa.CheckConstraint("attempt_no >= 1"),
        _enum(
            "status",
            "queued",
            "running",
            "succeeded",
            "failed",
            "timed_out",
            "cancelled",
            "superseded",
        ),
        _json_valid("input_scope_json"),
        _json_valid("project_context_json"),
        _json_valid("usage_json"),
        _json_valid("raw_response_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_scope_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    project_context_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    input_preview_md: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    started_at: Mapped[str | None] = mapped_column(sa.Text)
    finished_at: Mapped[str | None] = mapped_column(sa.Text)
    elapsed_ms: Mapped[int | None] = mapped_column(sa.Integer)
    lease_owner: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    lease_expires_at: Mapped[str | None] = mapped_column(sa.Text)
    error_code: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    usage_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    raw_response_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CandidateAIEvaluation(ScreeningBase):
    __tablename__ = "candidate_ai_evaluations"
    __table_args__ = (
        sa.Index(
            "ix_candidate_ai_evaluations_candidate",
            "candidate_id",
            sa.text("created_at DESC"),
        ),
        sa.CheckConstraint("topic_match_score BETWEEN 0 AND 100"),
        sa.CheckConstraint("academic_value_score BETWEEN 0 AND 100"),
        sa.CheckConstraint("citation_potential_score BETWEEN 0 AND 100"),
        sa.CheckConstraint("reading_priority_score BETWEEN 0 AND 100"),
        _enum(
            "read_recommendation",
            "must_read",
            "recommended",
            "optional",
            "not_recommended",
            "insufficient_evidence",
        ),
        _enum(
            "citation_recommendation",
            "strong_candidate",
            "candidate",
            "optional",
            "not_candidate",
            "insufficient_evidence",
        ),
        sa.CheckConstraint("confidence_raw BETWEEN 0 AND 1"),
        sa.CheckConstraint("confidence_adjusted BETWEEN 0 AND 1"),
        _enum("confidence_label", "high", "medium", "low"),
        _enum("input_coverage", "fulltext", "abstract", "metadata_only"),
        _enum("evidence_status", "verified", "partial", "none"),
        _json_valid("result_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("candidate_ai_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    topic_match_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    academic_value_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    citation_potential_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    reading_priority_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    read_recommendation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    citation_recommendation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence_raw: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence_adjusted: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence_label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_coverage: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    conclusion_summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CandidateEvaluationReason(ScreeningBase):
    __tablename__ = "candidate_evaluation_reasons"
    __table_args__ = (
        sa.Index("ix_candidate_evaluation_reasons_eval", "evaluation_id", "dimension"),
        _enum(
            "dimension",
            "topic_match",
            "academic_value",
            "citation_potential",
            "reading_priority",
        ),
        _enum("polarity", "positive", "negative", "risk", "neutral"),
        _enum("source_kind", "metadata", "abstract", "fulltext"),
        _enum("match_status", "exact", "partial", "none", "no_text_layer"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("candidate_ai_evaluations.id", ondelete="CASCADE"), nullable=False
    )
    dimension: Mapped[str] = mapped_column(sa.Text, nullable=False)
    polarity: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_page_index: Mapped[int | None] = mapped_column(sa.Integer)
    evidence_quote: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    match_status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="none")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CandidateDecision(ScreeningBase):
    __tablename__ = "candidate_decisions"
    __table_args__ = (
        sa.Index("ix_candidate_decisions_candidate_created", "candidate_id", sa.text("created_at DESC")),
        _enum("decision", "confirmed_store", "confirmed_deep_read", "skipped", "deleted"),
        _boolean("read_intent"),
        _boolean("cite_intent"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    evaluation_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("candidate_ai_evaluations.id", ondelete="SET NULL")
    )
    decision: Mapped[str] = mapped_column(sa.Text, nullable=False)
    read_intent: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    cite_intent: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    document_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="SET NULL")
    )
    project_document_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("project_documents.id", ondelete="SET NULL")
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CandidateOperationLog(ScreeningBase):
    __tablename__ = "candidate_operation_logs"
    __table_args__ = (
        sa.Index("ix_candidate_operation_logs_candidate", "candidate_id", "created_at"),
        _json_valid("detail_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        sa.ForeignKey("import_candidates.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(sa.Text)
    to_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

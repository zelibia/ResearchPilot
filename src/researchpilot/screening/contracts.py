"""P0-02 commands, AI schema, and camelCase response serializers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .orm import (
    CandidateAIEvaluation,
    CandidateAIRun,
    CandidateDecision,
    CandidateDuplicateMatch,
    CandidateEvaluationReason,
    CandidateFile,
    CandidateOperationLog,
    ImportCandidate,
)


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(item[:1].upper() + item[1:] for item in rest)


class ScreeningInput(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


CandidateStatus = Literal[
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
]
CandidateSourceKind = Literal["pdf", "image", "doi", "url", "manual"]
DuplicateStatus = Literal[
    "none", "exact_sha256", "exact_doi", "possible_title", "conflict"
]
ReadRecommendation = Literal[
    "must_read",
    "recommended",
    "optional",
    "not_recommended",
    "insufficient_evidence",
    "pending",
    "failed",
]
CitationRecommendation = Literal[
    "strong_candidate",
    "candidate",
    "optional",
    "not_candidate",
    "insufficient_evidence",
    "pending",
    "failed",
]
CandidateDecisionType = Literal[
    "confirmed_store", "confirmed_deep_read", "skipped", "deleted"
]
ScoreDimension = Literal[
    "topic_match", "academic_value", "citation_potential", "reading_priority"
]


class CandidateMetadata(ScreeningInput):
    title: str = ""
    title_raw: str = ""
    authors: list[str] = Field(default_factory=list)
    authors_raw: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    abstract: str = ""
    language: str = ""
    external_ids: dict[str, str] = Field(default_factory=dict)
    source_urls: list[str] = Field(default_factory=list)
    entry_type: str = "other"


class ProjectContext(ScreeningInput):
    project_id: str
    title: str = ""
    domain: str = ""
    description: str = ""
    active_goal: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    target_method_names: list[str] = Field(default_factory=list)
    existing_document_titles: list[str] = Field(default_factory=list)


class FulltextSection(ScreeningInput):
    title: str
    page_start: int = Field(ge=0)
    page_end: int = Field(ge=0)
    text: str


class ScreeningCandidateInput(ScreeningInput):
    candidate_id: str
    project_context: ProjectContext
    metadata: CandidateMetadata
    abstract: str = ""
    fulltext_sections: list[FulltextSection] = Field(default_factory=list)
    input_coverage: Literal["fulltext", "abstract", "metadata_only"] = "metadata_only"


class ScoreValue(ScreeningInput):
    score: int = Field(ge=0, le=100)
    rationale: str = ""


class ScreeningScores(ScreeningInput):
    topic_match: ScoreValue
    academic_value: ScoreValue
    citation_potential: ScoreValue
    reading_priority: ScoreValue


class ScreeningAIReason(ScreeningInput):
    dimension: ScoreDimension
    polarity: Literal["positive", "negative", "risk", "neutral"]
    text: str
    source_kind: Literal["metadata", "abstract", "fulltext"]
    page_index: int | None = Field(default=None, ge=0)
    quote: str = ""


class ScreeningAIResult(ScreeningInput):
    schema_version: Literal["screening.v1"] = "screening.v1"
    conclusion_summary: str
    scores: ScreeningScores
    reasons: list[ScreeningAIReason] = Field(min_length=1, max_length=6)
    model_confidence: float = Field(ge=0, le=1)
    confidence_rationale: str = ""


class ScreeningAIResponse(ScreeningInput):
    result: ScreeningAIResult
    model: str
    provider: str
    prompt_version: str
    elapsed_ms: int = Field(ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] = Field(default_factory=dict)


class CandidateUploadRef(ScreeningInput):
    storage_key: str
    sha256: str
    size_bytes: int = Field(ge=0)


class CreateCandidateCommand(ScreeningInput):
    project_id: str
    batch_id: str
    source_kind: CandidateSourceKind
    file_name: str
    mime_type: str
    upload: CandidateUploadRef
    created_by: str = ""
    client_mutation_id: str
    base_revision_no: int | None = None


class CandidateListQuery(ScreeningInput):
    project_id: str
    statuses: list[CandidateStatus] = Field(default_factory=list)
    search: str = ""
    duplicate_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class RequestScreeningCommand(ScreeningInput):
    candidate_id: str
    force: bool = False
    actor_id: str = ""
    client_mutation_id: str
    base_revision_no: int | None = None


class SubmitCandidateDecisionCommand(ScreeningInput):
    candidate_id: str
    expected_revision: int = Field(default=0, ge=0)
    decision: CandidateDecisionType
    evaluation_id: str | None = None
    cite_intent: bool = False
    note: str = ""
    idempotency_key: str
    client_mutation_id: str | None = None
    base_revision_no: int | None = Field(default=None, ge=1)
    duplicate_action: Literal["merge", "add_attachment", "keep_independent"] | None = None
    duplicate_document_id: str | None = None
    actor_id: str = ""


class BulkCandidateDecisionCommand(ScreeningInput):
    candidate_ids: list[str] = Field(min_length=1, max_length=200)
    decision: CandidateDecisionType
    cite_intent: bool = False
    idempotency_prefix: str
    expected_revisions: dict[str, int] = Field(default_factory=dict)
    duplicate_action: Literal["merge", "add_attachment", "keep_independent"] | None = None
    actor_id: str = ""


class RestoreCandidateCommand(ScreeningInput):
    candidate_id: str
    expected_revision: int = Field(ge=1)
    actor_id: str = ""
    client_mutation_id: str


class UpdateCandidateMetadataCommand(ScreeningInput):
    candidate_id: str
    metadata: CandidateMetadata
    expected_revision: int = Field(ge=1)
    actor_id: str = ""
    client_mutation_id: str


class CancelScreeningCommand(ScreeningInput):
    candidate_id: str
    actor_id: str = ""
    client_mutation_id: str
    base_revision_no: int | None = None


def _dt(value: Any) -> Any:
    return value


def candidate_file_out(model: CandidateFile) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "role": model.role,
        "fileName": model.file_name,
        "storageKey": model.storage_key,
        "mimeType": model.mime_type,
        "sha256": model.sha256,
        "sizeBytes": model.size_bytes,
        "pageCount": model.page_count,
        "textLayerStatus": model.text_layer_status,
        "extractionStatus": model.extraction_status,
        "extractionError": model.extraction_error,
        "promotedFileAssetId": model.promoted_file_asset_id,
        "createdAt": _dt(model.created_at),
        "updatedAt": _dt(model.updated_at),
        "deletedAt": _dt(model.deleted_at),
    }


def candidate_out(model: ImportCandidate) -> dict[str, Any]:
    return {
        "id": model.id,
        "batchId": model.batch_id,
        "projectId": model.project_id,
        "status": model.status,
        "sourceKind": model.source_kind,
        "displayTitle": model.display_title,
        "originalFilename": model.original_filename,
        "mimeType": model.mime_type,
        "pageCount": model.page_count,
        "language": model.language,
        "textLayerStatus": model.text_layer_status,
        "metadata": model.metadata_json,
        "metadataProvenance": model.metadata_provenance_json,
        "metadataWarnings": model.metadata_warnings_json,
        "fulltextCharCount": model.fulltext_char_count,
        "duplicateStatus": model.duplicate_status,
        "duplicateDocumentId": model.duplicate_document_id,
        "duplicateBasis": model.duplicate_basis,
        "readRecommendation": model.read_recommendation,
        "citationRecommendation": model.citation_recommendation,
        "readingPriorityScore": model.reading_priority_score,
        "confidenceAdjusted": model.confidence_adjusted,
        "currentEvaluationId": model.current_evaluation_id,
        "confirmedDocumentId": model.confirmed_document_id,
        "confirmedProjectDocumentId": model.confirmed_project_document_id,
        "errorCode": model.error_code,
        "errorMessage": model.error_message,
        "revision": model.revision,
        "skippedUntil": _dt(model.skipped_until),
        "purgeAfter": _dt(model.purge_after),
        "createdAt": _dt(model.created_at),
        "updatedAt": _dt(model.updated_at),
        "deletedAt": _dt(model.deleted_at),
    }


def duplicate_out(model: CandidateDuplicateMatch) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "documentId": model.document_id,
        "matchBasis": model.match_basis,
        "matchStatus": model.match_status,
        "titleSimilarity": model.title_similarity,
        "detail": model.detail_json,
        "createdAt": _dt(model.created_at),
    }


def ai_run_out(model: CandidateAIRun) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "attemptNo": model.attempt_no,
        "status": model.status,
        "provider": model.provider,
        "model": model.model,
        "promptVersion": model.prompt_version,
        "inputHash": model.input_hash,
        "inputScope": model.input_scope_json,
        "projectContext": model.project_context_json,
        "inputPreviewMarkdown": model.input_preview_md,
        "startedAt": _dt(model.started_at),
        "finishedAt": _dt(model.finished_at),
        "elapsedMs": model.elapsed_ms,
        "leaseOwner": model.lease_owner,
        "leaseExpiresAt": _dt(model.lease_expires_at),
        "errorCode": model.error_code,
        "errorMessage": model.error_message,
        "usage": model.usage_json,
        "rawResponse": model.raw_response_json,
        "createdAt": _dt(model.created_at),
        "updatedAt": _dt(model.updated_at),
    }


def evaluation_reason_out(model: CandidateEvaluationReason) -> dict[str, Any]:
    return {
        "id": model.id,
        "evaluationId": model.evaluation_id,
        "dimension": model.dimension,
        "polarity": model.polarity,
        "reasonText": model.reason_text,
        "sourceKind": model.source_kind,
        "evidencePageIndex": model.evidence_page_index,
        "evidenceQuote": model.evidence_quote,
        "matchStatus": model.match_status,
        "createdAt": _dt(model.created_at),
    }


def evaluation_out(
    model: CandidateAIEvaluation,
    reasons: list[CandidateEvaluationReason] | None = None,
) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "runId": model.run_id,
        "schemaVersion": model.schema_version,
        "topicMatchScore": model.topic_match_score,
        "academicValueScore": model.academic_value_score,
        "citationPotentialScore": model.citation_potential_score,
        "readingPriorityScore": model.reading_priority_score,
        "readRecommendation": model.read_recommendation,
        "citationRecommendation": model.citation_recommendation,
        "confidenceRaw": model.confidence_raw,
        "confidenceAdjusted": model.confidence_adjusted,
        "confidenceLabel": model.confidence_label,
        "inputCoverage": model.input_coverage,
        "evidenceStatus": model.evidence_status,
        "conclusionSummary": model.conclusion_summary,
        "result": model.result_json,
        "reasons": [evaluation_reason_out(item) for item in reasons or []],
        "createdAt": _dt(model.created_at),
    }


def decision_out(model: CandidateDecision) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "evaluationId": model.evaluation_id,
        "decision": model.decision,
        "readIntent": model.read_intent,
        "citeIntent": model.cite_intent,
        "note": model.note,
        "actorId": model.actor_id,
        "idempotencyKey": model.idempotency_key,
        "documentId": model.document_id,
        "projectDocumentId": model.project_document_id,
        "createdAt": _dt(model.created_at),
    }


def operation_log_out(model: CandidateOperationLog) -> dict[str, Any]:
    return {
        "id": model.id,
        "candidateId": model.candidate_id,
        "action": model.action,
        "fromStatus": model.from_status,
        "toStatus": model.to_status,
        "actorId": model.actor_id,
        "detail": model.detail_json,
        "createdAt": _dt(model.created_at),
    }

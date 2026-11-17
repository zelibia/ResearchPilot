"""Input DTOs and camelCase serializers matching the P0-01 TypeScript contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .orm import (
    Annotation,
    DocumentFieldValue,
    FileAsset,
    FileAssetLocation,
    LibraryDocument,
    OutlineNode,
    OutlineNodeEvidence,
    OutlineVersion,
    ProjectDocument,
    SummarySection,
    SummarySectionEvidence,
    SummaryVersion,
    SummaryVersionDocument,
)


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(item[:1].upper() + item[1:] for item in rest)


class ContractInput(BaseModel):
    """Accepts both TypeScript camelCase and Python snake_case field names."""

    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class AuthorInput(ContractInput):
    given: str = ""
    family: str = ""
    literal: str = ""
    sequence: int | None = None
    affiliation: str = ""


class MetadataValueInput(ContractInput):
    field_name: str
    value: Any
    source: str
    source_record_id: str = ""
    confidence: float | None = None
    is_current: bool = True


class DocumentCreateInput(ContractInput):
    id: str | None = None
    title: str = ""
    title_raw: str = ""
    title_source: str = ""
    authors: list[AuthorInput] = Field(default_factory=list)
    authors_raw: str = ""
    year: str = ""
    venue: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    abstract: str = ""
    language: str = ""
    entry_type: str = "other"
    doi: str = ""
    source_urls: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    external_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata_checks: list[Any] = Field(default_factory=list)
    guess_sources: dict[str, str] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)
    verification_status: Literal["unverified", "verified", "partial", "conflict"] = (
        "unverified"
    )
    verified_at: str = ""
    legacy_created_at: str = ""
    fallback_legacy_id: str = ""
    sha256: str = ""
    metadata_values: list[MetadataValueInput] = Field(default_factory=list)


class ProjectDocumentCreateInput(ContractInput):
    project_id: str
    document_id: str
    origin: Literal[
        "upload",
        "screenshot",
        "doi",
        "url",
        "library",
        "citation_candidate",
        "legacy",
    ] = "legacy"
    reading_state: Literal[
        "not_planned",
        "to_read",
        "reading",
        "outline_draft",
        "outline_confirmed",
        "synthesis_ready",
        "archived",
    ] = "not_planned"
    cite_candidate: bool = False
    project_note: str = ""
    added_by: str = ""
    last_read_page_index: int | None = None
    last_read_outline_node_id: str | None = None
    legacy_paper_id: str = ""
    legacy_paper_ids: list[str] = Field(default_factory=list)
    legacy_payload: dict[str, Any] = Field(default_factory=dict)


class FileAssetCreateInput(ContractInput):
    document_id: str
    role: Literal["main_pdf", "supplement", "screenshot", "ocr_text", "web_snapshot"]
    file_name: str
    storage_key: str
    mime_type: str = ""
    sha256: str
    size_bytes: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=0)
    text_layer: Literal["available", "missing", "partial"] = "missing"
    source_url: str = ""
    source_attachment_id: str = ""
    legacy_payload: dict[str, Any] = Field(default_factory=dict)
    legacy_paths: list[str] = Field(default_factory=list)
    available_at_migration: bool = True


class AnnotationCreateInput(ContractInput):
    id: str | None = None
    document_id: str
    file_id: str
    type: Literal["highlight", "underline", "strikeout", "note", "region"]
    quote: str = ""
    comment_markdown: str = ""
    color: str = ""
    page_index: int = Field(ge=0)
    page_label: str = ""
    text_source: Literal["pdf_text", "ocr"] = "pdf_text"
    text_hash: str = ""
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    rects: list[dict[str, float]] = Field(default_factory=list)
    selector: dict[str, Any] = Field(default_factory=dict)
    sort_index: str
    anchor_status: Literal["exact", "partial", "none", "no_text_layer", "out_of_range"] = (
        "exact"
    )
    author_id: str = ""
    source: str = "user"
    read_only: bool = False
    external_id: str = ""
    legacy_payload: dict[str, Any] = Field(default_factory=dict)


class OutlineEvidenceInput(ContractInput):
    kind: Literal["annotation", "page", "outline_node", "document"]
    document_id: str
    annotation_id: str | None = None
    target_outline_node_id: str | None = None
    page_index: int | None = None
    quote: str = ""
    match_status: Literal["exact", "partial", "none", "no_text_layer", "out_of_range"] = (
        "exact"
    )


class OutlineNodeInput(ContractInput):
    id: str | None = None
    parent_id: str | None = None
    order_index: int
    heading_level: int = Field(default=1, ge=1, le=4)
    title: str
    body_markdown: str = ""
    origin: Literal["ai", "user", "mixed"] = "ai"
    active: bool = True
    legacy_source: dict[str, Any] = Field(default_factory=dict)
    evidence: list[OutlineEvidenceInput] = Field(default_factory=list)


class OutlineVersionCreateInput(ContractInput):
    document_id: str
    kind: Literal["reading_outline", "paper_structure"] = "reading_outline"
    version: int = Field(default=1, ge=1)
    status: Literal["ai_draft", "draft", "confirmed", "archived"] = "ai_draft"
    base_version_id: str | None = None
    is_current: bool = True
    markdown: str = ""
    source_run_id: str = ""
    created_by: Literal["ai", "user", "mixed", "legacy"] = "ai"
    legacy_source: dict[str, Any] = Field(default_factory=dict)
    nodes: list[OutlineNodeInput] = Field(default_factory=list)


class SummarySectionEvidenceInput(ContractInput):
    kind: Literal["annotation", "page", "outline_node", "document"]
    document_id: str
    annotation_id: str | None = None
    outline_node_id: str | None = None
    page_index: int | None = None
    quote: str = ""
    match_status: Literal["exact", "partial", "none", "no_text_layer", "out_of_range"] = (
        "exact"
    )


class SummarySectionInput(ContractInput):
    id: str | None = None
    parent_id: str | None = None
    stable_key: str
    order_index: int
    title: str
    body_markdown: str = ""
    change_type: Literal["new", "changed", "unchanged", "removed", "unsupported"] = (
        "unchanged"
    )
    base_section_id: str | None = None
    evidence: list[SummarySectionEvidenceInput] = Field(default_factory=list)


class SummaryVersionCreateInput(ContractInput):
    project_id: str
    version: int = Field(default=1, ge=1)
    mode: Literal["independent", "incremental"] = "independent"
    base_version_id: str | None = None
    status: Literal[
        "draft",
        "queued",
        "running",
        "partial",
        "done",
        "failed",
        "cancelled",
        "final",
    ] = "draft"
    title: str = ""
    markdown: str = ""
    run_id: str = ""
    model: str = ""
    prompt_version: str = ""
    stale: bool = False
    created_by: str = ""
    finalized_at: str | None = None
    legacy_source: dict[str, Any] = Field(default_factory=dict)
    legacy_source_hash: str = ""
    section_evidence: list[SummarySectionInput] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)


def model_dict(obj: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    return {
        _camel(column.name): getattr(obj, column.key)
        for column in obj.__table__.columns
        if column.key not in excluded
    }


def document_out(model: LibraryDocument) -> dict[str, Any]:
    return {
        "id": model.id,
        "identityKey": model.identity_key,
        "doiKey": model.doi_key,
        "titleKey": model.title_key,
        "entryType": model.entry_type,
        "title": model.title,
        "titleRaw": model.title_raw,
        "titleSource": model.title_source,
        "authors": model.authors_json,
        "authorsRaw": model.authors_raw,
        "year": model.year,
        "venue": model.venue,
        "volume": model.volume,
        "issue": model.issue,
        "pages": model.pages,
        "publisher": model.publisher,
        "abstract": model.abstract,
        "language": model.language,
        "sourceUrls": model.source_urls_json,
        "externalIds": model.external_ids_json,
        "externalMetadata": model.external_metadata_json,
        "metadataChecks": model.metadata_checks_json,
        "guessSources": model.guess_sources_json,
        "parseWarnings": model.parse_warnings_json,
        "verificationStatus": model.verification_status,
        "verifiedAt": model.verified_at,
        "legacyCreatedAt": model.legacy_created_at,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
        "deletedAt": model.deleted_at,
    }


def field_value_out(model: DocumentFieldValue) -> dict[str, Any]:
    return {
        "id": model.id,
        "documentId": model.document_id,
        "fieldName": model.field_name,
        "value": model.value_json,
        "valueHash": model.value_hash,
        "source": model.source,
        "sourceRecordId": model.source_record_id,
        "confidence": model.confidence,
        "isCurrent": model.is_current,
        "createdAt": model.created_at,
    }


def project_document_out(model: ProjectDocument) -> dict[str, Any]:
    return {
        "id": model.id,
        "projectId": model.project_id,
        "documentId": model.document_id,
        "origin": model.origin,
        "readingState": model.reading_state,
        "citeCandidate": model.cite_candidate,
        "projectNote": model.project_note,
        "addedBy": model.added_by,
        "addedAt": model.added_at,
        "updatedAt": model.updated_at,
        "lastReadPageIndex": model.last_read_page_index,
        "lastReadOutlineNodeId": model.last_read_outline_node_id,
        "legacyPaperId": model.legacy_paper_id,
        "legacyPaperIds": model.legacy_paper_ids_json,
        "legacyPayload": model.legacy_payload_json,
        "deletedAt": model.deleted_at,
    }


def file_asset_out(model: FileAsset) -> dict[str, Any]:
    return {
        "id": model.id,
        "documentId": model.document_id,
        "role": model.role,
        "fileName": model.file_name,
        "storageKey": model.storage_key,
        "mimeType": model.mime_type,
        "sha256": model.sha256,
        "sizeBytes": model.size_bytes,
        "pageCount": model.page_count,
        "textLayer": model.text_layer,
        "sourceUrl": model.source_url,
        "sourceAttachmentId": model.source_attachment_id,
        "legacyPayload": model.legacy_payload_json,
        "createdAt": model.created_at,
        "deletedAt": model.deleted_at,
    }


def file_location_out(model: FileAssetLocation) -> dict[str, Any]:
    return {
        "id": model.id,
        "fileAssetId": model.file_asset_id,
        "locationType": model.location_type,
        "path": model.path,
        "availableAtMigration": model.available_at_migration,
        "createdAt": model.created_at,
    }


def annotation_out(model: Annotation) -> dict[str, Any]:
    return {
        "id": model.id,
        "documentId": model.document_id,
        "fileId": model.file_id,
        "type": model.type,
        "quote": model.quote,
        "commentMarkdown": model.comment_markdown,
        "color": model.color,
        "pageIndex": model.page_index,
        "pageLabel": model.page_label,
        "textSource": model.text_source,
        "textHash": model.text_hash,
        "charStart": model.char_start,
        "charEnd": model.char_end,
        "rects": model.rects_json,
        "selector": model.selector_json,
        "sortIndex": model.sort_index,
        "anchorStatus": model.anchor_status,
        "authorId": model.author_id,
        "updatedBy": model.updated_by,
        "deletedBy": model.deleted_by,
        "source": model.source,
        "readOnly": model.read_only,
        "externalId": model.external_id,
        "revision": model.revision,
        "legacyPayload": model.legacy_payload_json,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
        "deletedAt": model.deleted_at,
    }


def outline_version_out(model: OutlineVersion) -> dict[str, Any]:
    return {
        "id": model.id,
        "documentId": model.document_id,
        "kind": model.kind,
        "version": model.version,
        "status": model.status,
        "baseVersionId": model.base_version_id,
        "isCurrent": model.is_current,
        "markdown": model.markdown,
        "sourceRunId": model.source_run_id,
        "createdBy": model.created_by,
        "revision": model.revision,
        "legacySource": model.legacy_source_json,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
    }


def outline_node_out(model: OutlineNode) -> dict[str, Any]:
    return {
        "id": model.id,
        "outlineVersionId": model.outline_version_id,
        "parentId": model.parent_id,
        "orderIndex": model.order_index,
        "headingLevel": model.heading_level,
        "title": model.title,
        "bodyMarkdown": model.body_markdown,
        "origin": model.origin,
        "active": model.active,
        "legacySource": model.legacy_source_json,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
    }


def outline_evidence_out(model: OutlineNodeEvidence) -> dict[str, Any]:
    return {
        "id": model.id,
        "ownerOutlineNodeId": model.outline_node_id,
        "kind": model.kind,
        "documentId": model.document_id,
        "annotationId": model.annotation_id,
        "outlineNodeId": model.target_outline_node_id,
        "pageIndex": model.page_index,
        "quote": model.quote,
        "matchStatus": model.match_status,
        "createdAt": model.created_at,
    }


def summary_version_out(model: SummaryVersion) -> dict[str, Any]:
    return {
        "id": model.id,
        "projectId": model.project_id,
        "version": model.version,
        "mode": model.mode,
        "baseVersionId": model.base_version_id,
        "status": model.status,
        "title": model.title,
        "markdown": model.markdown,
        "runId": model.run_id,
        "model": model.model,
        "promptVersion": model.prompt_version,
        "stale": model.stale,
        "createdBy": model.created_by,
        "revision": model.revision,
        "legacySource": model.legacy_source_json,
        "legacySourceHash": model.legacy_source_hash,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
        "finalizedAt": model.finalized_at,
    }


def summary_document_out(model: SummaryVersionDocument) -> dict[str, Any]:
    return {
        "summaryVersionId": model.summary_version_id,
        "documentId": model.document_id,
        "fileSha256": model.file_sha256,
        "outlineVersionId": model.outline_version_id,
        "outlineRevision": model.outline_revision,
        "inclusionStatus": model.inclusion_status,
        "exclusionReason": model.exclusion_reason,
    }


def summary_section_out(model: SummarySection) -> dict[str, Any]:
    return {
        "id": model.id,
        "summaryVersionId": model.summary_version_id,
        "parentId": model.parent_id,
        "stableKey": model.stable_key,
        "orderIndex": model.order_index,
        "title": model.title,
        "bodyMarkdown": model.body_markdown,
        "changeType": model.change_type,
        "baseSectionId": model.base_section_id,
        "revision": model.revision,
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
    }


def summary_evidence_out(model: SummarySectionEvidence) -> dict[str, Any]:
    return {
        "id": model.id,
        "summarySectionId": model.summary_section_id,
        "kind": model.kind,
        "documentId": model.document_id,
        "annotationId": model.annotation_id,
        "outlineNodeId": model.outline_node_id,
        "pageIndex": model.page_index,
        "quote": model.quote,
        "matchStatus": model.match_status,
        "createdAt": model.created_at,
    }

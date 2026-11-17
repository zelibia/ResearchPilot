"""Base services for the v1.16 P0-01 data model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from .contracts import (
    AnnotationCreateInput,
    DocumentCreateInput,
    FileAssetCreateInput,
    MetadataValueInput,
    OutlineVersionCreateInput,
    ProjectDocumentCreateInput,
    SummaryVersionCreateInput,
    annotation_out,
    document_out,
    field_value_out,
    file_asset_out,
    file_location_out,
    outline_evidence_out,
    outline_node_out,
    outline_version_out,
    project_document_out,
    summary_document_out,
    summary_evidence_out,
    summary_section_out,
    summary_version_out,
)
from .db import session_scope
from .normalization import (
    DocumentCandidate,
    document_identity_key,
    first_author_compatible,
    first_author_family,
    normalize_doi,
    normalize_title,
    stable_id,
    title_key,
    title_quality,
    year_compatible,
)
from .orm import (
    Annotation,
    DocumentFieldValue,
    FileAsset,
    FileAssetLocation,
    LibraryDocument,
    MigrationIdMap,
    OutlineNode,
    OutlineNodeEvidence,
    OutlineVersion,
    ProjectDocument,
    SummarySection,
    SummarySectionEvidence,
    SummaryVersion,
    SummaryVersionDocument,
    now_iso,
)


class V116Error(RuntimeError):
    code = "V116_ERROR"


class NotFoundError(V116Error):
    code = "NOT_FOUND"


class ValidationError(V116Error):
    code = "VALIDATION_ERROR"


class ConflictError(V116Error):
    code = "CONFLICT"


class RevisionConflictError(ConflictError):
    code = "REVISION_CONFLICT"

    def __init__(self, current_revision: int, message: str = "revision conflict") -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class DuplicateMatch:
    document: LibraryDocument
    match_basis: str
    match_status: str
    conflicting_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "documentId": self.document.id,
            "matchBasis": self.match_basis,
            "matchStatus": self.match_status,
            "conflictingFields": list(self.conflicting_fields),
        }


def _require(session: Session, model: type[Any], object_id: str) -> Any:
    value = session.get(model, object_id)
    if value is None:
        raise NotFoundError(f"{model.__name__} not found: {object_id}")
    return value


def _session_factory_or_default(factory: sessionmaker[Session] | None) -> sessionmaker[Session]:
    if factory is None:
        raise ValueError("session_factory is required")
    return factory


def _normalize_metadata_source(source: str) -> str:
    return {
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
    }.get(source, "legacy")


class DocumentService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def get(self, document_id: str) -> dict[str, Any] | None:
        with session_scope(self.factory) as session:
            document = session.get(LibraryDocument, document_id)
            return document_out(document) if document else None

    def list_project_documents(self, project_id: str) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                sa.select(ProjectDocument)
                .where(ProjectDocument.project_id == project_id)
                .order_by(ProjectDocument.updated_at.desc(), ProjectDocument.id)
            ).all()
            return [project_document_out(item) for item in rows]

    def find_duplicates(
        self,
        *,
        doi: str = "",
        title: str = "",
        authors: list[Any] | str | None = None,
        year: str = "",
        sha256: str = "",
    ) -> list[DuplicateMatch]:
        with session_scope(self.factory) as session:
            return self.find_duplicates_in_session(
                session, doi=doi, title=title, authors=authors, year=year, sha256=sha256
            )

    def find_duplicates_in_session(
        self,
        session: Session,
        *,
        doi: str = "",
        title: str = "",
        authors: list[Any] | str | None = None,
        year: str = "",
        sha256: str = "",
    ) -> list[DuplicateMatch]:
        """Connection-scoped duplicate lookup for wider transactions."""
        return self._find_duplicates(
            session, doi=doi, title=title, authors=authors, year=year, sha256=sha256
        )

    def duplicate_resolution(
        self,
        *,
        doi: str = "",
        title: str = "",
        authors: list[Any] | str | None = None,
        year: str = "",
        sha256: str = "",
    ) -> dict[str, Any]:
        matches = self.find_duplicates(
            doi=doi, title=title, authors=authors, year=year, sha256=sha256
        )
        if not matches:
            action = "create_new"
        elif len(matches) > 1:
            action = "review"
        elif any(item.match_basis == "sha256" for item in matches):
            action = "add_attachment"
        else:
            action = "link_existing"
        return {
            "matches": [item.as_dict() for item in matches],
            "recommendedAction": action,
        }

    def _find_duplicates(
        self,
        session: Session,
        *,
        doi: str = "",
        title: str = "",
        authors: list[Any] | str | None = None,
        year: str = "",
        sha256: str = "",
    ) -> list[DuplicateMatch]:
        matches: list[DuplicateMatch] = []
        doi_key = normalize_doi(doi)
        if doi_key:
            docs = session.scalars(
                sa.select(LibraryDocument).where(
                    LibraryDocument.doi_key == doi_key,
                    LibraryDocument.deleted_at.is_(None),
                )
            ).all()
            matches.extend(DuplicateMatch(item, "doi", "exact") for item in docs)

        normalized_title_key = title_key(title)
        if normalized_title_key and title_quality(title):
            rows = session.scalars(
                sa.select(LibraryDocument).where(
                    LibraryDocument.title_key == normalized_title_key,
                    LibraryDocument.deleted_at.is_(None),
                )
            ).all()
            for item in rows:
                conflicting = tuple(
                    field
                    for field, ok in (
                        ("doi", not doi_key or not item.doi_key or doi_key == item.doi_key),
                        ("year", year_compatible(year, item.year)),
                        ("authors", first_author_compatible(authors, item.authors_json)),
                    )
                    if not ok
                )
                if conflicting:
                    continue
                if not any(match.document.id == item.id for match in matches):
                    matches.append(DuplicateMatch(item, "title", "exact"))

        if sha256:
            docs = session.scalars(
                sa.select(LibraryDocument)
                .join(FileAsset, FileAsset.document_id == LibraryDocument.id)
                .where(
                    FileAsset.sha256 == sha256.lower(),
                    FileAsset.deleted_at.is_(None),
                    LibraryDocument.deleted_at.is_(None),
                )
            ).all()
            for item in docs:
                if not any(match.document.id == item.id for match in matches):
                    matches.append(DuplicateMatch(item, "sha256", "exact"))
        return matches

    def create_or_merge(self, payload: DocumentCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            return self.create_or_merge_in_session(session, payload)

    def create_or_merge_in_session(
        self, session: Session, payload: DocumentCreateInput
    ) -> dict[str, Any]:
        matches = self.find_duplicates_in_session(
            session,
            doi=payload.doi,
            title=payload.title,
            authors=payload.authors,
            year=payload.year,
            sha256=payload.sha256,
        )
        if len({item.document.id for item in matches}) > 1:
            raise ConflictError(
                "multiple canonical documents matched; resolve the DOI/title conflict explicitly"
            )
        if matches:
            document = matches[0].document
            self._fill_empty_metadata(document, payload)
            self._write_field_values(session, document.id, payload)
            document.updated_at = now_iso()
            session.flush()
            return document_out(document)

        identity = document_identity_key(
            doi=payload.doi,
            title=payload.title,
            sha256=payload.sha256,
            fallback_id=payload.fallback_legacy_id,
        )
        return self._create_new_in_session(
            session,
            payload,
            identity_key=identity,
        )

    def create_independent_in_session(
        self,
        session: Session,
        payload: DocumentCreateInput,
        *,
        independent_key: str,
        allow_sha256_collision: bool = False,
    ) -> dict[str, Any]:
        """Create a reviewed independent record without fuzzy merging.

        The caller must have already resolved any exact DOI/SHA duplicate. The
        independent key keeps ``identity_key`` unique while exact title
        collisions remain available for later review.
        """
        matches = self.find_duplicates_in_session(
            session,
            doi=payload.doi,
            title=payload.title,
            authors=payload.authors,
            year=payload.year,
            sha256=payload.sha256,
        )
        blocking = {"doi"} if allow_sha256_collision else {"doi", "sha256"}
        if any(match.match_basis in blocking for match in matches):
            raise ConflictError("exact duplicate cannot be kept as an independent document")
        return self._create_new_in_session(
            session,
            payload,
            identity_key=f"review:{independent_key}",
        )

    def _create_new_in_session(
        self,
        session: Session,
        payload: DocumentCreateInput,
        *,
        identity_key: str,
    ) -> dict[str, Any]:
        document_id = payload.id or stable_id("doc", identity_key)
        existing = session.get(LibraryDocument, document_id)
        if existing is not None:
            return document_out(existing)

        document = LibraryDocument(
            id=document_id,
            identity_key=identity_key,
            doi_key=normalize_doi(payload.doi) or None,
            title_key=title_key(payload.title) or None,
            entry_type=payload.entry_type,
            title=payload.title,
            title_raw=payload.title_raw or payload.title,
            title_source=payload.title_source,
            authors_json=[item.model_dump() for item in payload.authors],
            authors_raw=payload.authors_raw,
            year=payload.year,
            venue=payload.venue,
            volume=payload.volume,
            issue=payload.issue,
            pages=payload.pages,
            publisher=payload.publisher,
            abstract=payload.abstract,
            language=payload.language,
            source_urls_json=list(payload.source_urls),
            external_ids_json=dict(payload.external_ids),
            external_metadata_json=dict(payload.external_metadata),
            metadata_checks_json=list(payload.metadata_checks),
            guess_sources_json=dict(payload.guess_sources),
            parse_warnings_json=list(payload.parse_warnings),
            verification_status=payload.verification_status,
            verified_at=payload.verified_at,
            legacy_created_at=payload.legacy_created_at,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        if payload.doi:
            document.external_ids_json.setdefault("doi", payload.doi)
        session.add(document)
        session.flush()
        self._write_field_values(session, document.id, payload)
        return document_out(document)

    def _fill_empty_metadata(self, document: LibraryDocument, payload: DocumentCreateInput) -> None:
        for attr, value in {
            "title": payload.title,
            "title_raw": payload.title_raw,
            "title_source": payload.title_source,
            "authors_raw": payload.authors_raw,
            "year": payload.year,
            "venue": payload.venue,
            "volume": payload.volume,
            "issue": payload.issue,
            "pages": payload.pages,
            "publisher": payload.publisher,
            "abstract": payload.abstract,
            "language": payload.language,
            "verified_at": payload.verified_at,
        }.items():
            if not getattr(document, attr) and value:
                setattr(document, attr, value)
        if not document.doi_key and payload.doi:
            document.doi_key = normalize_doi(payload.doi) or None
        if not document.title_key and payload.title:
            document.title_key = title_key(payload.title) or None
        if not document.authors_json and payload.authors:
            document.authors_json = [item.model_dump() for item in payload.authors]
        for key, value in payload.external_ids.items():
            document.external_ids_json.setdefault(key, value)

    def _write_field_values(
        self, session: Session, document_id: str, payload: DocumentCreateInput
    ) -> None:
        rows = list(payload.metadata_values)
        if payload.title and not any(
            item.field_name == "title" and item.is_current for item in rows
        ):
            rows.append(
                type(payload.metadata_values[0])(
                    field_name="title",
                    value=payload.title,
                    source=payload.title_source or "legacy",
                )
                if payload.metadata_values
                else _field_value_model("title", payload.title, payload.title_source or "legacy")
            )
        normalized_title = normalize_title(payload.title)
        if normalized_title:
            rows.append(_field_value_model("__normalized_title__", normalized_title, "legacy"))
        seen: set[tuple[str, str, str]] = set()
        for item in rows:
            import hashlib

            source = _normalize_metadata_source(item.source)
            value_hash = hashlib.sha256(
                str(item.value).encode("utf-8")
                if not isinstance(item.value, (dict, list))
                else json.dumps(item.value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            key = (item.field_name, value_hash, source)
            if key in seen:
                continue
            seen.add(key)
            existing = session.scalar(
                sa.select(DocumentFieldValue).where(
                    DocumentFieldValue.document_id == document_id,
                    DocumentFieldValue.field_name == item.field_name,
                    DocumentFieldValue.value_hash == value_hash,
                    DocumentFieldValue.source == source,
                )
            )
            if existing is None:
                if item.is_current:
                    session.execute(
                        sa.update(DocumentFieldValue)
                        .where(
                            DocumentFieldValue.document_id == document_id,
                            DocumentFieldValue.field_name == item.field_name,
                            DocumentFieldValue.is_current.is_(True),
                        )
                        .values(is_current=False)
                    )
                session.add(
                    DocumentFieldValue(
                        id=stable_id("dfv", document_id, item.field_name, value_hash, source),
                        document_id=document_id,
                        field_name=item.field_name,
                        value_json=item.value,
                        value_hash=value_hash,
                        source=source,
                        source_record_id=item.source_record_id,
                        confidence=item.confidence,
                        is_current=item.is_current,
                        created_at=now_iso(),
                    )
                )
        session.flush()

    def attach_to_project(self, payload: ProjectDocumentCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            return self.attach_to_project_in_session(session, payload)

    def attach_to_project_in_session(
        self, session: Session, payload: ProjectDocumentCreateInput
    ) -> dict[str, Any]:
        _require(session, LibraryDocument, payload.document_id)
        project_exists = session.execute(
            sa.text("SELECT 1 FROM projects WHERE id = :id"),
            {"id": payload.project_id},
        ).first()
        if not project_exists:
            raise NotFoundError(f"Project not found: {payload.project_id}")
        existing = session.scalar(
            sa.select(ProjectDocument).where(
                ProjectDocument.project_id == payload.project_id,
                ProjectDocument.document_id == payload.document_id,
            )
        )
        if existing is not None:
            if payload.legacy_paper_id:
                existing.legacy_paper_ids_json = sorted(
                    set(existing.legacy_paper_ids_json) | {payload.legacy_paper_id}
                )
            existing.updated_at = now_iso()
            session.flush()
            return project_document_out(existing)

        row = ProjectDocument(
            id=stable_id("pd", payload.project_id, payload.document_id),
            project_id=payload.project_id,
            document_id=payload.document_id,
            origin=payload.origin,
            reading_state=payload.reading_state,
            cite_candidate=payload.cite_candidate,
            project_note=payload.project_note,
            added_by=payload.added_by,
            added_at=now_iso(),
            updated_at=now_iso(),
            last_read_page_index=payload.last_read_page_index,
            last_read_outline_node_id=payload.last_read_outline_node_id,
            legacy_paper_id=payload.legacy_paper_id,
            legacy_paper_ids_json=payload.legacy_paper_ids
            or ([payload.legacy_paper_id] if payload.legacy_paper_id else []),
            legacy_payload_json=payload.legacy_payload,
        )
        session.add(row)
        session.flush()
        return project_document_out(row)

    def add_file(self, payload: FileAssetCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            return self.add_file_in_session(session, payload)

    def add_file_in_session(
        self, session: Session, payload: FileAssetCreateInput
    ) -> dict[str, Any]:
        _require(session, LibraryDocument, payload.document_id)
        if not payload.sha256.strip():
            raise ValidationError("file sha256 is required")
        if len(payload.sha256) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in payload.sha256
        ):
            raise ValidationError("file sha256 must be a 64-character hex digest")
        if Path(payload.storage_key).is_absolute() or (
            len(payload.storage_key) > 1 and payload.storage_key[1] == ":"
        ):
            raise ValidationError("file storage_key must be a controlled relative key")
        existing = session.scalar(
            sa.select(FileAsset).where(
                FileAsset.document_id == payload.document_id,
                FileAsset.role == payload.role,
                FileAsset.sha256 == payload.sha256,
            )
        )
        if existing is None:
            existing = FileAsset(
                id=stable_id("file", payload.document_id, payload.role, payload.sha256),
                document_id=payload.document_id,
                role=payload.role,
                file_name=payload.file_name,
                storage_key=payload.storage_key,
                mime_type=payload.mime_type,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                page_count=payload.page_count,
                text_layer=payload.text_layer,
                source_url=payload.source_url,
                source_attachment_id=payload.source_attachment_id,
                legacy_payload_json=payload.legacy_payload,
                created_at=now_iso(),
            )
            session.add(existing)
            session.flush()
        else:
            existing.file_name = existing.file_name or payload.file_name
            existing.size_bytes = existing.size_bytes or payload.size_bytes
            existing.page_count = existing.page_count or payload.page_count
            existing.text_layer = payload.text_layer or existing.text_layer
        for path in payload.legacy_paths:
            location_id = stable_id("fal", existing.id, path)
            if session.get(FileAssetLocation, location_id) is None:
                session.add(
                    FileAssetLocation(
                        id=location_id,
                        file_asset_id=existing.id,
                        location_type="legacy",
                        path=path,
                        available_at_migration=(
                            payload.available_at_migration and Path(path).is_file()
                        ),
                        created_at=now_iso(),
                    )
                )
        session.flush()
        return file_asset_out(existing)

    def resolve_legacy_paper(self, legacy_paper_id: str) -> dict[str, Any] | None:
        with session_scope(self.factory) as session:
            row = session.scalar(
                sa.select(ProjectDocument).where(
                    ProjectDocument.legacy_paper_id == legacy_paper_id
                )
            )
            if row is not None:
                return project_document_out(row)
            mapping = session.scalar(
                sa.select(MigrationIdMap)
                .where(
                    MigrationIdMap.entity_type == "project_document",
                    MigrationIdMap.legacy_id == legacy_paper_id,
                )
                .order_by(MigrationIdMap.created_at.desc())
            )
            if mapping is None:
                return None
            mapped = session.get(ProjectDocument, mapping.new_id)
            return project_document_out(mapped) if mapped else None


def _field_value_model(field_name: str, value: Any, source: str) -> MetadataValueInput:
    return MetadataValueInput(field_name=field_name, value=value, source=source)


class AnnotationService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def list_for_file(self, document_id: str, file_id: str) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                sa.select(Annotation)
                .where(
                    Annotation.document_id == document_id,
                    Annotation.file_id == file_id,
                    Annotation.deleted_at.is_(None),
                )
                .order_by(Annotation.page_index, Annotation.sort_index)
            ).all()
            return [annotation_out(item) for item in rows]

    def create(self, payload: AnnotationCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            annotation = self.create_in_session(session, payload)
            return {"annotation": annotation, "revision": annotation["revision"]}

    def create_in_session(
        self, session: Session, payload: AnnotationCreateInput
    ) -> dict[str, Any]:
        if payload.color and not (
            len(payload.color) == 7
            and payload.color.startswith("#")
            and all(char in "0123456789abcdefABCDEF" for char in payload.color[1:])
        ):
            raise ValidationError("annotation color must be #RRGGBB")
        if payload.char_start is not None and payload.char_end is not None:
            if payload.char_end < payload.char_start:
                raise ValidationError("char_end must be >= char_start")
        file = _require(session, FileAsset, payload.file_id)
        if file.document_id != payload.document_id:
            raise ValidationError("annotation file does not belong to document")
        row = Annotation(
            id=payload.id or stable_id(
                "ann",
                payload.document_id,
                payload.file_id,
                payload.page_index,
                payload.sort_index,
                payload.quote,
            ),
            document_id=payload.document_id,
            file_id=payload.file_id,
            type=payload.type,
            quote=payload.quote,
            comment_markdown=payload.comment_markdown,
            color=payload.color,
            page_index=payload.page_index,
            page_label=payload.page_label,
            text_source=payload.text_source,
            text_hash=payload.text_hash,
            char_start=payload.char_start,
            char_end=payload.char_end,
            rects_json=list(payload.rects),
            selector_json=dict(payload.selector),
            sort_index=payload.sort_index,
            anchor_status=payload.anchor_status,
            author_id=payload.author_id,
            updated_by=payload.author_id,
            source=payload.source,
            read_only=payload.read_only,
            external_id=payload.external_id,
            revision=1,
            legacy_payload_json=payload.legacy_payload,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session.add(row)
        session.flush()
        return annotation_out(row)

    def update_comment(
        self,
        annotation_id: str,
        *,
        comment_markdown: str,
        base_revision: int,
        updated_by: str,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = _require(session, Annotation, annotation_id)
            self._check_revision(row, base_revision)
            if row.read_only:
                raise ValidationError("annotation is read-only")
            row.comment_markdown = comment_markdown
            row.updated_by = updated_by
            row.updated_at = now_iso()
            row.revision += 1
            session.flush()
            annotation = annotation_out(row)
            return {"annotation": annotation, "revision": annotation["revision"]}

    def update_style(
        self,
        annotation_id: str,
        *,
        base_revision: int,
        updated_by: str,
        color: str | None = None,
        type: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = _require(session, Annotation, annotation_id)
            self._check_revision(row, base_revision)
            if row.read_only:
                raise ValidationError("annotation is read-only")
            if color is not None:
                if not (
                    len(color) == 7
                    and color.startswith("#")
                    and all(char in "0123456789abcdefABCDEF" for char in color[1:])
                ):
                    raise ValidationError("annotation color must be #RRGGBB")
                row.color = color
            if type is not None:
                if type not in {"highlight", "underline", "strikeout"}:
                    raise ValidationError("invalid text annotation type")
                row.type = type
            row.updated_by = updated_by
            row.updated_at = now_iso()
            row.revision += 1
            session.flush()
            annotation = annotation_out(row)
            return {"annotation": annotation, "revision": annotation["revision"]}

    def delete(
        self,
        annotation_id: str,
        *,
        base_revision: int,
        deleted_by: str,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = _require(session, Annotation, annotation_id)
            self._check_revision(row, base_revision)
            if row.read_only:
                raise ValidationError("annotation is read-only")
            row.deleted_at = now_iso()
            row.deleted_by = deleted_by
            row.updated_by = deleted_by
            row.updated_at = row.deleted_at
            row.revision += 1
            session.flush()
            annotation = annotation_out(row)
            return {"annotation": annotation, "revision": annotation["revision"]}

    def restore(
        self,
        annotation_id: str,
        *,
        base_revision: int,
        updated_by: str,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = _require(session, Annotation, annotation_id)
            self._check_revision(row, base_revision)
            row.deleted_at = None
            row.deleted_by = ""
            row.updated_by = updated_by
            row.updated_at = now_iso()
            row.revision += 1
            session.flush()
            annotation = annotation_out(row)
            return {"annotation": annotation, "revision": annotation["revision"]}

    @staticmethod
    def _check_revision(row: Annotation, base_revision: int) -> None:
        if row.revision != base_revision:
            raise RevisionConflictError(row.revision)


class OutlineService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def list_versions(self, document_id: str, kind: str = "reading_outline") -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                sa.select(OutlineVersion)
                .where(
                    OutlineVersion.document_id == document_id,
                    OutlineVersion.kind == kind,
                )
                .order_by(OutlineVersion.version.desc())
            ).all()
            return [outline_version_out(item) for item in rows]

    def create_version(self, payload: OutlineVersionCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            return self.create_version_in_session(session, payload)

    def create_version_in_session(
        self, session: Session, payload: OutlineVersionCreateInput
    ) -> dict[str, Any]:
        _require(session, LibraryDocument, payload.document_id)
        if payload.version < 1:
            raise ValidationError("outline version must be >= 1")
        if payload.base_version_id:
            base = _require(session, OutlineVersion, payload.base_version_id)
            if base.document_id != payload.document_id or base.kind != payload.kind:
                raise ValidationError("base outline version belongs to another document or kind")
        if session.scalar(
            sa.select(OutlineVersion).where(
                OutlineVersion.document_id == payload.document_id,
                OutlineVersion.kind == payload.kind,
                OutlineVersion.version == payload.version,
            )
        ):
            raise ConflictError("outline version already exists")
        if payload.is_current:
            previous = session.scalars(
                sa.select(OutlineVersion).where(
                    OutlineVersion.document_id == payload.document_id,
                    OutlineVersion.kind == payload.kind,
                    OutlineVersion.is_current.is_(True),
                )
            ).all()
            for row in previous:
                row.is_current = False
        version = OutlineVersion(
            id=stable_id("outv", payload.document_id, payload.kind, str(payload.version)),
            document_id=payload.document_id,
            kind=payload.kind,
            version=payload.version,
            status=payload.status,
            base_version_id=payload.base_version_id,
            is_current=payload.is_current,
            markdown=payload.markdown,
            source_run_id=payload.source_run_id,
            created_by=payload.created_by,
            revision=1,
            legacy_source_json=payload.legacy_source,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session.add(version)
        session.flush()
        pending = list(payload.nodes)
        id_by_old: dict[str, str] = {}
        for item in pending:
            old_key = item.id or item.title
            if old_key in id_by_old:
                raise ValidationError(f"duplicate outline node key: {old_key}")
            id_by_old[old_key] = stable_id(
                "outn", version.id, str(item.order_index), item.title
            )
        if len({item.order_index for item in pending if item.parent_id is None}) != len(
            [item for item in pending if item.parent_id is None]
        ):
            raise ValidationError("outline root nodes require unique order_index values")
        pending_by_key = {item.id or item.title: item for item in pending}
        for item in pending:
            if item.parent_id and item.parent_id not in id_by_old:
                raise ValidationError(f"outline parent not found: {item.parent_id}")
        ordered: list[Any] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise ValidationError("outline parent relationship contains a cycle")
            visiting.add(key)
            item = pending_by_key[key]
            if item.parent_id:
                visit(item.parent_id)
            visiting.remove(key)
            visited.add(key)
            ordered.append(item)

        for item in pending:
            visit(item.id or item.title)

        for item in ordered:
            node_id = id_by_old[item.id or item.title]
            parent_id = id_by_old.get(item.parent_id) if item.parent_id else None
            node = OutlineNode(
                id=node_id,
                outline_version_id=version.id,
                parent_id=parent_id,
                order_index=item.order_index,
                heading_level=item.heading_level,
                title=item.title,
                body_markdown=item.body_markdown,
                origin=item.origin,
                active=item.active,
                legacy_source_json=item.legacy_source,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            session.add(node)
            session.flush()
            for evidence in item.evidence:
                if evidence.annotation_id:
                    annotation = _require(session, Annotation, evidence.annotation_id)
                    if annotation.document_id != evidence.document_id:
                        raise ValidationError(
                            "outline evidence annotation belongs to another document"
                        )
                target_id = (
                    id_by_old.get(evidence.target_outline_node_id, evidence.target_outline_node_id)
                    if evidence.target_outline_node_id
                    else None
                )
                session.add(
                    OutlineNodeEvidence(
                        id=stable_id(
                            "outev", node.id, evidence.kind, evidence.document_id,
                            evidence.annotation_id or "",
                            evidence.target_outline_node_id or "",
                            str(evidence.page_index or ""),
                        ),
                        outline_node_id=node.id,
                        kind=evidence.kind,
                        document_id=evidence.document_id,
                        annotation_id=evidence.annotation_id,
                        target_outline_node_id=target_id,
                        page_index=evidence.page_index,
                        quote=evidence.quote,
                        match_status=evidence.match_status,
                        created_at=now_iso(),
                    )
                )
        session.flush()
        return outline_version_out(version)

    def current(self, document_id: str, kind: str = "reading_outline") -> dict[str, Any] | None:
        with session_scope(self.factory) as session:
            version = session.scalar(
                sa.select(OutlineVersion).where(
                    OutlineVersion.document_id == document_id,
                    OutlineVersion.kind == kind,
                    OutlineVersion.is_current.is_(True),
                )
            )
            if version is None:
                return None
            nodes = session.scalars(
                sa.select(OutlineNode)
                .where(OutlineNode.outline_version_id == version.id)
                .order_by(OutlineNode.order_index)
            ).all()
            evidence = session.scalars(
                sa.select(OutlineNodeEvidence)
                .where(OutlineNodeEvidence.outline_node_id.in_([node.id for node in nodes]))
            ).all() if nodes else []
            history = session.scalars(
                sa.select(OutlineVersion)
                .where(
                    OutlineVersion.document_id == document_id,
                    OutlineVersion.kind == kind,
                )
                .order_by(OutlineVersion.version.desc())
            ).all()
            return {
                "current": outline_version_out(version),
                "nodes": [outline_node_out(node) for node in nodes],
                "evidence": [outline_evidence_out(item) for item in evidence],
                "versionHistory": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "version": item.version,
                        "status": item.status,
                        "createdBy": item.created_by,
                        "createdAt": item.created_at,
                        "updatedAt": item.updated_at,
                    }
                    for item in history
                ],
            }


class SummaryService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            rows = session.scalars(
                sa.select(SummaryVersion)
                .where(SummaryVersion.project_id == project_id)
                .order_by(SummaryVersion.version.desc())
            ).all()
            return [summary_version_out(item) for item in rows]

    def bootstrap(self, summary_version_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            version = _require(session, SummaryVersion, summary_version_id)
            documents = session.scalars(
                sa.select(SummaryVersionDocument)
                .where(SummaryVersionDocument.summary_version_id == version.id)
                .order_by(SummaryVersionDocument.document_id)
            ).all()
            sections = session.scalars(
                sa.select(SummarySection)
                .where(SummarySection.summary_version_id == version.id)
                .order_by(SummarySection.order_index, SummarySection.id)
            ).all()
            evidence = session.scalars(
                sa.select(SummarySectionEvidence)
                .where(
                    SummarySectionEvidence.summary_section_id.in_(
                        [section.id for section in sections]
                    )
                )
                .order_by(SummarySectionEvidence.created_at)
            ).all() if sections else []
            return {
                "version": summary_version_out(version),
                "documents": [summary_document_out(item) for item in documents],
                "sections": [summary_section_out(item) for item in sections],
                "evidence": [summary_evidence_out(item) for item in evidence],
            }

    def create_version(self, payload: SummaryVersionCreateInput) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            return self.create_version_in_session(session, payload)

    def create_version_in_session(
        self, session: Session, payload: SummaryVersionCreateInput
    ) -> dict[str, Any]:
        if payload.version < 1:
            raise ValidationError("summary version must be >= 1")
        project_exists = session.execute(
            sa.text("SELECT 1 FROM projects WHERE id = :id"),
            {"id": payload.project_id},
        ).first()
        if not project_exists:
            raise NotFoundError(f"Project not found: {payload.project_id}")
        if payload.base_version_id:
            base = _require(session, SummaryVersion, payload.base_version_id)
            if base.project_id != payload.project_id:
                raise ValidationError("base summary version belongs to another project")
        if session.scalar(
            sa.select(SummaryVersion).where(
                SummaryVersion.project_id == payload.project_id,
                SummaryVersion.version == payload.version,
            )
        ):
            raise ConflictError("summary version already exists")
        row = SummaryVersion(
            id=stable_id("sum", payload.project_id, str(payload.version)),
            project_id=payload.project_id,
            version=payload.version,
            mode=payload.mode,
            base_version_id=payload.base_version_id,
            status=payload.status,
            title=payload.title,
            markdown=payload.markdown,
            run_id=payload.run_id,
            model=payload.model,
            prompt_version=payload.prompt_version,
            stale=payload.stale,
            created_by=payload.created_by,
            revision=1,
            legacy_source_json=payload.legacy_source,
            legacy_source_hash=payload.legacy_source_hash,
            created_at=now_iso(),
            updated_at=now_iso(),
            finalized_at=payload.finalized_at,
        )
        session.add(row)
        session.flush()
        for document_id in dict.fromkeys(payload.documents):
            _require(session, LibraryDocument, document_id)
            session.add(
                SummaryVersionDocument(
                    summary_version_id=row.id,
                    document_id=document_id,
                    inclusion_status="included",
                )
            )
        section_ids: dict[str, str] = {}
        for item in payload.section_evidence:
            old_key = item.id or item.stable_key
            if old_key in section_ids:
                raise ValidationError(f"duplicate summary section key: {old_key}")
            section_ids[old_key] = stable_id(
                "sumsec", row.id, item.stable_key
            )
        pending_by_key = {
            item.id or item.stable_key: item for item in payload.section_evidence
        }
        for item in payload.section_evidence:
            if item.parent_id and item.parent_id not in section_ids:
                raise ValidationError(f"summary parent not found: {item.parent_id}")
        ordered: list[Any] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise ValidationError("summary parent relationship contains a cycle")
            visiting.add(key)
            item = pending_by_key[key]
            if item.parent_id:
                visit(item.parent_id)
            visiting.remove(key)
            visited.add(key)
            ordered.append(item)

        for item in payload.section_evidence:
            visit(item.id or item.stable_key)

        for item in ordered:
            section_id = section_ids[item.id or item.stable_key]
            section = SummarySection(
                id=section_id,
                summary_version_id=row.id,
                parent_id=section_ids.get(item.parent_id) if item.parent_id else None,
                stable_key=item.stable_key,
                order_index=item.order_index,
                title=item.title,
                body_markdown=item.body_markdown,
                change_type=item.change_type,
                base_section_id=item.base_section_id,
                revision=1,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            session.add(section)
            session.flush()
            for evidence in item.evidence:
                session.add(
                    SummarySectionEvidence(
                        id=stable_id(
                            "sumev", section.id, evidence.kind, evidence.document_id,
                            evidence.annotation_id or "",
                            evidence.outline_node_id or "",
                            str(evidence.page_index or ""),
                        ),
                        summary_section_id=section.id,
                        kind=evidence.kind,
                        document_id=evidence.document_id,
                        annotation_id=evidence.annotation_id,
                        outline_node_id=evidence.outline_node_id,
                        page_index=evidence.page_index,
                        quote=evidence.quote,
                        match_status=evidence.match_status,
                        created_at=now_iso(),
                    )
                )
        session.flush()
        return summary_version_out(row)


class V116Services:
    """Service facade used by upper layers."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.documents = DocumentService(factory)
        self.annotations = AnnotationService(factory)
        self.outlines = OutlineService(factory)
        self.summaries = SummaryService(factory)
        self.factory = factory

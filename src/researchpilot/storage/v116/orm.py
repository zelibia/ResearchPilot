"""SQLAlchemy 2.x ORM for the v1.16 P0-01 global literature model.

The legacy ``researchpilot.storage.repository`` API is intentionally untouched.
This module is a side-by-side model used by the v1.16 services and migration
engine. The existing ``papers`` table remains the compatibility source of truth
until an explicit cutover.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _enum(column: str, *values: str) -> sa.CheckConstraint:
    rendered = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({rendered})")


def _json_valid(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"json_valid({column})")


def _boolean(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN (0, 1)")


class Base(DeclarativeBase):
    pass


# ``projects`` is owned by the legacy repository. Registering a minimal table
# object lets SQLAlchemy order v1.16 foreign keys without creating or altering
# the legacy table (``create_all(checkfirst=True)`` leaves it untouched).
legacy_projects = sa.Table(
    "projects",
    Base.metadata,
    sa.Column("id", sa.Text, primary_key=True),
)


class MigrationRun(Base):
    __tablename__ = "migration_runs"
    __table_args__ = (
        sa.Index("ix_migration_runs_status_started", "status", sa.text("started_at DESC")),
        _enum(
            "status",
            "pending",
            "preflight",
            "backed_up",
            "schema_created",
            "documents_built",
            "memberships_built",
            "files_built",
            "derived_backfilled",
            "validated",
            "cutover_ready",
            "committed",
            "failed",
        ),
        _json_valid("source_counts_json"),
        _json_valid("result_counts_json"),
        _json_valid("error_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    schema_from: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    schema_to: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    started_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(sa.Text)
    backup_path: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    backup_sha256: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_counts_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    result_counts_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    error_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    note: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


class MigrationIdMap(Base):
    __tablename__ = "migration_id_map"
    __table_args__ = (
        sa.Index("ix_migration_id_map_new", "entity_type", "new_id"),
        _enum("match_basis", "sha256", "doi", "title", "legacy_id", "embedded", "manual_override"),
    )

    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("migration_runs.id", ondelete="CASCADE"), primary_key=True
    )
    entity_type: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    legacy_table: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    legacy_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    new_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    match_basis: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False)


class MigrationConflict(Base):
    __tablename__ = "migration_conflicts"
    __table_args__ = (
        sa.Index(
            "ix_migration_conflicts_run_resolution",
            "run_id",
            "resolution",
            "kind",
        ),
        _enum(
            "kind",
            "doi_title_mismatch",
            "same_file_different_doi",
            "title_collision",
            "ambiguous_title_match",
            "metadata_conflict",
            "missing_file",
            "invalid_outline",
            "invalid_annotation",
            "unknown_legacy_shape",
        ),
        _json_valid("legacy_ids_json"),
        _json_valid("document_ids_json"),
        _json_valid("detail_json"),
        _enum("resolution", "pending", "auto_resolved", "manual_resolved", "kept_separate"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("migration_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    legacy_ids_json: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    document_ids_json: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    detail_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    resolution: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    resolved_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    resolved_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False)


class MigrationUnmappedRecord(Base):
    __tablename__ = "migration_unmapped_records"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "legacy_table", "legacy_id"),
        _json_valid("payload_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("migration_runs.id", ondelete="CASCADE"), nullable=False
    )
    legacy_table: Mapped[str] = mapped_column(sa.Text, nullable=False)
    legacy_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_json: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False)


class LibraryDocument(Base):
    __tablename__ = "library_documents"
    __table_args__ = (
        sa.UniqueConstraint("identity_key", name="uq_library_documents_identity_key"),
        sa.Index(
            "ux_library_documents_doi_key",
            "doi_key",
            unique=True,
            sqlite_where=sa.text("doi_key IS NOT NULL AND doi_key <> ''"),
        ),
        sa.Index(
            "ix_library_documents_title_key",
            "title_key",
            sqlite_where=sa.text("title_key IS NOT NULL AND title_key <> ''"),
        ),
        sa.Index("ix_library_documents_year", "year"),
        _json_valid("authors_json"),
        _json_valid("source_urls_json"),
        _json_valid("external_ids_json"),
        _json_valid("external_metadata_json"),
        _json_valid("metadata_checks_json"),
        _json_valid("guess_sources_json"),
        _json_valid("parse_warnings_json"),
        _enum("verification_status", "unverified", "verified", "partial", "conflict"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    identity_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    doi_key: Mapped[str | None] = mapped_column(sa.Text)
    title_key: Mapped[str | None] = mapped_column(sa.Text)
    entry_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="other")
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    title_raw: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    title_source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    authors_json: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    authors_raw: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    year: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    venue: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    volume: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    issue: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    pages: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    publisher: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    abstract: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_urls_json: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    external_ids_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    external_metadata_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    metadata_checks_json: Mapped[list[Any]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    guess_sources_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    parse_warnings_json: Mapped[list[Any]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    verification_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="unverified"
    )
    verified_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    legacy_created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)

    field_values: Mapped[list[DocumentFieldValue]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    project_links: Mapped[list[ProjectDocument]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    files: Mapped[list[FileAsset]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[Annotation]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        overlaps="annotations,file_asset",
    )
    outline_versions: Mapped[list[OutlineVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentFieldValue(Base):
    __tablename__ = "document_field_values"
    __table_args__ = (
        sa.UniqueConstraint(
            "document_id", "field_name", "value_hash", "source",
            name="uq_document_field_value",
        ),
        sa.Index(
            "ix_document_field_values_current",
            "document_id",
            "field_name",
            "is_current",
        ),
        _json_valid("value_json"),
        _enum(
            "source",
            "pdf_embedded",
            "first_page",
            "filename",
            "body",
            "crossref",
            "openalex",
            "ocr",
            "manual",
            "legacy",
        ),
        _boolean("is_current"),
        sa.Index(
            "ux_document_field_values_one_current",
            "document_id",
            "field_name",
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    value_json: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    value_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

    document: Mapped[LibraryDocument] = relationship(back_populates="field_values")


class ProjectDocument(Base):
    __tablename__ = "project_documents"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "document_id", name="uq_project_document"),
        sa.Index(
            "ux_project_documents_legacy_paper",
            "legacy_paper_id",
            unique=True,
            sqlite_where=sa.text("legacy_paper_id <> ''"),
        ),
        sa.Index(
            "ix_project_documents_project_state",
            "project_id",
            "reading_state",
            sa.text("updated_at DESC"),
        ),
        sa.Index("ix_project_documents_document", "document_id"),
        _enum(
            "origin",
            "upload",
            "screenshot",
            "doi",
            "url",
            "library",
            "citation_candidate",
            "legacy",
        ),
        _enum(
            "reading_state",
            "not_planned",
            "to_read",
            "reading",
            "outline_draft",
            "outline_confirmed",
            "synthesis_ready",
            "archived",
        ),
        _boolean("cite_candidate"),
        sa.CheckConstraint(
            "last_read_page_index IS NULL OR last_read_page_index >= 0"
        ),
        _json_valid("legacy_paper_ids_json"),
        _json_valid("legacy_payload_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), nullable=False
    )
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reading_state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="not_planned"
    )
    cite_candidate: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    project_note: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    added_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    added_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    last_read_page_index: Mapped[int | None] = mapped_column(sa.Integer)
    last_read_outline_node_id: Mapped[str | None] = mapped_column(sa.Text)
    legacy_paper_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    legacy_paper_ids_json: Mapped[list[Any]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    legacy_payload_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)

    document: Mapped[LibraryDocument] = relationship(back_populates="project_links")


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        sa.Index(
            "ux_collections_project_parent_name",
            "project_id",
            sa.text("COALESCE(parent_id, '')"),
            "name_key",
            unique=True,
        ),
        _enum("kind", "manual", "smart"),
        _json_valid("rule_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("collections.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False, default="manual")
    rule_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    order_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class CollectionDocument(Base):
    __tablename__ = "collection_documents"
    __table_args__ = (
        sa.Index("ix_collection_documents_document", "document_id"),
    )

    collection_id: Mapped[str] = mapped_column(
        sa.ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    tag_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    color: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class DocumentTag(Base):
    __tablename__ = "document_tags"
    __table_args__ = (
        sa.Index("ix_document_tags_tag", "tag_id", "document_id"),
        _enum("source", "user", "import", "ai"),
    )

    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="user")
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class FileAsset(Base):
    __tablename__ = "file_assets"
    __table_args__ = (
        sa.UniqueConstraint("id", "document_id", name="uq_file_asset_document"),
        sa.UniqueConstraint(
            "document_id", "role", "sha256", name="uq_file_asset_content_role"
        ),
        _enum("role", "main_pdf", "supplement", "screenshot", "ocr_text", "web_snapshot"),
        sa.CheckConstraint("size_bytes >= 0"),
        sa.CheckConstraint("page_count IS NULL OR page_count >= 0"),
        _enum("text_layer", "available", "missing", "partial"),
        _json_valid("legacy_payload_json"),
        sa.Index("ix_file_assets_sha256", "sha256"),
        sa.Index("ix_file_assets_document_role", "document_id", "role"),
        sa.Index(
            "ux_file_assets_one_active_main_pdf",
            "document_id",
            unique=True,
            sqlite_where=sa.text("role = 'main_pdf' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    file_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    text_layer: Mapped[str] = mapped_column(sa.Text, nullable=False, default="missing")
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_attachment_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    legacy_payload_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)

    document: Mapped[LibraryDocument] = relationship(back_populates="files")
    locations: Mapped[list[FileAssetLocation]] = relationship(
        back_populates="file_asset", cascade="all, delete-orphan"
    )
    annotations: Mapped[list[Annotation]] = relationship(
        back_populates="file_asset", overlaps="annotations,document"
    )


class FileAssetLocation(Base):
    __tablename__ = "file_asset_locations"
    __table_args__ = (
        sa.Index("ix_file_asset_locations_asset", "file_asset_id", "location_type"),
        _enum("location_type", "managed", "legacy"),
        _boolean("available_at_migration"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    file_asset_id: Mapped[str] = mapped_column(
        sa.ForeignKey("file_assets.id", ondelete="CASCADE"), nullable=False
    )
    location_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    available_at_migration: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

    file_asset: Mapped[FileAsset] = relationship(back_populates="locations")


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["file_id", "document_id"],
            ["file_assets.id", "file_assets.document_id"],
            ondelete="CASCADE",
        ),
        sa.Index(
            "ux_annotations_external",
            "file_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=sa.text("external_id <> ''"),
        ),
        sa.Index(
            "ix_annotations_file_page_sort",
            "file_id",
            "page_index",
            "sort_index",
        ),
        sa.Index("ix_annotations_document_updated", "document_id", sa.text("updated_at DESC")),
        _enum("type", "highlight", "underline", "strikeout", "note", "region"),
        sa.CheckConstraint(
            "color = '' OR color GLOB "
            "'#[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]'"
        ),
        sa.CheckConstraint("page_index >= 0"),
        _enum("text_source", "pdf_text", "ocr"),
        sa.CheckConstraint("char_start IS NULL OR char_start >= 0"),
        sa.CheckConstraint("char_end IS NULL OR char_end >= 0"),
        _json_valid("rects_json"),
        _json_valid("selector_json"),
        _enum("anchor_status", "exact", "partial", "none", "no_text_layer", "out_of_range"),
        _enum("source", "user", "external_pdf", "ai_suggested", "legacy"),
        _boolean("read_only"),
        sa.CheckConstraint("revision >= 1"),
        _json_valid("legacy_payload_json"),
        sa.CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end >= char_start"
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    quote: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    comment_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    color: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    page_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    page_label: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    text_source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pdf_text")
    text_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    char_start: Mapped[int | None] = mapped_column(sa.Integer)
    char_end: Mapped[int | None] = mapped_column(sa.Integer)
    rects_json: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    selector_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    sort_index: Mapped[str] = mapped_column(sa.Text, nullable=False)
    anchor_status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="exact")
    author_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    deleted_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="user")
    read_only: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    external_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    legacy_payload_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(sa.Text)

    document: Mapped[LibraryDocument] = relationship(
        back_populates="annotations", overlaps="annotations,file_asset"
    )
    file_asset: Mapped[FileAsset] = relationship(
        back_populates="annotations", overlaps="annotations,document"
    )


class AnnotationTag(Base):
    __tablename__ = "annotation_tags"

    annotation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(
        sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class AnnotationOperationBatch(Base):
    """P0-04 batch receipt with enough state for a deterministic undo."""

    __tablename__ = "annotation_operation_batches"
    __table_args__ = (
        sa.Index(
            "ix_annotation_operation_batches_document",
            "document_id",
            sa.text("created_at DESC"),
        ),
        _enum(
            "action",
            "batch_delete",
            "batch_set_color",
            "batch_set_type",
        ),
        _json_valid("target_ids_json"),
        _json_valid("filter_json"),
        _json_valid("before_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    file_asset_id: Mapped[str] = mapped_column(
        sa.ForeignKey("file_assets.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target_ids_json: Mapped[list[str]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    filter_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    before_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    client_mutation_id: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default=""
    )
    affected_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    undone_at: Mapped[str | None] = mapped_column(sa.Text)


class AnnotationAudit(Base):
    """Immutable annotation mutation journal.

    The P0-04 design called this concept ``annotation_operation_logs``.  The
    implementation keeps the explicitly requested ``annotation_audit`` table
    and exposes a compatibility view with the design's name.
    """

    __tablename__ = "annotation_audit"
    __table_args__ = (
        sa.Index(
            "ix_annotation_audit_annotation",
            "annotation_id",
            "created_at",
        ),
        sa.Index(
            "ix_annotation_audit_document",
            "document_id",
            sa.text("created_at DESC"),
        ),
        sa.Index("ix_annotation_audit_batch", "batch_id", "created_at"),
        _json_valid("before_json"),
        _json_valid("after_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("annotation_operation_batches.id", ondelete="SET NULL")
    )
    annotation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(
        sa.ForeignKey("file_assets.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    request_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    client_mutation_id: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default=""
    )
    before_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    after_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class AnnotationMutationReceipt(Base):
    """Idempotency receipt for a clientMutationId."""

    __tablename__ = "annotation_mutation_receipts"
    __table_args__ = (
        sa.Index("ix_annotation_mutation_receipts_annotation", "annotation_id"),
        sa.Index("ix_annotation_mutation_receipts_batch", "batch_id"),
        _json_valid("response_json"),
    )

    client_mutation_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    annotation_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    batch_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("annotation_operation_batches.id", ondelete="SET NULL")
    )
    response_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    event_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class AnnotationUndoToken(Base):
    """Ten-second single-use undo grant created by soft delete."""

    __tablename__ = "annotation_undo_tokens"
    __table_args__ = (
        sa.Index(
            "ix_annotation_undo_tokens_annotation",
            "annotation_id",
            "expires_at",
        ),
        sa.CheckConstraint("used_at IS NULL OR used_at <> ''"),
    )

    token: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    annotation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(
        sa.ForeignKey("file_assets.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    issued_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    used_at: Mapped[str | None] = mapped_column(sa.Text)


class AnnotationEventOutbox(Base):
    """Durable ordered P0-04 events consumed by UI bridges and tests."""

    __tablename__ = "annotation_event_outbox"
    __table_args__ = (
        sa.UniqueConstraint("sequence", name="uq_annotation_event_sequence"),
        sa.Index(
            "ix_annotation_event_outbox_document",
            "document_id",
            "sequence",
        ),
        _json_valid("payload_json"),
    )

    event_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    file_asset_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class AnnotationEventSequence(Base):
    """SQLite AUTOINCREMENT allocator for concurrent event writers."""

    __tablename__ = "annotation_event_sequence"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


class OutlineVersion(Base):
    __tablename__ = "outline_versions"
    __table_args__ = (
        sa.UniqueConstraint("document_id", "kind", "version", name="uq_outline_version"),
        sa.Index(
            "ux_outline_versions_current",
            "document_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("is_current = 1"),
        ),
        _enum("kind", "paper_structure", "reading_outline"),
        sa.CheckConstraint("version >= 1"),
        _enum("status", "ai_draft", "draft", "confirmed", "archived"),
        _boolean("is_current"),
        _enum("created_by", "ai", "user", "mixed", "legacy"),
        sa.CheckConstraint("revision >= 1"),
        _json_valid("legacy_source_json"),
        sa.Index(
            "ix_outline_versions_document_kind_updated",
            "document_id",
            "kind",
            sa.text("updated_at DESC"),
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False, default="reading_outline")
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ai_draft")
    base_version_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_versions.id", ondelete="SET NULL")
    )
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_run_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ai")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    legacy_source_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

    document: Mapped[LibraryDocument] = relationship(back_populates="outline_versions")
    nodes: Mapped[list[OutlineNode]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class OutlineNode(Base):
    __tablename__ = "outline_nodes"
    __table_args__ = (
        sa.Index(
            "ux_outline_nodes_position",
            "outline_version_id",
            sa.text("COALESCE(parent_id, '')"),
            "order_index",
            unique=True,
        ),
        sa.Index("ix_outline_nodes_parent", "outline_version_id", "parent_id", "order_index"),
        sa.CheckConstraint("order_index >= 0"),
        sa.CheckConstraint("heading_level BETWEEN 1 AND 4"),
        _enum("origin", "ai", "user", "mixed"),
        _boolean("active"),
        _json_valid("legacy_source_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_versions.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_nodes.id", ondelete="CASCADE")
    )
    order_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    heading_level: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ai")
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    legacy_source_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

    version: Mapped[OutlineVersion] = relationship(back_populates="nodes")
    parent: Mapped[OutlineNode | None] = relationship(
        remote_side="OutlineNode.id", back_populates="children"
    )
    children: Mapped[list[OutlineNode]] = relationship(back_populates="parent")


class OutlineNodeEvidence(Base):
    __tablename__ = "outline_node_evidence"
    __table_args__ = (
        sa.Index("ix_outline_node_evidence_node", "outline_node_id"),
        _enum("kind", "annotation", "page", "outline_node", "document"),
        _enum("match_status", "exact", "partial", "none", "no_text_layer", "out_of_range"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_node_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_nodes.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    annotation_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="SET NULL")
    )
    target_outline_node_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_nodes.id", ondelete="SET NULL")
    )
    page_index: Mapped[int | None] = mapped_column(sa.Integer)
    quote: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    match_status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="exact")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryVersion(Base):
    __tablename__ = "summary_versions"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "version", name="uq_summary_version"),
        sa.Index(
            "ux_summary_versions_run",
            "run_id",
            unique=True,
            sqlite_where=sa.text("run_id <> ''"),
        ),
        sa.Index(
            "ux_summary_versions_legacy_source",
            "project_id",
            "legacy_source_hash",
            unique=True,
            sqlite_where=sa.text("legacy_source_hash <> ''"),
        ),
        sa.Index("ix_summary_versions_project_created", "project_id", sa.text("created_at DESC")),
        sa.CheckConstraint("version >= 1"),
        _enum("mode", "independent", "incremental"),
        _enum(
            "status",
            "draft",
            "queued",
            "running",
            "partial",
            "done",
            "failed",
            "cancelled",
            "final",
        ),
        _boolean("stale"),
        sa.CheckConstraint("revision >= 1"),
        _json_valid("legacy_source_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_version_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    run_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    stale: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    legacy_source_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    legacy_source_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    finalized_at: Mapped[str | None] = mapped_column(sa.Text)

    sections: Mapped[list[SummarySection]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    documents: Mapped[list[SummaryVersionDocument]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class SummaryVersionDocument(Base):
    __tablename__ = "summary_version_documents"
    __table_args__ = (
        sa.Index("ix_summary_version_documents_document", "document_id"),
        _enum("inclusion_status", "included", "excluded"),
    )

    summary_version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_versions.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), primary_key=True
    )
    file_sha256: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    outline_version_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_versions.id", ondelete="SET NULL")
    )
    outline_revision: Mapped[int | None] = mapped_column(sa.Integer)
    inclusion_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    exclusion_reason: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

    version: Mapped[SummaryVersion] = relationship(back_populates="documents")


class SummarySection(Base):
    __tablename__ = "summary_sections"
    __table_args__ = (
        sa.UniqueConstraint("summary_version_id", "stable_key", name="uq_summary_section_key"),
        sa.Index(
            "ix_summary_sections_version_order",
            "summary_version_id",
            "parent_id",
            "order_index",
        ),
        sa.CheckConstraint("order_index >= 0"),
        _enum("change_type", "new", "changed", "unchanged", "removed", "unsupported"),
        sa.CheckConstraint("revision >= 1"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_versions.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_sections.id", ondelete="CASCADE")
    )
    stable_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    order_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    change_type: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unchanged")
    base_section_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_sections.id", ondelete="SET NULL")
    )
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

    version: Mapped[SummaryVersion] = relationship(back_populates="sections")


class SummarySectionEvidence(Base):
    __tablename__ = "summary_section_evidence"
    __table_args__ = (
        sa.Index("ix_summary_section_evidence_section", "summary_section_id"),
        _enum("kind", "annotation", "page", "outline_node", "document"),
        _enum("match_status", "exact", "partial", "none", "no_text_layer", "out_of_range"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_section_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_sections.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), nullable=False
    )
    annotation_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="SET NULL")
    )
    outline_node_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_nodes.id", ondelete="SET NULL")
    )
    page_index: Mapped[int | None] = mapped_column(sa.Integer)
    quote: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    match_status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="exact")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class DocumentRelation(Base):
    __tablename__ = "document_relations"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "fingerprint", name="uq_document_relation"),
        sa.Index(
            "ix_document_relations_from",
            "project_id",
            "from_document_id",
            "relation_type",
        ),
        _enum("relation_type", "cites", "extends", "compares", "replaces", "diverges"),
        _enum("source", "openalex", "user", "llm"),
        _enum("certainty", "stated", "implied", "inferred"),
        _boolean("verified"),
        _enum("review_status", "pending", "confirmed", "rejected"),
        sa.CheckConstraint("from_document_id <> to_document_id"),
        sa.CheckConstraint("verified = 0 OR locator <> '' OR external_id <> ''"),
        sa.Index(
            "ix_document_relations_to",
            "project_id",
            "to_document_id",
            "relation_type",
        ),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), nullable=False
    )
    to_document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    external_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    locator: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_record_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    certainty: Mapped[str] = mapped_column(sa.Text, nullable=False, default="inferred")
    verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    review_status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    review_note: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    fingerprint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


# ---------------------------------------------------------------------------
# P0-05 versioned reading outlines
# ---------------------------------------------------------------------------


class ReadingOutline(Base):
    __tablename__ = "reading_outlines"
    __table_args__ = (
        sa.UniqueConstraint("document_id", "kind", name="uq_reading_outline_document_kind"),
        _enum("kind", "reading_outline", "paper_structure"),
        sa.CheckConstraint("current_revision_no >= 0"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="reading_outline"
    )
    current_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_revisions.id", ondelete="SET NULL")
    )
    current_revision_no: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineAIRun(Base):
    __tablename__ = "outline_ai_runs"
    __table_args__ = (
        sa.Index(
            "ix_outline_ai_runs_outline_created",
            "outline_id",
            sa.text("created_at DESC"),
        ),
        _enum("status", "queued", "running", "succeeded", "failed", "cancelled"),
        _json_valid("input_scope_json"),
        _json_valid("result_json"),
        sa.CheckConstraint("base_revision_no >= 0"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_id: Mapped[str] = mapped_column(
        sa.ForeignKey("reading_outlines.id", ondelete="CASCADE"), nullable=False
    )
    base_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_revisions.id", ondelete="SET NULL")
    )
    base_revision_no: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_scope_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    result_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    error_code: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    started_at: Mapped[str | None] = mapped_column(sa.Text)
    finished_at: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineRevision(Base):
    __tablename__ = "outline_revisions"
    __table_args__ = (
        sa.UniqueConstraint(
            "outline_id", "revision_no", name="uq_outline_revision_no"
        ),
        sa.Index(
            "ix_outline_revisions_outline_revision",
            "outline_id",
            sa.text("revision_no DESC"),
        ),
        _enum("status", "applied", "ai_candidate", "rejected"),
        _enum(
            "source",
            "manual",
            "ai",
            "ai_merge",
            "rollback",
            "legacy",
            "migration",
        ),
        _json_valid("tree_json"),
        _json_valid("legacy_source_json"),
        sa.CheckConstraint("revision_no >= 1"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_id: Mapped[str] = mapped_column(
        sa.ForeignKey("reading_outlines.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    base_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_revisions.id", ondelete="SET NULL")
    )
    base_revision_no: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tree_json: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    change_summary: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ai_run_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_ai_runs.id", ondelete="SET NULL")
    )
    legacy_source_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    legacy_source_hash: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default=""
    )


class OutlineNodeEntity(Base):
    __tablename__ = "outline_node_entities"
    __table_args__ = (
        sa.Index("ix_outline_node_entities_outline", "outline_id"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_id: Mapped[str] = mapped_column(
        sa.ForeignKey("reading_outlines.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


class OutlineRevisionNode(Base):
    __tablename__ = "outline_revision_nodes"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["revision_id", "parent_node_id"],
            [
                "outline_revision_nodes.revision_id",
                "outline_revision_nodes.node_id",
            ],
            ondelete="CASCADE",
        ),
        sa.Index(
            "ix_outline_revision_nodes_stable_node",
            "node_id",
            "revision_id",
        ),
        sa.CheckConstraint("order_index >= 0"),
        sa.CheckConstraint("heading_level BETWEEN 1 AND 6"),
        _boolean("locked"),
        _boolean("user_edited"),
        _enum("origin", "ai", "user", "mixed", "legacy"),
    )

    revision_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    node_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_node_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_node_id: Mapped[str | None] = mapped_column(sa.Text)
    order_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    heading_level: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    anchor_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    note_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    locked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    user_edited: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ai")
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False)


class OutlineNodeAnnotation(Base):
    __tablename__ = "outline_node_annotations"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["revision_id", "node_id"],
            [
                "outline_revision_nodes.revision_id",
                "outline_revision_nodes.node_id",
            ],
            ondelete="CASCADE",
        ),
        sa.Index(
            "ix_outline_node_annotations_annotation",
            "annotation_id",
            "revision_id",
        ),
        _enum("relation_type", "evidence", "related", "follow_up"),
    )

    revision_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    node_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    annotation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="CASCADE"), primary_key=True
    )
    relation_type: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="evidence"
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class ProjectOutlineNodeState(Base):
    __tablename__ = "project_outline_node_states"
    __table_args__ = (
        sa.Index(
            "ix_project_outline_node_states_outline",
            "project_id",
            "outline_id",
            "completed",
        ),
        _boolean("completed"),
    )

    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    outline_id: Mapped[str] = mapped_column(
        sa.ForeignKey("reading_outlines.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_node_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    completed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    completed_at: Mapped[str | None] = mapped_column(sa.Text)
    last_viewed_at: Mapped[str | None] = mapped_column(sa.Text)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineMergeConflict(Base):
    __tablename__ = "outline_merge_conflicts"
    __table_args__ = (
        sa.Index(
            "ix_outline_merge_conflicts_revision",
            "candidate_revision_id",
            "resolution",
            "node_id",
        ),
        _enum(
            "conflict_type",
            "both_modified",
            "user_deleted_ai_modified",
            "ai_deleted_user_modified",
            "move_vs_content",
            "anchor_conflict",
            "parent_conflict",
        ),
        _enum(
            "resolution",
            "pending",
            "keep_current",
            "accept_ai",
            "manual_merge",
            "dismissed",
        ),
        _json_valid("base_node_json"),
        _json_valid("current_node_json"),
        _json_valid("ai_node_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    outline_id: Mapped[str] = mapped_column(
        sa.ForeignKey("reading_outlines.id", ondelete="CASCADE"), nullable=False
    )
    ai_run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_ai_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_revision_id: Mapped[str] = mapped_column(
        sa.ForeignKey("outline_revisions.id", ondelete="CASCADE"), nullable=False
    )
    base_revision_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    current_revision_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    node_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_node_entities.id", ondelete="SET NULL")
    )
    conflict_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_node_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    current_node_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    ai_node_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    resolution: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="pending"
    )
    resolved_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    resolved_at: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineMutationReceipt(Base):
    __tablename__ = "outline_mutation_receipts"
    __table_args__ = (_json_valid("response_json"),)

    client_mutation_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    outline_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    response_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineEventOutbox(Base):
    __tablename__ = "outline_event_outbox"
    __table_args__ = (
        sa.UniqueConstraint("sequence", name="uq_outline_event_sequence"),
        sa.Index(
            "ix_outline_event_outbox_document",
            "document_id",
            "sequence",
        ),
        _json_valid("payload_json"),
    )

    event_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    outline_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class OutlineEventSequence(Base):
    __tablename__ = "outline_event_sequence"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


# ---------------------------------------------------------------------------
# P0-06 project summaries
# ---------------------------------------------------------------------------


class ProjectSummary(Base):
    __tablename__ = "project_summaries"
    __table_args__ = (
        sa.UniqueConstraint("project_id", name="uq_project_summary"),
        sa.CheckConstraint("current_revision_no >= 0"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    current_official_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="SET NULL")
    )
    current_revision_no: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryAIRunRecord(Base):
    __tablename__ = "summary_ai_runs"
    __table_args__ = (
        sa.Index(
            "ix_summary_ai_runs_summary_created",
            "summary_id",
            sa.text("created_at DESC"),
        ),
        _enum("mode", "independent", "incremental"),
        _enum("scope_type", "single_document", "multi_document", "day", "project"),
        _enum(
            "status",
            "queued",
            "running",
            "partial",
            "succeeded",
            "failed",
            "cancelled",
        ),
        _json_valid("scope_json"),
        _json_valid("input_scope_json"),
        _json_valid("result_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_id: Mapped[str] = mapped_column(
        sa.ForeignKey("project_summaries.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="SET NULL")
    )
    base_revision_no: Mapped[int | None] = mapped_column(sa.Integer)
    scope_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_scope_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    result_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    error_code: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error_message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    started_at: Mapped[str | None] = mapped_column(sa.Text)
    finished_at: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryRevisionRecord(Base):
    __tablename__ = "summary_revisions"
    __table_args__ = (
        sa.UniqueConstraint("summary_id", "revision_no", name="uq_summary_revision"),
        sa.Index(
            "ix_summary_revisions_summary_revision",
            "summary_id",
            sa.text("revision_no DESC"),
        ),
        _enum("mode", "independent", "incremental"),
        _enum("status", "candidate", "official", "rejected"),
        _enum("source", "ai", "manual", "rollback", "legacy", "migration"),
        _enum("scope_type", "single_document", "multi_document", "day", "project"),
        _json_valid("scope_json"),
        _json_valid("structured_json"),
        _json_valid("legacy_source_json"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_id: Mapped[str] = mapped_column(
        sa.ForeignKey("project_summaries.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False)
    base_revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="SET NULL")
    )
    base_revision_no: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    structured_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    change_summary: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)
    confirmed_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    confirmed_at: Mapped[str | None] = mapped_column(sa.Text)
    ai_run_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_ai_runs.id", ondelete="SET NULL")
    )
    legacy_source_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    legacy_source_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


class SummarySectionEntity(Base):
    __tablename__ = "summary_section_entities"
    __table_args__ = (
        sa.UniqueConstraint("summary_id", "stable_key", name="uq_summary_section_key"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_id: Mapped[str] = mapped_column(
        sa.ForeignKey("project_summaries.id", ondelete="CASCADE"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryRevisionSection(Base):
    __tablename__ = "summary_revision_sections"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["revision_id", "parent_section_id"],
            [
                "summary_revision_sections.revision_id",
                "summary_revision_sections.section_id",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "revision_id", "stable_key", name="uq_summary_revision_section_key"
        ),
        _enum(
            "change_status",
            "added",
            "changed",
            "unchanged",
            "removed",
            "insufficient_evidence",
        ),
    )

    revision_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    section_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_section_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_section_id: Mapped[str | None] = mapped_column(sa.Text)
    stable_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    order_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    change_status: Mapped[str | None] = mapped_column(sa.Text)
    base_section_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_section_entities.id", ondelete="SET NULL")
    )
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False)


class SummaryRevisionDocumentRecord(Base):
    __tablename__ = "summary_revision_documents"
    __table_args__ = (
        sa.Index(
            "ix_summary_revision_documents_document",
            "document_id",
            "revision_id",
        ),
        _enum("inclusion_status", "included", "excluded"),
    )

    revision_id: Mapped[str] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), primary_key=True
    )
    inclusion_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    exclusion_reason: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    file_sha256: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    outline_revision_id: Mapped[str | None] = mapped_column(sa.Text)
    outline_revision_no: Mapped[int | None] = mapped_column(sa.Integer)
    annotation_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    source_updated_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


class SummaryEvidenceRecord(Base):
    __tablename__ = "summary_revision_section_evidence"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["revision_id", "section_id"],
            [
                "summary_revision_sections.revision_id",
                "summary_revision_sections.section_id",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "revision_id", "section_id", "evidence_token", name="uq_summary_evidence"
        ),
        _enum("evidence_status", "exact", "partial", "none", "insufficient"),
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    revision_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    section_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_token: Mapped[str] = mapped_column(sa.Text, nullable=False)
    document_id: Mapped[str] = mapped_column(
        sa.ForeignKey("library_documents.id", ondelete="RESTRICT"), nullable=False
    )
    outline_node_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("outline_node_entities.id", ondelete="SET NULL")
    )
    annotation_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("annotations.id", ondelete="SET NULL")
    )
    claim_id: Mapped[str | None] = mapped_column(sa.Text)
    anchor_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    quote: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source_label: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    evidence_status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryOperationLog(Base):
    __tablename__ = "summary_operation_logs"
    __table_args__ = (_json_valid("detail_json"),)

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    summary_id: Mapped[str] = mapped_column(
        sa.ForeignKey("project_summaries.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("summary_revisions.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict
    )
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryEventOutbox(Base):
    __tablename__ = "summary_event_outbox"
    __table_args__ = (_json_valid("payload_json"),)

    event_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    project_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)


class SummaryEventSequence(Base):
    __tablename__ = "summary_event_sequence"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


class SummaryMutationReceipt(Base):
    __tablename__ = "summary_mutation_receipts"
    __table_args__ = (_json_valid("response_json"),)

    client_mutation_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    summary_id: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    response_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(sa.Text, nullable=False, default=now_iso)

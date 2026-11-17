"""Public API for the ResearchPilot v1.16 P0-01 data layer."""

from .contracts import (
    AnnotationCreateInput,
    DocumentCreateInput,
    FileAssetCreateInput,
    OutlineVersionCreateInput,
    ProjectDocumentCreateInput,
    SummaryVersionCreateInput,
)
from .db import (
    SCHEMA_VERSION,
    current_schema_versions,
    deploy_schema,
    engine_for,
    get_model_version,
    session_factory,
    session_scope,
    set_model_version,
)
from .migration import MigrationEngine
from .normalization import (
    document_identity_key,
    normalize_doi,
    normalize_title,
    title_key,
)
from .orm import Base
from .services import (
    AnnotationService,
    ConflictError,
    DocumentService,
    DuplicateMatch,
    NotFoundError,
    OutlineService,
    RevisionConflictError,
    SummaryService,
    V116Error,
    V116Services,
    ValidationError,
)

__all__ = [
    "SCHEMA_VERSION",
    "AnnotationCreateInput",
    "AnnotationService",
    "Base",
    "ConflictError",
    "DocumentCreateInput",
    "DocumentService",
    "DuplicateMatch",
    "FileAssetCreateInput",
    "MigrationEngine",
    "NotFoundError",
    "OutlineService",
    "OutlineVersionCreateInput",
    "ProjectDocumentCreateInput",
    "RevisionConflictError",
    "SummaryService",
    "SummaryVersionCreateInput",
    "V116Error",
    "V116Services",
    "ValidationError",
    "current_schema_versions",
    "deploy_schema",
    "document_identity_key",
    "engine_for",
    "get_model_version",
    "normalize_doi",
    "normalize_title",
    "session_factory",
    "session_scope",
    "set_model_version",
    "title_key",
]

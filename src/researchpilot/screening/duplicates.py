"""Three-level duplicate lookup for screening candidates."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..storage.v116.normalization import (
    first_author_compatible,
    normalize_doi,
    title_quality,
    title_similarity,
    year_compatible,
)
from ..storage.v116.orm import LibraryDocument
from ..storage.v116.services import DocumentService
from ..storage.v116.normalization import stable_id
from .contracts import CandidateMetadata
from .orm import CandidateDuplicateMatch, ImportCandidate, now_iso

TITLE_FUZZY_THRESHOLD = 0.88


class DuplicateResolver:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.documents = DocumentService(factory)

    def refresh(
        self,
        session: Session,
        candidate: ImportCandidate,
        metadata: CandidateMetadata,
        *,
        sha256: str,
    ) -> list[CandidateDuplicateMatch]:
        exact = self.documents.find_duplicates_in_session(
            session,
            doi=metadata.doi,
            title=metadata.title,
            authors=metadata.authors,
            year=metadata.year,
            sha256=sha256,
        )
        matched: dict[tuple[str, str], tuple[str, float | None, dict[str, Any]]] = {}
        for match in exact:
            status = match.match_status
            similarity: float | None = None
            if match.match_basis == "doi" and title_quality(metadata.title) and title_quality(
                match.document.title
            ):
                similarity = title_similarity(metadata.title, match.document.title)
                if similarity < 0.80:
                    status = "conflict"
            if match.match_basis == "sha256":
                candidate_doi = normalize_doi(metadata.doi)
                if candidate_doi and match.document.doi_key and candidate_doi != match.document.doi_key:
                    status = "conflict"
            matched[(match.document.id, match.match_basis)] = (
                status,
                similarity,
                {
                    "title": match.document.title,
                    "doi": match.document.doi_key or "",
                    "conflictingFields": list(match.conflicting_fields),
                },
            )

        # A bounded fuzzy pass is intentional: exact DOI/title remains the
        # canonical identity; fuzzy title matches are only review candidates.
        if title_quality(metadata.title):
            rows = session.scalars(sa.select(LibraryDocument).limit(2000)).all()
            for document in rows:
                if (document.id, "title") in matched or not title_quality(document.title):
                    continue
                similarity = title_similarity(metadata.title, document.title)
                if similarity < TITLE_FUZZY_THRESHOLD:
                    continue
                if not year_compatible(metadata.year, document.year) or not first_author_compatible(
                    metadata.authors, document.authors_json
                ):
                    continue
                conflict = bool(
                    normalize_doi(metadata.doi)
                    and document.doi_key
                    and normalize_doi(metadata.doi) != document.doi_key
                )
                matched[(document.id, "title")] = (
                    "conflict" if conflict else ("exact" if similarity >= 0.98 else "possible"),
                    similarity,
                    {
                        "title": document.title,
                        "doi": document.doi_key or "",
                        "year": document.year,
                    },
                )

        session.execute(
            sa.delete(CandidateDuplicateMatch).where(
                CandidateDuplicateMatch.candidate_id == candidate.id
            )
        )
        rows: list[CandidateDuplicateMatch] = []
        for (document_id, basis), (match_status, similarity, detail) in sorted(
            matched.items()
        ):
            row = CandidateDuplicateMatch(
                id=stable_id("cdm", candidate.id, document_id, basis),
                candidate_id=candidate.id,
                document_id=document_id,
                match_basis=basis,
                match_status=match_status,
                title_similarity=similarity,
                detail_json=detail,
                created_at=now_iso(),
            )
            session.add(row)
            rows.append(row)

        exact_sha = [
            row for row in rows if row.match_basis == "sha256" and row.match_status == "exact"
        ]
        exact_doi = [
            row for row in rows if row.match_basis == "doi" and row.match_status == "exact"
        ]
        conflicts = [row for row in rows if row.match_status == "conflict"]
        if exact_sha:
            candidate.duplicate_status = (
                "conflict" if len(exact_sha) > 1 else "exact_sha256"
            )
            candidate.duplicate_document_id = exact_sha[0].document_id
            candidate.duplicate_basis = "sha256"
        elif exact_doi:
            candidate.duplicate_status = "exact_doi"
            candidate.duplicate_document_id = exact_doi[0].document_id
            candidate.duplicate_basis = "doi"
        elif conflicts:
            candidate.duplicate_status = "conflict"
            candidate.duplicate_document_id = conflicts[0].document_id
            candidate.duplicate_basis = "title"
        elif rows:
            candidate.duplicate_status = "possible_title"
            candidate.duplicate_document_id = rows[0].document_id
            candidate.duplicate_basis = "title"
        else:
            candidate.duplicate_status = "none"
            candidate.duplicate_document_id = None
            candidate.duplicate_basis = ""
        session.flush()
        return rows

    def resolve_now(
        self,
        *,
        doi: str = "",
        title: str = "",
        authors: list[str] | str | None = None,
        year: str = "",
        sha256: str = "",
    ) -> list[dict[str, Any]]:
        with self.factory() as session:
            matches = self.documents.find_duplicates_in_session(
                session,
                doi=doi,
                title=title,
                authors=authors,
                year=year,
                sha256=sha256,
            )
            return [item.as_dict() for item in matches]

"""Optional FastAPI transport for the P0-02 candidate service."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterable

try:
    from fastapi import FastAPI, File, Form, Query, Request, UploadFile
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised only without optional deps
    raise ImportError(
        "FastAPI transport requires: pip install fastapi 'uvicorn[standard]' python-multipart"
    ) from exc

from ..config import PATHS
from ..storage.v116.db import engine_for, session_factory
from .contracts import (
    BulkCandidateDecisionCommand,
    CandidateListQuery,
    CancelScreeningCommand,
    CreateCandidateCommand,
    RequestScreeningCommand,
    RestoreCandidateCommand,
    SubmitCandidateDecisionCommand,
    UpdateCandidateMetadataCommand,
)
from .db import deploy_screening_schema
from .errors import ScreeningError, api_error_payload
from .service import CandidateService


def create_screening_app(
    *,
    db_path: str | Path | None = None,
    quarantine_root: str | Path | None = None,
    deploy: bool = False,
) -> FastAPI:
    engine = engine_for(db_path or PATHS.db_path)
    if deploy:
        deploy_screening_schema(engine)
    factory = session_factory(engine)
    service = CandidateService(
        factory,
        quarantine_root=quarantine_root or (PATHS.outputs / "quarantine"),
    )
    app = FastAPI(title="ResearchPilot Screening API", version="1.16.2-p0-02")
    app.state.engine = engine
    app.state.service = service

    @app.exception_handler(ScreeningError)
    async def screening_error_handler(
        request: Request, exc: ScreeningError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=api_error_payload(
                exc,
                request_id=(
                    request.headers.get("X-Request-ID") or uuid.uuid4().hex
                ),
            ),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "schema": "1.16.2-p0-02"}

    @app.post("/api/projects/{project_id}/import-batches")
    async def create_batch(
        project_id: str,
        body: dict,
    ) -> dict:
        batch = service.create_batch(
            project_id=project_id,
            batch_id=str(body.get("batchId") or ""),
            source_surface=str(body.get("sourceSurface") or "api"),
            created_by=str(body.get("createdBy") or ""),
            note=str(body.get("note") or ""),
        )
        return {
            "id": batch.id,
            "projectId": batch.project_id,
            "sourceSurface": batch.source_surface,
            "status": batch.status,
            "createdBy": batch.created_by,
            "note": batch.note,
            "createdAt": batch.created_at,
            "updatedAt": batch.updated_at,
        }

    @app.post("/api/projects/{project_id}/import-candidates/upload")
    async def upload_candidate(
        project_id: str,
        file: UploadFile = File(...),
        batch_id: str = Form(..., alias="batchId"),
        created_by: str = Form(default="", alias="createdBy"),
        client_mutation_id: str = Form(..., alias="clientMutationId"),
    ) -> dict:
        def chunks() -> Iterable[bytes]:
            file.file.seek(0)
            while chunk := file.file.read(1024 * 1024):
                yield chunk

        return service.upload_candidate(
            project_id=project_id,
            batch_id=batch_id,
            file_name=file.filename or "upload",
            mime_type=file.content_type or "",
            chunks=chunks(),
            created_by=created_by,
            client_mutation_id=client_mutation_id,
        )

    @app.post("/api/import-candidates")
    async def create_candidate(body: dict) -> dict:
        command = CreateCandidateCommand.model_validate(body)
        return service.create_candidate(command)

    @app.get("/api/import-candidates")
    async def list_candidates(
        project_id: str = Query(alias="projectId"),
        status: list[str] = Query(default=[]),
        search: str = "",
        duplicate_only: bool = Query(default=False, alias="duplicateOnly"),
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        query = CandidateListQuery.model_validate(
            {
                "projectId": project_id,
                "statuses": status,
                "search": search,
                "duplicateOnly": duplicate_only,
                "limit": limit,
                "cursor": cursor,
            }
        )
        return service.list(query)

    @app.get("/api/import-candidates/{candidate_id}")
    async def candidate_detail(candidate_id: str) -> dict:
        return service.detail(candidate_id)

    @app.post("/api/import-candidates/{candidate_id}/screen")
    async def request_screening(candidate_id: str, body: dict) -> dict:
        payload = {**body, "candidateId": candidate_id}
        return service.request_screening(RequestScreeningCommand.model_validate(payload))

    @app.patch("/api/import-candidates/{candidate_id}/metadata")
    async def update_metadata(candidate_id: str, body: dict) -> dict:
        payload = {**body, "candidateId": candidate_id}
        return service.update_metadata(
            UpdateCandidateMetadataCommand.model_validate(payload)
        )

    @app.post("/api/import-candidates/{candidate_id}/cancel-screen")
    async def cancel_screening(candidate_id: str, body: dict | None = None) -> dict:
        payload = {**(body or {}), "candidateId": candidate_id}
        return service.cancel(CancelScreeningCommand.model_validate(payload))

    @app.post("/api/import-candidates/{candidate_id}/decision")
    async def submit_decision(candidate_id: str, body: dict) -> dict:
        payload = {**body, "candidateId": candidate_id}
        return service.submit_decision(
            SubmitCandidateDecisionCommand.model_validate(payload)
        )

    @app.post("/api/import-candidates/bulk-decision")
    async def submit_bulk_decision(body: dict) -> dict:
        return service.submit_bulk(
            BulkCandidateDecisionCommand.model_validate(body)
        )

    @app.post("/api/import-candidates/{candidate_id}/restore")
    async def restore_candidate(candidate_id: str, body: dict) -> dict:
        payload = {**body, "candidateId": candidate_id}
        return service.restore(RestoreCandidateCommand.model_validate(payload))

    return app

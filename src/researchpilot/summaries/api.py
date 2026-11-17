"""FastAPI transport for P0-06 project summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import PATHS
from ..storage.v116.db import deploy_schema, engine_for, session_factory
from .contracts import (
    CompleteGenerationRequest,
    ConfirmRevisionCommand,
    GenerateSummaryCommand,
    RejectRevisionCommand,
    RestoreSummaryRevisionCommand,
    SaveMarkdownCommand,
    UpdateSectionCommand,
)
from .errors import SummaryError
from .service import SummaryApplicationService


def create_summary_app(
    *,
    db_path: str | Path | None = None,
    deploy: bool = False,
    llm_client: Any = None,
) -> FastAPI:
    engine = engine_for(db_path or PATHS.db_path)
    if deploy:
        deploy_schema(engine, note="P0-06 summary API")
    factory = session_factory(engine)
    service = SummaryApplicationService(factory, llm_client=llm_client)
    app = FastAPI(
        title="ResearchPilot Summary API",
        version="1.16.1-p0-06",
        description="Versioned project summaries, evidence and generation runs.",
    )
    app.state.engine = engine
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Actor-ID", "X-Request-ID"],
    )

    @app.exception_handler(SummaryError)
    async def summary_error(_request: Request, exc: SummaryError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.as_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "INTERNAL_ERROR",
                "message": "总结请求字段无效",
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "schema": "1.16.1-p0-06"}

    @app.get("/api/projects/{project_id}/summary", tags=["summary"])
    async def project_summary(project_id: str) -> dict[str, Any]:
        return service.get_project_summary(project_id)

    @app.get("/api/summaries/{summary_id}/revisions", tags=["revisions"])
    async def revisions(summary_id: str) -> dict[str, Any]:
        return service.list_revisions(summary_id)

    @app.get(
        "/api/summaries/{summary_id}/revisions/{revision_no}", tags=["revisions"]
    )
    async def revision(summary_id: str, revision_no: int) -> dict[str, Any]:
        return service.get_revision(summary_id, revision_no)

    @app.get("/api/summaries/{summary_id}/diff", tags=["revisions"])
    async def diff(
        summary_id: str, left: int = Query(ge=1), right: int = Query(ge=1)
    ) -> dict[str, Any]:
        return service.compare(summary_id, left, right)

    @app.post("/api/projects/{project_id}/summary/generate", tags=["generation"])
    async def generate(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        command = GenerateSummaryCommand.model_validate(
            {**body, "projectId": project_id}
        )
        return (
            service.generate_live(command)
            if service.llm_client is not None
            else service.generate(command)
        )

    @app.get(
        "/api/summaries/{summary_id}/ai-runs/{run_id}", tags=["generation"]
    )
    async def get_run(summary_id: str, run_id: str) -> dict[str, Any]:
        return service.get_run(summary_id, run_id)

    @app.get("/api/summary-ai-runs/{run_id}/state", tags=["generation"])
    async def run_state(run_id: str) -> dict[str, Any]:
        return service.generation_state(run_id)

    @app.post(
        "/api/summaries/{summary_id}/ai-runs/{run_id}/complete",
        tags=["generation"],
    )
    async def complete(
        summary_id: str, run_id: str, body: CompleteGenerationRequest
    ) -> dict[str, Any]:
        return service.complete_generation(summary_id, run_id, body)

    @app.post(
        "/api/summaries/{summary_id}/ai-runs/{run_id}/cancel",
        tags=["generation"],
    )
    async def cancel(summary_id: str, run_id: str) -> dict[str, Any]:
        return service.cancel_run(summary_id, run_id)

    @app.post(
        "/api/summaries/{summary_id}/revisions/{revision_id}/section-update",
        tags=["editing"],
    )
    async def update_section(
        summary_id: str, revision_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        command = UpdateSectionCommand.model_validate(
            {
                **body,
                "summaryId": summary_id,
                "revisionId": revision_id,
            }
        )
        return service.update_section(command)

    @app.post(
        "/api/summaries/{summary_id}/revisions/{revision_id}/markdown",
        tags=["editing"],
    )
    async def save_markdown(
        summary_id: str, revision_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        command = SaveMarkdownCommand.model_validate(
            {
                **body,
                "summaryId": summary_id,
                "revisionId": revision_id,
            }
        )
        return service.save_markdown(command)

    @app.post(
        "/api/summaries/{summary_id}/revisions/{revision_id}/confirm",
        tags=["official"],
    )
    async def confirm(
        summary_id: str, revision_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return service.confirm(
            ConfirmRevisionCommand.model_validate(
                {**body, "summaryId": summary_id, "revisionId": revision_id}
            )
        )

    @app.post(
        "/api/summaries/{summary_id}/revisions/{revision_id}/reject",
        tags=["official"],
    )
    async def reject(
        summary_id: str, revision_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return service.reject(
            RejectRevisionCommand.model_validate(
                {**body, "summaryId": summary_id, "revisionId": revision_id}
            )
        )

    @app.post("/api/summaries/{summary_id}/restore", tags=["official"])
    async def restore(
        summary_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return service.restore(
            RestoreSummaryRevisionCommand.model_validate(
                {**body, "summaryId": summary_id}
            )
        )

    @app.get("/api/summaries/{summary_id}/sections", tags=["query"])
    async def filter_sections(
        summary_id: str,
        revision_no: int = Query(alias="revisionNo"),
        query: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        return {
            "items": service.filter_sections(
                summary_id, revision_no, query, status
            )
        }

    @app.get("/api/summary-events", tags=["events"])
    async def events(
        after: int = Query(default=0, alias="afterSequence", ge=0),
        project_id: str | None = Query(default=None, alias="projectId"),
    ) -> dict[str, Any]:
        return {"events": service.events_after(after, project_id)}

    @app.post(
        "/api/summaries/{summary_id}/revisions/{revision_no}/sections/{section_id}/evidence/{evidence_id}/click",
        tags=["evidence"],
    )
    async def evidence_click(
        summary_id: str, revision_no: int, section_id: str, evidence_id: str
    ) -> dict[str, Any]:
        return service.evidence_clicked(
            summary_id, revision_no, section_id, evidence_id
        )

    return app

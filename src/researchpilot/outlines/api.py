"""FastAPI transport for P0-05 outline editing and revision management."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import PATHS
from ..storage.v116.db import deploy_schema, engine_for, session_factory
from .contracts import (
    OUTLINE_EDIT_ADAPTER,
    ApplyOutlineMergeCommand,
    CompleteOutlineAIRunRequest,
    MarkdownPreviewRequest,
    OutlineAIRunRequest,
    ResolveOutlineConflictCommand,
    RestoreOutlineRevisionCommand,
    SetOutlineNodeCompletedCommand,
    ActorContext,
)
from .errors import OutlineError
from .service import OutlineApplicationService


def _error_response(exc: OutlineError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.as_dict())


def create_outline_app(
    *,
    db_path: str | Path | None = None,
    deploy: bool = False,
    llm_client: Any = None,
) -> FastAPI:
    engine = engine_for(db_path or PATHS.db_path)
    if deploy:
        deploy_schema(engine, note="P0-05 outline API")
    factory = session_factory(engine)
    service = OutlineApplicationService(factory, llm_client=llm_client)
    app = FastAPI(
        title="ResearchPilot Outline API",
        version="1.16.1-p0-05",
        description=(
            "Versioned outline tree editing, Markdown parsing, deterministic "
            "AI merge, project progress and reader anchoring."
        ),
    )
    app.state.engine = engine
    app.state.factory = factory
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "X-Actor-ID", "X-Request-ID"],
    )

    @app.exception_handler(OutlineError)
    async def outline_error_handler(
        _request: Request,
        exc: OutlineError,
    ) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "OUTLINE_INVALID_TREE",
                "message": "；".join(
                    str(item.get("msg", "invalid outline request"))
                    for item in exc.errors()
                ),
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        )

    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def database_error_handler(
        _request: Request,
        _exc: sa.exc.SQLAlchemyError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "大纲数据库操作失败，事务已回滚",
            },
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "schema": "1.16.1-p0-05",
            "protocolVersion": "rp.outline.v1",
        }

    @app.get(
        "/api/documents/{document_id}/outlines/{kind}",
        tags=["outline"],
    )
    async def bootstrap(
        document_id: str,
        kind: str,
        project_id: str = Query(alias="projectId"),
    ) -> dict[str, Any]:
        if kind not in {"reading_outline", "paper_structure"}:
            raise OutlineError("OUTLINE_NOT_FOUND", f"unsupported outline kind: {kind}")
        return service.get_bootstrap(document_id, project_id, kind=kind)

    @app.post("/api/outlines/{outline_id}/edits", tags=["outline"])
    async def apply_edit(outline_id: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {**body, "outlineId": outline_id}
        command = OUTLINE_EDIT_ADAPTER.validate_python(payload)
        return service.apply_edit(command)

    @app.post("/api/outlines/{outline_id}/markdown-preview", tags=["markdown"])
    async def markdown_preview(
        outline_id: str, body: MarkdownPreviewRequest
    ) -> dict[str, Any]:
        return service.markdown_preview(outline_id, body)

    @app.get("/api/outlines/{outline_id}/revisions", tags=["revisions"])
    async def list_revisions(outline_id: str) -> dict[str, Any]:
        return {"items": service.list_revisions(outline_id)}

    @app.get(
        "/api/outlines/{outline_id}/revisions/{revision_no}",
        tags=["revisions"],
    )
    async def get_revision(outline_id: str, revision_no: int) -> dict[str, Any]:
        return service.get_revision(outline_id, revision_no)

    @app.post(
        "/api/outlines/{outline_id}/revisions/{revision_no}/switch",
        tags=["revisions"],
    )
    async def switch_revision(
        outline_id: str,
        revision_no: int,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = body or {}
        mode = str(payload.get("mode") or "view")
        actor = (
            ActorContext.model_validate(payload["actor"])
            if payload.get("actor")
            else None
        )
        return service.switch_revision_view(
            outline_id,
            revision_no,
            mode=mode,
            actor=actor,
        )

    @app.get("/api/outlines/{outline_id}/diff", tags=["revisions"])
    async def diff_revisions(
        outline_id: str,
        left: int = Query(ge=1),
        right: int = Query(ge=1),
    ) -> dict[str, Any]:
        return service.compare_revisions(outline_id, left, right)

    @app.post("/api/outlines/{outline_id}/restore", tags=["revisions"])
    async def restore_revision(
        outline_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        command = RestoreOutlineRevisionCommand.model_validate(
            {**body, "outlineId": outline_id}
        )
        return service.restore_revision(command)

    @app.post("/api/outlines/{outline_id}/ai-runs", tags=["ai"])
    async def request_ai_run(
        outline_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        request = OutlineAIRunRequest.model_validate(
            {**body, "outlineId": outline_id}
        )
        if service.llm_client is not None:
            return service.generate_live(
                outline_id=outline_id,
                base_revision_no=request.base_revision_no,
                include_annotations=request.include_annotations,
                actor=request.actor,
                client_mutation_id=request.client_mutation_id,
            )
        return service.request_ai_run(request)

    @app.get(
        "/api/outlines/{outline_id}/ai-runs/{run_id}",
        tags=["ai"],
    )
    async def get_ai_run(outline_id: str, run_id: str) -> dict[str, Any]:
        with factory() as session:
            from ..storage.v116.orm import OutlineAIRun
            from .service import _ai_run_out

            row = session.get(OutlineAIRun, run_id)
            if row is None or row.outline_id != outline_id:
                raise OutlineError("AI_RUN_NOT_READY", f"AI run not found: {run_id}")
            return _ai_run_out(row)

    @app.post(
        "/api/outlines/{outline_id}/ai-runs/{run_id}/complete",
        tags=["ai"],
    )
    async def complete_ai_run(
        outline_id: str,
        run_id: str,
        body: CompleteOutlineAIRunRequest,
    ) -> dict[str, Any]:
        return service.complete_ai_run(outline_id, run_id, body)

    @app.get("/api/outlines/{outline_id}/merge-preview", tags=["ai"])
    async def merge_preview(
        outline_id: str,
        run_id: str = Query(alias="runId"),
    ) -> dict[str, Any]:
        return service.merge_preview(outline_id, run_id)

    @app.post(
        "/api/outlines/{outline_id}/merge-conflicts/{conflict_id}/resolve",
        tags=["ai"],
    )
    async def resolve_conflict(
        outline_id: str,
        conflict_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        command = ResolveOutlineConflictCommand.model_validate(
            {**body, "conflictId": conflict_id}
        )
        return service.resolve_conflict(outline_id, command)

    @app.post(
        "/api/outlines/{outline_id}/merge-preview/apply",
        tags=["ai"],
    )
    async def apply_merge(
        outline_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        command = ApplyOutlineMergeCommand.model_validate(
            {**body, "outlineId": outline_id}
        )
        return service.apply_merge(command)

    @app.put(
        "/api/projects/{project_id}/outlines/{outline_id}/nodes/{node_id}/completion",
        tags=["progress"],
    )
    async def set_completion(
        project_id: str,
        outline_id: str,
        node_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        actor = ActorContext.model_validate(body.get("actor"))
        command = SetOutlineNodeCompletedCommand(
            projectId=project_id,
            outlineId=outline_id,
            nodeId=node_id,
            completed=bool(body.get("completed")),
            actor=actor,
        )
        return service.set_completed(command)

    @app.post(
        "/api/outlines/{outline_id}/nodes/{node_id}/locate",
        tags=["reader"],
    )
    async def locate_node(
        outline_id: str,
        node_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        actor = ActorContext.model_validate(body.get("actor"))
        return service.node_clicked(outline_id, node_id, actor)

    @app.get("/api/outline-events", tags=["events"])
    async def outline_events(
        after_sequence: int = Query(default=0, ge=0, alias="afterSequence"),
        document_id: str | None = Query(default=None, alias="documentId"),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return {
            "events": service.events_after(
                after_sequence=after_sequence,
                document_id=document_id,
                limit=limit,
            )
        }

    return app

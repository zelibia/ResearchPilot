"""FastAPI transport for the P0-04 annotation backend."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from ..config import PATHS
from ..storage.v116.annotation_schema import fts5_trigram_available
from ..storage.v116.db import deploy_schema, engine_for, session_factory
from .contracts import (
    ActorContext,
    AnnotationSidebarFilter,
    AnnotationSidebarQuery,
    BatchDeleteAnnotationsCommand,
    BatchSetAnnotationColorCommand,
    CreateAnnotationCommand,
    DeleteAnnotationCommand,
    LocateFromSidebarInput,
    RestoreAnnotationCommand,
    UndoBatchCommand,
    UpdateAnnotationCommentCommand,
    UpdateAnnotationStyleCommand,
)
from .errors import AnnotationError, read_only
from .services import AnnotationApplicationService, Authorizer


def _csv(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        result.extend(item.strip() for item in value.split(",") if item.strip())
    return list(dict.fromkeys(result))


def _request_actor(
    request: Request,
    actor_id: str | None,
    request_id: str | None,
) -> ActorContext:
    resolved_actor = (
        actor_id
        or request.headers.get("X-Actor-ID")
        or request.headers.get("X-User-ID")
    )
    resolved_request = (
        request_id
        or request.headers.get("X-Request-ID")
        or f"req_{uuid.uuid4().hex}"
    )
    if not resolved_actor:
        raise read_only("缺少 X-Actor-ID")
    return ActorContext(actorId=resolved_actor, requestId=resolved_request)


def _validation_code(exc: RequestValidationError) -> str:
    fields = " ".join(
        ".".join(str(item) for item in error.get("loc", ()))
        for error in exc.errors()
    ).lower()
    message = "；".join(str(error.get("msg", "")) for error in exc.errors())
    if "color" in fields:
        return "INVALID_COLOR"
    if ".type" in fields or fields.endswith("type"):
        return "INVALID_TYPE"
    if "quote" in fields:
        return "EMPTY_SELECTION"
    if "textsource" in fields or "ocr" in message.lower():
        return "OCR_TEXT_UNAVAILABLE"
    return "ANCHOR_INVALID"


def _selection_from_body(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    draft = payload.get("draft") if isinstance(payload, dict) else None
    if not isinstance(draft, dict):
        return None
    return {
        "preserved": True,
        "documentId": draft.get("documentId"),
        "fileAssetId": draft.get("fileAssetId"),
        "quote": draft.get("quote", ""),
        "selector": draft.get("selector", {}),
        "rects": draft.get("rects", []),
        "draft": draft,
    }


def create_annotation_app(
    *,
    db_path: str | Path | None = None,
    deploy: bool = False,
    reader_mode: str = "pdf_text",
    authorizer: Authorizer | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    engine = engine_for(db_path or PATHS.db_path)
    if deploy:
        deploy_schema(engine, note="P0-04 annotation API")
    factory = session_factory(engine)
    service = AnnotationApplicationService(
        factory,
        reader_mode=reader_mode,
        authorizer=authorizer,
    )
    app = FastAPI(
        title="ResearchPilot Annotation API",
        version="1.16.1-p0-04",
        description=(
            "P0-04 text annotations, comments, sidebar search, anchors, "
            "batch mutations and audit."
        ),
    )
    app.state.engine = engine
    app.state.factory = factory
    app.state.service = service
    configured_origins = cors_origins
    if configured_origins is None:
        raw_origins = os.environ.get("ANNOTATION_CORS_ORIGINS", "*")
        configured_origins = [
            value.strip()
            for value in raw_origins.split(",")
            if value.strip()
        ] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configured_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Actor-ID", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(AnnotationError)
    async def annotation_error_handler(
        _request: Request,
        exc: AnnotationError,
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.as_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        code = _validation_code(exc)
        selection = _selection_from_body(await request.body())
        payload: dict[str, Any] = {
            "code": code,
            "message": "；".join(
                str(error.get("msg", "invalid request")) for error in exc.errors()
            ),
            "details": {"errors": jsonable_encoder(exc.errors())},
        }
        if selection is not None:
            payload["selectionState"] = selection
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def database_error_handler(
        _request: Request,
        _exc: sa.exc.SQLAlchemyError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "数据库操作失败，事务已回滚",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        _request: Request,
        _exc: Exception,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
            },
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "schema": "1.16.1-p0-04",
            "readerMode": service.reader_mode,
            "fts5Trigram": fts5_trigram_available(engine),
        }

    @app.get(
        "/api/documents/{document_id}/files/{file_id}/annotation-capabilities",
        tags=["annotations"],
    )
    async def capabilities(
        document_id: str,
        file_id: str,
        mode: str | None = None,
    ) -> dict[str, Any]:
        with factory() as session:
            service._require_file(session, document_id, file_id)
        return service.capabilities(mode)

    @app.get(
        "/api/documents/{document_id}/files/{file_id}/annotations",
        tags=["annotations"],
    )
    async def list_annotations(
        request: Request,
        document_id: str,
        file_id: str,
        types: list[str] | None = Query(default=None),
        colors: list[str] | None = Query(default=None),
        text_sources: list[str] | None = Query(default=None, alias="textSources"),
        edited_only: bool = Query(default=False, alias="editedOnly"),
        comment_only: bool = Query(default=False, alias="commentOnly"),
        search_query: str = Query(default="", alias="searchQuery"),
        sort: str = "page_asc",
        limit: int = 50,
        cursor: str | None = None,
        actor_id: str | None = Query(default=None, alias="actorId"),
        request_id: str | None = Query(default=None, alias="requestId"),
        mode: str | None = None,
    ) -> dict[str, Any]:
        actor = _request_actor(request, actor_id, request_id)
        query = AnnotationSidebarQuery.model_validate(
            {
                "documentId": document_id,
                "fileAssetId": file_id,
                "filter": AnnotationSidebarFilter(
                    types=_csv(types),
                    colors=_csv(colors),
                    textSources=_csv(text_sources),
                    editedOnly=edited_only,
                    commentOnly=comment_only,
                    searchQuery=search_query,
                ),
                "sort": sort,
                "limit": limit,
                "cursor": cursor,
            }
        )
        return service.list(query, actor, mode=mode)

    @app.post(
        "/api/documents/{document_id}/files/{file_id}/annotations",
        status_code=201,
        tags=["annotations"],
    )
    async def create_annotation(
        document_id: str,
        file_id: str,
        body: CreateAnnotationCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.draft.document_id != document_id:
            raise AnnotationError("DOCUMENT_MISMATCH", "路径 documentId 与 draft 不一致")
        if body.draft.file_asset_id != file_id:
            raise AnnotationError("FILE_MISMATCH", "路径 fileAssetId 与 draft 不一致")
        return service.create(body, mode=mode)

    @app.patch("/api/annotations/{annotation_id}/comment", tags=["annotations"])
    async def update_comment(
        annotation_id: str,
        body: UpdateAnnotationCommentCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.annotation_id and body.annotation_id != annotation_id:
            raise AnnotationError("ANCHOR_INVALID", "路径与请求体 annotationId 不一致")
        body.annotation_id = annotation_id
        return service.update_comment(body, mode=mode)

    @app.patch("/api/annotations/{annotation_id}/style", tags=["annotations"])
    async def update_style(
        annotation_id: str,
        body: UpdateAnnotationStyleCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.annotation_id and body.annotation_id != annotation_id:
            raise AnnotationError("ANCHOR_INVALID", "路径与请求体 annotationId 不一致")
        body.annotation_id = annotation_id
        return service.update_style(body, mode=mode)

    @app.delete("/api/annotations/{annotation_id}", tags=["annotations"])
    async def delete_annotation(
        annotation_id: str,
        body: DeleteAnnotationCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.annotation_id and body.annotation_id != annotation_id:
            raise AnnotationError("ANCHOR_INVALID", "路径与请求体 annotationId 不一致")
        body.annotation_id = annotation_id
        return service.delete(body, mode=mode)

    @app.post("/api/annotations/{annotation_id}/restore", tags=["annotations"])
    async def restore_annotation(
        annotation_id: str,
        body: RestoreAnnotationCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.annotation_id and body.annotation_id != annotation_id:
            raise AnnotationError("ANCHOR_INVALID", "路径与请求体 annotationId 不一致")
        body.annotation_id = annotation_id
        return service.restore(body, mode=mode)

    @app.post(
        "/api/documents/{document_id}/annotations/batch-delete",
        tags=["batch"],
    )
    async def batch_delete(
        document_id: str,
        body: BatchDeleteAnnotationsCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.document_id != document_id:
            raise AnnotationError("DOCUMENT_MISMATCH", "路径 documentId 与请求体不一致")
        return service.batch_delete(body, mode=mode)

    @app.post(
        "/api/documents/{document_id}/annotations/batch-color",
        tags=["batch"],
    )
    async def batch_set_color(
        document_id: str,
        body: BatchSetAnnotationColorCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if body.document_id != document_id:
            raise AnnotationError("DOCUMENT_MISMATCH", "路径 documentId 与请求体不一致")
        return service.batch_set_color(body, mode=mode)

    @app.post("/api/annotation-batches/{batch_id}/undo", tags=["batch"])
    async def undo_batch(
        batch_id: str,
        body: UndoBatchCommand,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return service.undo_batch(batch_id, body, mode=mode)

    @app.get(
        "/api/documents/{document_id}/annotations/search",
        tags=["search"],
    )
    async def search_annotations(
        request: Request,
        document_id: str,
        q: str = Query(min_length=1),
        file_asset_id: str = Query(alias="fileAssetId"),
        limit: int = Query(default=50, ge=1, le=200),
        actor_id: str | None = Query(default=None, alias="actorId"),
        request_id: str | None = Query(default=None, alias="requestId"),
        mode: str | None = None,
    ) -> dict[str, Any]:
        actor = _request_actor(request, actor_id, request_id)
        query = AnnotationSidebarQuery.model_validate(
            {
                "documentId": document_id,
                "fileAssetId": file_asset_id,
                "filter": {"searchQuery": q},
                "sort": "page_asc",
                "limit": limit,
            }
        )
        return service.list(query, actor, mode=mode)

    @app.get("/api/annotations/{annotation_id}/anchor", tags=["reader"])
    async def reader_anchor(
        request: Request,
        annotation_id: str,
        actor_id: str | None = Query(default=None, alias="actorId"),
        request_id: str | None = Query(default=None, alias="requestId"),
    ) -> dict[str, Any]:
        actor = _request_actor(request, actor_id, request_id)
        return service.reader_anchor(annotation_id, actor)

    @app.post("/api/annotations/{annotation_id}/locate", tags=["reader"])
    async def locate_from_sidebar(
        request: Request,
        annotation_id: str,
        body: dict[str, Any] | None = None,
        document_id: str | None = Query(default=None, alias="documentId"),
        file_asset_id: str | None = Query(default=None, alias="fileAssetId"),
        actor_id: str | None = Query(default=None, alias="actorId"),
        request_id: str | None = Query(default=None, alias="requestId"),
        mode: str | None = None,
    ) -> dict[str, Any]:
        payload = body or {}
        actor = _request_actor(
            request,
            actor_id or str(payload.get("actorId") or ""),
            request_id,
        )
        with factory() as session:
            row = service._require_annotation(session, annotation_id)
            resolved_document = document_id or str(
                payload.get("documentId") or row.document_id
            )
            resolved_file = file_asset_id or str(
                payload.get("fileAssetId") or row.file_id
            )
        locate_input = LocateFromSidebarInput(
            annotationId=annotation_id,
            documentId=resolved_document,
            fileAssetId=resolved_file,
        )
        return service.locate_from_sidebar(locate_input, actor, mode=mode)

    @app.get("/api/annotations/events", tags=["events"])
    async def annotation_events(
        after_sequence: int = Query(default=0, ge=0, alias="afterSequence"),
        document_id: str | None = Query(default=None, alias="documentId"),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        events = service.events_after(
            after_sequence=after_sequence,
            document_id=document_id,
            limit=limit,
        )
        return {"events": events}

    @app.get("/api/annotations/{annotation_id}/audit", tags=["audit"])
    async def annotation_audit(
        request: Request,
        annotation_id: str,
        actor_id: str | None = Query(default=None, alias="actorId"),
        request_id: str | None = Query(default=None, alias="requestId"),
    ) -> dict[str, Any]:
        actor = _request_actor(request, actor_id, request_id)
        return {"items": service.audit_trail(annotation_id, actor)}

    return app

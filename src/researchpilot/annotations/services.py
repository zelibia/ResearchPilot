"""P0-04 annotation, comment, sidebar, batch and audit application service."""

from __future__ import annotations

import base64
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..storage.v116.contracts import annotation_out
from ..storage.v116.db import session_scope
from ..storage.v116.orm import (
    Annotation,
    AnnotationAudit,
    AnnotationMutationReceipt,
    AnnotationOperationBatch,
    AnnotationUndoToken,
    FileAsset,
)
from .anchors import anchor_resolve_result, annotation_to_reader_anchor
from .contracts import (
    ActorContext,
    AnnotationModeCapabilities,
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
from .errors import (
    AnnotationError,
    conflict_error,
    internal_error,
    invalid_anchor,
    invalid_color,
    not_found,
    read_only,
)
from .events import (
    canonical_request_hash,
    emit_event,
    event_from_row,
    list_events,
    utc_now_iso,
)
from .validation import (
    cross_page,
    ensure_editable,
    normalize_color,
    normalized_selector,
    primary_text_hash,
    primary_text_source,
    require_text_type,
)

UNDO_WINDOW_SECONDS = 10


Authorizer = Callable[[ActorContext, str, str, str, Annotation | None], bool]


def _default_authorizer(
    _actor: ActorContext,
    _action: str,
    _document_id: str,
    _file_id: str,
    _annotation: Annotation | None,
) -> bool:
    return True


def _parse_utc(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _selection_state(command: CreateAnnotationCommand) -> dict[str, Any]:
    draft = command.draft
    return {
        "preserved": True,
        "documentId": draft.document_id,
        "fileAssetId": draft.file_asset_id,
        "quote": draft.quote,
        "selector": draft.selector.model_dump(by_alias=True),
        "rects": [item.model_dump(by_alias=True) for item in draft.rects],
        "draft": draft.model_dump(by_alias=True),
    }


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:
        raise invalid_anchor("侧栏 cursor 无效") from exc


class AnnotationApplicationService:
    """Application service matching the P0-04 TypeScript interface."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        reader_mode: str = "pdf_text",
        authorizer: Authorizer | None = None,
    ) -> None:
        if reader_mode not in {"pdf_text", "ocr_text", "png_fallback"}:
            raise ValueError(f"unsupported reader mode: {reader_mode}")
        self.factory = factory
        self.reader_mode = reader_mode
        self.authorizer = authorizer or _default_authorizer

    # ------------------------------------------------------------------
    # Capabilities and authorization
    # ------------------------------------------------------------------

    def capabilities(self, mode: str | None = None) -> dict[str, Any]:
        effective_mode = self._mode(mode)
        writable = effective_mode != "png_fallback"
        return AnnotationModeCapabilities(
            mode=effective_mode,
            canCreateTextAnnotation=writable,
            canEditAnnotation=writable,
            canDeleteAnnotation=writable,
            canComment=writable,
            canSearchSidebar=True,
            canLocateFromSidebar=True,
            showsTextSourceBadge=effective_mode in {"ocr_text", "png_fallback"},
        ).model_dump(by_alias=True)

    def _mode(self, override: str | None) -> str:
        mode = override or self.reader_mode
        if mode not in {"pdf_text", "ocr_text", "png_fallback"}:
            raise AnnotationError("INTERNAL_ERROR", f"unknown reader mode: {mode}")
        return mode

    def _authorize(
        self,
        actor: ActorContext,
        action: str,
        document_id: str,
        file_id: str,
        annotation: Annotation | None = None,
    ) -> None:
        if not self.authorizer(actor, action, document_id, file_id, annotation):
            raise read_only("当前操作人没有访问该批注的权限")

    @staticmethod
    def _require_file(
        session: Session,
        document_id: str,
        file_id: str,
    ) -> FileAsset:
        file_asset = session.get(FileAsset, file_id)
        if file_asset is None or file_asset.deleted_at is not None:
            raise AnnotationError("FILE_MISMATCH", f"file asset not found: {file_id}")
        if file_asset.document_id != document_id:
            raise AnnotationError(
                "FILE_MISMATCH",
                "fileAssetId 不属于指定 documentId",
            )
        return file_asset

    @staticmethod
    def _require_annotation(session: Session, annotation_id: str) -> Annotation:
        row = session.get(Annotation, annotation_id)
        if row is None:
            raise not_found(annotation_id)
        return row

    @staticmethod
    def _check_scope(
        row: Annotation,
        document_id: str,
        file_id: str,
    ) -> None:
        if row.document_id != document_id:
            raise AnnotationError("DOCUMENT_MISMATCH", "批注不属于指定文档")
        if row.file_id != file_id:
            raise AnnotationError("FILE_MISMATCH", "批注不属于指定文件")

    @staticmethod
    def _check_revision(row: Annotation, base_revision: int) -> None:
        if row.revision != base_revision:
            raise conflict_error(annotation_out(row))

    def _receipt(
        self,
        session: Session,
        client_mutation_id: str,
        action: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        receipt = session.get(AnnotationMutationReceipt, client_mutation_id)
        if receipt is None:
            return None
        if receipt.action != action or receipt.request_hash != request_hash:
            raise AnnotationError(
                "REVISION_CONFLICT",
                "clientMutationId 已被不同的请求使用",
            )
        return copy.deepcopy(receipt.response_json)

    @staticmethod
    def _save_receipt(
        session: Session,
        *,
        client_mutation_id: str,
        actor: ActorContext,
        action: str,
        request_hash: str,
        response: dict[str, Any],
        annotation_id: str = "",
        batch_id: str | None = None,
        event_id: str = "",
    ) -> None:
        session.add(
            AnnotationMutationReceipt(
                client_mutation_id=client_mutation_id,
                actor_id=actor.actor_id,
                action=action,
                request_hash=request_hash,
                annotation_id=annotation_id,
                batch_id=batch_id,
                response_json=copy.deepcopy(response),
                event_id=event_id,
                created_at=utc_now_iso(),
            )
        )
        session.flush()

    # ------------------------------------------------------------------
    # Create / comments / style
    # ------------------------------------------------------------------

    def create(
        self,
        command: CreateAnnotationCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if actor is not None and actor.actor_id != command.actor.actor_id:
            raise read_only("actor 上下文不一致")
        effective_mode = self._mode(mode)
        if effective_mode == "png_fallback":
            raise AnnotationError(
                "TEXT_LAYER_UNAVAILABLE",
                "PNG 降级模式只允许读取已有批注",
                selection_state=_selection_state(command),
            )

        try:
            return self._create(command)
        except AnnotationError as exc:
            if exc.selection_state is None:
                raise AnnotationError(
                    exc.code,
                    exc.message,
                    current=exc.current,
                    selection_state=_selection_state(command),
                    details=exc.details,
                ) from exc
            raise
        except sa.exc.SQLAlchemyError as exc:
            raise internal_error(
                "标注写入失败，事务已回滚",
                selection_state=_selection_state(command),
            ) from exc

    def _create(self, command: CreateAnnotationCommand) -> dict[str, Any]:
        draft = command.draft
        require_text_type(draft.type)
        color = normalize_color(draft.color)
        selector = normalized_selector(draft)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))

        try:
            with session_scope(self.factory) as session:
                replayed = self._receipt(
                    session,
                    command.client_mutation_id,
                    "create",
                    request_hash,
                )
                if replayed is not None:
                    return replayed

                file_asset = self._require_file(
                    session,
                    draft.document_id,
                    draft.file_asset_id,
                )
                if file_asset.text_layer == "missing" and draft.anchor_status != "no_text_layer":
                    raise AnnotationError(
                        "TEXT_LAYER_UNAVAILABLE",
                        "文件没有文字层，不能创建文本锚点",
                    )
                self._authorize(
                    command.actor,
                    "create",
                    draft.document_id,
                    draft.file_asset_id,
                )

                now = utc_now_iso()
                row = Annotation(
                    id=f"ann_{uuid.uuid4().hex}",
                    document_id=draft.document_id,
                    file_id=draft.file_asset_id,
                    type=draft.type,
                    quote=draft.quote,
                    comment_markdown=draft.comment_markdown,
                    color=color,
                    page_index=draft.page_index,
                    page_label=draft.page_label,
                    text_source=primary_text_source(selector),
                    text_hash=primary_text_hash(selector),
                    char_start=draft.selector.char_start,
                    char_end=(
                        None
                        if cross_page(selector)
                        else draft.selector.char_end
                    ),
                    rects_json=[item.model_dump(by_alias=True) for item in draft.rects],
                    selector_json=selector,
                    sort_index=draft.sort_index,
                    anchor_status=draft.anchor_status,
                    author_id=command.actor.actor_id,
                    updated_by=command.actor.actor_id,
                    deleted_by="",
                    source="user",
                    read_only=False,
                    external_id="",
                    revision=1,
                    legacy_payload_json={},
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                )
                session.add(row)
                session.flush()
                after = annotation_out(row)
                self._audit(
                    session,
                    row,
                    action="created",
                    actor=command.actor,
                    client_mutation_id=command.client_mutation_id,
                    before={},
                    after=after,
                )
                # The TS command intentionally has no separate selectionId.
                # clientMutationId is stable across optimistic retries and is
                # therefore the only deterministic selection identifier.
                selection_id = command.client_mutation_id
                event = emit_event(
                    session,
                    event_type="annotation_created",
                    document_id=row.document_id,
                    file_asset_id=row.file_id,
                    payload={
                        "annotation": after,
                        "selectionId": selection_id,
                    },
                )
                response = {
                    "annotation": after,
                    "revision": row.revision,
                    "undoToken": None,
                }
                self._save_receipt(
                    session,
                    client_mutation_id=command.client_mutation_id,
                    actor=command.actor,
                    action="create",
                    request_hash=request_hash,
                    response=response,
                    annotation_id=row.id,
                    event_id=event.event_id,
                )
                return response
        except Exception as exc:
            if isinstance(exc, AnnotationError):
                raise
            raise internal_error(
                "标注写入失败，事务已回滚",
                selection_state=_selection_state(command),
            ) from exc

    def update_comment(
        self,
        command: UpdateAnnotationCommentCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "comment_update",
                request_hash,
            )
            if replayed is not None:
                return replayed
            row = self._require_annotation(session, command.annotation_id)
            self._authorize(
                command.actor,
                "update_comment",
                row.document_id,
                row.file_id,
                row,
            )
            ensure_editable(row.read_only, row.source)
            self._check_revision(row, command.base_revision)
            if row.deleted_at is not None:
                raise read_only("已删除批注不能编辑评论")
            before = copy.deepcopy(annotation_out(row))
            changed = row.comment_markdown != command.comment_markdown
            if changed:
                row.comment_markdown = command.comment_markdown
                row.updated_by = command.actor.actor_id
                row.updated_at = utc_now_iso()
                row.revision += 1
                session.flush()
                after = annotation_out(row)
                self._audit(
                    session,
                    row,
                    action="comment_updated",
                    actor=command.actor,
                    client_mutation_id=command.client_mutation_id,
                    before=before,
                    after=after,
                )
                event = emit_event(
                    session,
                    event_type="annotation_updated",
                    document_id=row.document_id,
                    file_asset_id=row.file_id,
                    payload={
                        "annotation": after,
                        "changedFields": ["comment"],
                    },
                )
                event_id = event.event_id
            else:
                after = annotation_out(row)
                event_id = ""
            response = {
                "annotation": after,
                "revision": row.revision,
                "undoToken": None,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="comment_update",
                request_hash=request_hash,
                response=response,
                annotation_id=row.id,
                event_id=event_id,
            )
            return response

    def update_style(
        self,
        command: UpdateAnnotationStyleCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "style_update",
                request_hash,
            )
            if replayed is not None:
                return replayed
            row = self._require_annotation(session, command.annotation_id)
            self._authorize(
                command.actor,
                "update_style",
                row.document_id,
                row.file_id,
                row,
            )
            ensure_editable(row.read_only, row.source)
            self._check_revision(row, command.base_revision)
            if row.deleted_at is not None:
                raise read_only("已删除批注不能修改样式")
            before = copy.deepcopy(annotation_out(row))
            changed_fields: list[str] = []
            if command.patch.type is not None:
                new_type = require_text_type(command.patch.type)
                if row.type != new_type:
                    row.type = new_type
                    changed_fields.append("type")
            if command.patch.color is not None:
                new_color = normalize_color(command.patch.color)
                if row.color != new_color:
                    row.color = new_color
                    changed_fields.append("color")
            if changed_fields:
                row.updated_by = command.actor.actor_id
                row.updated_at = utc_now_iso()
                row.revision += 1
                session.flush()
                after = annotation_out(row)
                self._audit(
                    session,
                    row,
                    action="style_updated",
                    actor=command.actor,
                    client_mutation_id=command.client_mutation_id,
                    before=before,
                    after=after,
                )
                event = emit_event(
                    session,
                    event_type="annotation_updated",
                    document_id=row.document_id,
                    file_asset_id=row.file_id,
                    payload={
                        "annotation": after,
                        "changedFields": changed_fields,
                    },
                )
                event_id = event.event_id
            else:
                after = annotation_out(row)
                event_id = ""
            response = {
                "annotation": after,
                "revision": row.revision,
                "undoToken": None,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="style_update",
                request_hash=request_hash,
                response=response,
                annotation_id=row.id,
                event_id=event_id,
            )
            return response

    # ------------------------------------------------------------------
    # Delete / restore
    # ------------------------------------------------------------------

    def delete(
        self,
        command: DeleteAnnotationCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "delete",
                request_hash,
            )
            if replayed is not None:
                return replayed
            row = self._require_annotation(session, command.annotation_id)
            self._authorize(
                command.actor,
                "delete",
                row.document_id,
                row.file_id,
                row,
            )
            ensure_editable(row.read_only, row.source)
            self._check_revision(row, command.base_revision)
            if row.deleted_at is not None:
                raise read_only("批注已经删除")
            before = copy.deepcopy(annotation_out(row))
            deleted_at = utc_now_iso()
            row.deleted_at = deleted_at
            row.deleted_by = command.actor.actor_id
            row.updated_by = command.actor.actor_id
            row.updated_at = deleted_at
            row.revision += 1
            session.flush()
            after = annotation_out(row)
            undo_token = f"undo_{uuid.uuid4().hex}"
            session.add(
                AnnotationUndoToken(
                    token=undo_token,
                    annotation_id=row.id,
                    document_id=row.document_id,
                    file_id=row.file_id,
                    actor_id=command.actor.actor_id,
                    issued_at=deleted_at,
                    expires_at=(
                        _parse_utc(deleted_at)
                        + timedelta(seconds=UNDO_WINDOW_SECONDS)
                    ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                )
            )
            self._audit(
                session,
                row,
                action="deleted",
                actor=command.actor,
                client_mutation_id=command.client_mutation_id,
                before=before,
                after=after,
            )
            event = emit_event(
                session,
                event_type="annotation_deleted",
                document_id=row.document_id,
                file_asset_id=row.file_id,
                payload={
                    "annotationId": row.id,
                    "deletedAt": deleted_at,
                },
            )
            response = {
                "annotation": after,
                "revision": row.revision,
                "undoToken": undo_token,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="delete",
                request_hash=request_hash,
                response=response,
                annotation_id=row.id,
                event_id=event.event_id,
            )
            return response

    def restore(
        self,
        command: RestoreAnnotationCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
        enforce_undo_window: bool = True,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "restore",
                request_hash,
            )
            if replayed is not None:
                return replayed
            row = self._require_annotation(session, command.annotation_id)
            self._authorize(
                command.actor,
                "restore",
                row.document_id,
                row.file_id,
                row,
            )
            self._check_revision(row, command.base_revision)
            if row.deleted_at is None:
                raise read_only("批注未被删除，无需恢复")
            token = self._undo_token(session, row, command.undo_token)
            if enforce_undo_window:
                if _parse_utc(token.expires_at) < datetime.now(timezone.utc):
                    raise read_only("10 秒撤销窗口已结束")
            before = copy.deepcopy(annotation_out(row))
            row.deleted_at = None
            row.deleted_by = ""
            row.updated_by = command.actor.actor_id
            row.updated_at = utc_now_iso()
            row.revision += 1
            token.used_at = utc_now_iso()
            session.flush()
            after = annotation_out(row)
            self._audit(
                session,
                row,
                action="restored",
                actor=command.actor,
                client_mutation_id=command.client_mutation_id,
                before=before,
                after=after,
            )
            event = emit_event(
                session,
                event_type="annotation_restored",
                document_id=row.document_id,
                file_asset_id=row.file_id,
                payload={"annotation": after},
            )
            response = {
                "annotation": after,
                "revision": row.revision,
                "undoToken": None,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="restore",
                request_hash=request_hash,
                response=response,
                annotation_id=row.id,
                event_id=event.event_id,
            )
            return response

    @staticmethod
    def _undo_token(
        session: Session,
        row: Annotation,
        requested: str | None,
    ) -> AnnotationUndoToken:
        statement = sa.select(AnnotationUndoToken).where(
            AnnotationUndoToken.annotation_id == row.id
        )
        if requested:
            statement = statement.where(AnnotationUndoToken.token == requested)
        else:
            statement = statement.where(AnnotationUndoToken.used_at.is_(None))
        token = session.scalar(statement.order_by(AnnotationUndoToken.issued_at.desc()))
        if token is None:
            raise read_only("撤销令牌无效或已使用")
        if token.used_at is not None:
            raise read_only("撤销令牌已使用")
        return token

    # ------------------------------------------------------------------
    # Sidebar query and full-text search
    # ------------------------------------------------------------------

    def list(
        self,
        query: AnnotationSidebarQuery,
        actor: ActorContext,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        effective_mode = self._mode(mode)
        with session_scope(self.factory) as session:
            self._require_file(session, query.document_id, query.file_asset_id)
            self._authorize(
                actor,
                "read",
                query.document_id,
                query.file_asset_id,
            )
            base_conditions = [
                Annotation.document_id == query.document_id,
                Annotation.file_id == query.file_asset_id,
                Annotation.deleted_at.is_(None),
            ]
            conditions = list(base_conditions)
            filters = query.filter
            if filters.types:
                conditions.append(Annotation.type.in_(filters.types))
            if filters.colors:
                conditions.append(Annotation.color.in_(filters.colors))
            if filters.text_sources:
                conditions.append(Annotation.text_source.in_(filters.text_sources))
            if filters.edited_only:
                conditions.append(Annotation.revision > 1)
            if filters.comment_only:
                conditions.append(sa.func.length(Annotation.comment_markdown) > 0)

            ranks: dict[str, int] = {}
            if filters.search_query.strip():
                ranked_ids = self._search_ids(
                    session,
                    query.document_id,
                    query.file_asset_id,
                    filters.search_query.strip(),
                )
                ranks = {annotation_id: index for index, annotation_id in enumerate(ranked_ids)}
                conditions.append(Annotation.id.in_(ranked_ids))

            statement = sa.select(Annotation).where(*conditions)
            if ranks:
                rank_order = sa.case(ranks, value=Annotation.id, else_=len(ranks))
                if query.sort == "page_asc":
                    statement = statement.order_by(
                        rank_order,
                        Annotation.page_index,
                        Annotation.sort_index,
                        Annotation.id,
                    )
                else:
                    statement = statement.order_by(
                        rank_order,
                        *self._sort_columns(query.sort),
                    )
            else:
                statement = statement.order_by(*self._sort_columns(query.sort))

            total = session.scalar(
                sa.select(sa.func.count()).select_from(statement.subquery())
            )
            cursor = _decode_cursor(query.cursor)
            cursor_filter = cursor.get("filter")
            expected_filter = canonical_request_hash(
                {
                    "documentId": query.document_id,
                    "fileAssetId": query.file_asset_id,
                    "filter": filters.model_dump(by_alias=True),
                    "sort": query.sort,
                }
            )
            if cursor_filter and cursor_filter != expected_filter:
                raise invalid_anchor("cursor 与当前筛选条件不匹配")
            offset = int(cursor.get("offset", 0))
            if offset < 0:
                raise invalid_anchor("cursor offset 无效")
            rows = session.scalars(statement.offset(offset).limit(query.limit)).all()
            next_cursor = None
            if offset + len(rows) < int(total or 0):
                next_cursor = _encode_cursor(
                    {"offset": offset + len(rows), "filter": expected_filter}
                )
            counts = self._filter_counts(
                session,
                query.document_id,
                query.file_asset_id,
                base_conditions,
            )
            items = [
                self._sidebar_item(
                    row,
                    editable=(
                        effective_mode != "png_fallback"
                        and not row.read_only
                        and row.source != "external_pdf"
                    ),
                )
                for row in rows
            ]
            return {
                "items": items,
                "nextCursor": next_cursor,
                "resultCount": int(total or 0),
                "filterCounts": counts,
            }

    @staticmethod
    def _sort_columns(sort: str) -> tuple[Any, ...]:
        if sort == "updated_desc":
            return (
                Annotation.updated_at.desc(),
                Annotation.page_index,
                Annotation.sort_index,
                Annotation.id,
            )
        if sort == "created_desc":
            return (
                Annotation.created_at.desc(),
                Annotation.page_index,
                Annotation.sort_index,
                Annotation.id,
            )
        return (
            Annotation.page_index,
            Annotation.sort_index,
            Annotation.id,
        )

    @staticmethod
    def _filter_counts(
        session: Session,
        document_id: str,
        file_id: str,
        conditions: list[Any],
    ) -> dict[str, Any]:
        by_type = {
            str(kind): int(count)
            for kind, count in session.execute(
                sa.select(Annotation.type, sa.func.count())
                .where(*conditions)
                .group_by(Annotation.type)
            )
        }
        by_color = {
            str(color): int(count)
            for color, count in session.execute(
                sa.select(Annotation.color, sa.func.count())
                .where(*conditions)
                .group_by(Annotation.color)
            )
        }
        ocr = session.scalar(
            sa.select(sa.func.count()).where(
                *conditions,
                Annotation.text_source == "ocr",
            )
        )
        return {
            "byType": {
                key: value
                for key, value in by_type.items()
                if key in {"highlight", "underline", "strikeout"}
            },
            "byColor": by_color,
            "ocr": int(ocr or 0),
        }

    @staticmethod
    def _search_ids(
        session: Session,
        document_id: str,
        file_id: str,
        query: str,
    ) -> list[str]:
        if len(query.strip()) >= 3:
            safe_query = '"' + query.replace('"', '""') + '"'
            try:
                rows = session.execute(
                    sa.text(
                        """
                        SELECT annotation_id
                        FROM annotation_fts
                        WHERE document_id = :document_id
                          AND annotation_fts MATCH :query
                        ORDER BY bm25(annotation_fts), rowid
                        """
                    ),
                    {"document_id": document_id, "query": safe_query},
                )
                ids = [str(row[0]) for row in rows]
                if ids:
                    allowed = set(
                        session.scalars(
                            sa.select(Annotation.id).where(
                                Annotation.id.in_(ids),
                                Annotation.document_id == document_id,
                                Annotation.file_id == file_id,
                                Annotation.deleted_at.is_(None),
                            )
                        ).all()
                    )
                    return [item for item in ids if item in allowed]
                # A no-match FTS result is authoritative.
                return []
            except sa.exc.DatabaseError:
                pass
        pattern = f"%{query}%"
        return list(
            session.scalars(
                sa.select(Annotation.id)
                .where(
                    Annotation.document_id == document_id,
                    Annotation.file_id == file_id,
                    Annotation.deleted_at.is_(None),
                    sa.or_(
                        Annotation.quote.ilike(pattern),
                        Annotation.comment_markdown.ilike(pattern),
                    ),
                )
                .order_by(Annotation.page_index, Annotation.sort_index)
            ).all()
        )

    def search(
        self,
        document_id: str,
        file_id: str,
        query: str,
        actor: ActorContext,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        sidebar_query = AnnotationSidebarQuery.model_validate(
            {
                "documentId": document_id,
                "fileAssetId": file_id,
                "filter": {"searchQuery": query},
                "sort": "page_asc",
                "limit": limit,
            }
        )
        return self.list(sidebar_query, actor)

    @staticmethod
    def _sidebar_item(row: Annotation, *, editable: bool) -> dict[str, Any]:
        quote = row.quote.strip()
        comment = row.comment_markdown.strip()
        return {
            "annotation": annotation_out(row),
            "pageLabel": row.page_label or str(row.page_index + 1),
            "quotePreview": quote[:180],
            "commentPreview": comment[:240],
            "textSource": row.text_source,
            "anchorStatus": row.anchor_status,
            "editable": editable,
            "selected": False,
        }

    # ------------------------------------------------------------------
    # Batch mutations and undo
    # ------------------------------------------------------------------

    def batch_delete(
        self,
        command: BatchDeleteAnnotationsCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return self._batch_delete(command, actor, mode)

    def _batch_delete(
        self,
        command: BatchDeleteAnnotationsCommand,
        actor: ActorContext | None,
        mode: str | None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        batch_id = f"abatch_{uuid.uuid4().hex}"
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "batch_delete",
                request_hash,
            )
            if replayed is not None:
                return replayed
            target_file = self._resolve_batch_file(session, command)
            batch = AnnotationOperationBatch(
                id=batch_id,
                document_id=command.document_id,
                file_asset_id=target_file,
                action="batch_delete",
                target_ids_json=list(command.annotation_ids),
                filter_json={},
                before_json={},
                actor_id=command.actor.actor_id,
                client_mutation_id=command.client_mutation_id,
                affected_count=0,
                created_at=utc_now_iso(),
            )
            session.add(batch)
            session.flush()
            affected: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            snapshots: dict[str, Any] = {}
            for annotation_id in command.annotation_ids:
                try:
                    with session.begin_nested():
                        row = self._require_annotation(session, annotation_id)
                        self._check_scope(row, command.document_id, target_file)
                        self._authorize(
                            command.actor,
                            "batch_delete",
                            row.document_id,
                            row.file_id,
                            row,
                        )
                        ensure_editable(row.read_only, row.source)
                        expected = command.base_revisions.get(annotation_id)
                        if expected is None:
                            expected = command.base_revision_no
                        if expected is not None:
                            self._check_revision(row, expected)
                        if row.deleted_at is not None:
                            raise read_only("批注已经删除")
                        before = copy.deepcopy(annotation_out(row))
                        deleted_at = utc_now_iso()
                        row.deleted_at = deleted_at
                        row.deleted_by = command.actor.actor_id
                        row.updated_by = command.actor.actor_id
                        row.updated_at = deleted_at
                        row.revision += 1
                        session.flush()
                        after = annotation_out(row)
                        undo_token = f"undo_{uuid.uuid4().hex}"
                        session.add(
                            AnnotationUndoToken(
                                token=undo_token,
                                annotation_id=row.id,
                                document_id=row.document_id,
                                file_id=row.file_id,
                                actor_id=command.actor.actor_id,
                                issued_at=deleted_at,
                                expires_at=(
                                    _parse_utc(deleted_at)
                                    + timedelta(seconds=UNDO_WINDOW_SECONDS)
                                ).isoformat(timespec="milliseconds").replace(
                                    "+00:00", "Z"
                                ),
                            )
                        )
                        self._audit(
                            session,
                            row,
                            action="batch_delete",
                            actor=command.actor,
                            client_mutation_id=command.client_mutation_id,
                            before=before,
                            after=after,
                            batch_id=batch_id,
                        )
                        affected.append(
                            {
                                "annotation": after,
                                "revision": row.revision,
                                "undoToken": undo_token,
                            }
                        )
                        snapshots[row.id] = before
                except AnnotationError as exc:
                    failed.append(
                        {
                            "annotationId": annotation_id,
                            "code": exc.code,
                            "message": exc.message,
                        }
                    )
            batch.affected_count = len(affected)
            batch.before_json = snapshots
            event_id = ""
            if affected:
                event = emit_event(
                    session,
                    event_type="annotation_batch_changed",
                    document_id=command.document_id,
                    file_asset_id=target_file,
                    payload={
                        "batchId": batch_id,
                        "action": "delete",
                        "annotationIds": [
                            item["annotation"]["id"] for item in affected
                        ],
                    },
                )
                event_id = event.event_id
            response = {
                "batchId": batch_id,
                "affected": affected,
                "failed": failed,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="batch_delete",
                request_hash=request_hash,
                response=response,
                batch_id=batch_id,
                event_id=event_id,
            )
            return response

    def batch_set_color(
        self,
        command: BatchSetAnnotationColorCommand,
        actor: ActorContext | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        command = self._with_actor(command, actor)
        color = normalize_color(command.color)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        batch_id = f"abatch_{uuid.uuid4().hex}"
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "batch_set_color",
                request_hash,
            )
            if replayed is not None:
                return replayed
            target_file = self._resolve_batch_file(session, command)
            batch = AnnotationOperationBatch(
                id=batch_id,
                document_id=command.document_id,
                file_asset_id=target_file,
                action="batch_set_color",
                target_ids_json=list(command.annotation_ids),
                filter_json={"color": color},
                before_json={},
                actor_id=command.actor.actor_id,
                client_mutation_id=command.client_mutation_id,
                affected_count=0,
                created_at=utc_now_iso(),
            )
            session.add(batch)
            session.flush()
            affected: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            snapshots: dict[str, Any] = {}
            for annotation_id in command.annotation_ids:
                try:
                    with session.begin_nested():
                        row = self._require_annotation(session, annotation_id)
                        self._check_scope(row, command.document_id, target_file)
                        self._authorize(
                            command.actor,
                            "batch_set_color",
                            row.document_id,
                            row.file_id,
                            row,
                        )
                        ensure_editable(row.read_only, row.source)
                        expected = command.base_revisions.get(annotation_id)
                        if expected is None:
                            expected = command.base_revision_no
                        if expected is not None:
                            self._check_revision(row, expected)
                        if row.deleted_at is not None:
                            raise read_only("已删除批注不能改色")
                        before = copy.deepcopy(annotation_out(row))
                        changed = row.color != color
                        if changed:
                            row.color = color
                            row.updated_by = command.actor.actor_id
                            row.updated_at = utc_now_iso()
                            row.revision += 1
                            session.flush()
                        after = annotation_out(row)
                        if changed:
                            self._audit(
                                session,
                                row,
                                action="batch_set_color",
                                actor=command.actor,
                                client_mutation_id=command.client_mutation_id,
                                before=before,
                                after=after,
                                batch_id=batch_id,
                            )
                        affected.append(
                            {
                                "annotation": after,
                                "revision": row.revision,
                                "undoToken": None,
                            }
                        )
                        snapshots[row.id] = before
                except AnnotationError as exc:
                    failed.append(
                        {
                            "annotationId": annotation_id,
                            "code": exc.code,
                            "message": exc.message,
                        }
                    )
            batch.affected_count = len(affected)
            batch.before_json = snapshots
            event_id = ""
            if affected:
                event = emit_event(
                    session,
                    event_type="annotation_batch_changed",
                    document_id=command.document_id,
                    file_asset_id=target_file,
                    payload={
                        "batchId": batch_id,
                        "action": "set_color",
                        "annotationIds": [
                            item["annotation"]["id"] for item in affected
                        ],
                    },
                )
                event_id = event.event_id
            response = {
                "batchId": batch_id,
                "affected": affected,
                "failed": failed,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="batch_set_color",
                request_hash=request_hash,
                response=response,
                batch_id=batch_id,
                event_id=event_id,
            )
            return response

    @staticmethod
    def _resolve_batch_file(
        session: Session,
        command: BatchDeleteAnnotationsCommand | BatchSetAnnotationColorCommand,
    ) -> str:
        if command.file_asset_id:
            file_asset = AnnotationApplicationService._require_file(
                session,
                command.document_id,
                command.file_asset_id,
            )
            return file_asset.id
        first = session.get(Annotation, command.annotation_ids[0])
        if first is None:
            raise not_found(command.annotation_ids[0])
        file_asset = AnnotationApplicationService._require_file(
            session,
            command.document_id,
            first.file_id,
        )
        return file_asset.id

    def undo_batch(
        self,
        batch_id: str,
        command: UndoBatchCommand,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_writable(mode)
        request_hash = canonical_request_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "batch_undo",
                request_hash,
            )
            if replayed is not None:
                return replayed
            batch = session.get(AnnotationOperationBatch, batch_id)
            if batch is None:
                raise not_found(batch_id)
            if batch.undone_at is not None:
                raise read_only("该批量操作已经撤销")
            if _parse_utc(batch.created_at) + timedelta(
                seconds=UNDO_WINDOW_SECONDS
            ) < datetime.now(timezone.utc):
                raise read_only("10 秒撤销窗口已结束")
            self._authorize(
                command.actor,
                "batch_undo",
                batch.document_id,
                batch.file_asset_id,
            )
            affected: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            action = (
                "delete"
                if batch.action == "batch_delete"
                else "set_color"
            )
            for annotation_id in batch.target_ids_json:
                try:
                    with session.begin_nested():
                        row = self._require_annotation(session, annotation_id)
                        self._check_scope(
                            row,
                            batch.document_id,
                            batch.file_asset_id,
                        )
                        before = copy.deepcopy(annotation_out(row))
                        restored = False
                        if batch.action == "batch_delete":
                            if row.deleted_at is None:
                                raise read_only("批注已处于未删除状态")
                            row.deleted_at = None
                            row.deleted_by = ""
                            self._undo_token(session, row, None).used_at = utc_now_iso()
                            restored = True
                        elif batch.action == "batch_set_color":
                            original = batch.before_json.get(annotation_id, {})
                            if not original:
                                raise invalid_anchor("批量撤销缺少原始快照")
                            if row.color != original.get("color"):
                                row.color = str(original["color"])
                                restored = True
                        if restored:
                            row.updated_by = command.actor.actor_id
                            row.updated_at = utc_now_iso()
                            row.revision += 1
                            session.flush()
                            after = annotation_out(row)
                            self._audit(
                                session,
                                row,
                                action=(
                                    "batch_undo_delete"
                                    if batch.action == "batch_delete"
                                    else "batch_undo_color"
                                ),
                                actor=command.actor,
                                client_mutation_id=command.client_mutation_id,
                                before=before,
                                after=after,
                                batch_id=batch_id,
                            )
                            if batch.action == "batch_delete":
                                emit_event(
                                    session,
                                    event_type="annotation_restored",
                                    document_id=row.document_id,
                                    file_asset_id=row.file_id,
                                    payload={"annotation": after},
                                )
                        else:
                            after = annotation_out(row)
                        affected.append(
                            {
                                "annotation": after,
                                "revision": row.revision,
                                "undoToken": None,
                            }
                        )
                except AnnotationError as exc:
                    failed.append(
                        {
                            "annotationId": annotation_id,
                            "code": exc.code,
                            "message": exc.message,
                        }
                    )
            batch.undone_at = utc_now_iso()
            event = emit_event(
                session,
                event_type="annotation_batch_changed",
                document_id=batch.document_id,
                file_asset_id=batch.file_asset_id,
                payload={
                    "batchId": batch.id,
                    "action": action,
                    "annotationIds": [
                        item["annotation"]["id"] for item in affected
                    ],
                },
            )
            response = {
                "batchId": batch.id,
                "affected": affected,
                "failed": failed,
            }
            self._save_receipt(
                session,
                client_mutation_id=command.client_mutation_id,
                actor=command.actor,
                action="batch_undo",
                request_hash=request_hash,
                response=response,
                batch_id=batch.id,
                event_id=event.event_id,
            )
            return response

    # ------------------------------------------------------------------
    # Reader bridge and audit
    # ------------------------------------------------------------------

    def reader_anchor(self, annotation_id: str, actor: ActorContext) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            row = self._require_annotation(session, annotation_id)
            self._authorize(
                actor,
                "locate",
                row.document_id,
                row.file_id,
                row,
            )
            return annotation_to_reader_anchor(row)

    def locate_from_sidebar(
        self,
        payload: LocateFromSidebarInput | dict[str, Any],
        actor: ActorContext,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(payload, dict):
            payload = LocateFromSidebarInput.model_validate(payload)
        effective_mode = self._mode(mode)
        with session_scope(self.factory) as session:
            row = self._require_annotation(session, payload.annotation_id)
            self._check_scope(row, payload.document_id, payload.file_asset_id)
            self._authorize(
                actor,
                "locate",
                row.document_id,
                row.file_id,
                row,
            )
            if row.deleted_at is not None:
                raise not_found(row.id)
            result = anchor_resolve_result(row, mode=effective_mode)
            event = emit_event(
                session,
                event_type="annotation_sidebar_located",
                document_id=row.document_id,
                file_asset_id=row.file_id,
                payload={
                    "annotationId": row.id,
                    "anchorResolveResult": result,
                },
            )
            return event_from_row(event)

    def events_after(
        self,
        *,
        after_sequence: int = 0,
        document_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            return list_events(
                session,
                after_sequence=after_sequence,
                document_id=document_id,
                limit=limit,
            )

    def audit_trail(
        self,
        annotation_id: str,
        actor: ActorContext,
    ) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            row = self._require_annotation(session, annotation_id)
            self._authorize(
                actor,
                "audit",
                row.document_id,
                row.file_id,
                row,
            )
            rows = session.scalars(
                sa.select(AnnotationAudit)
                .where(AnnotationAudit.annotation_id == annotation_id)
                .order_by(AnnotationAudit.created_at, AnnotationAudit.id)
            ).all()
            return [
                {
                    "id": item.id,
                    "batchId": item.batch_id,
                    "annotationId": item.annotation_id,
                    "documentId": item.document_id,
                    "fileAssetId": item.file_id,
                    "action": item.action,
                    "actorId": item.actor_id,
                    "requestId": item.request_id,
                    "clientMutationId": item.client_mutation_id,
                    "before": item.before_json,
                    "after": item.after_json,
                    "createdAt": item.created_at,
                }
                for item in rows
            ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_writable(self, mode: str | None) -> None:
        if self._mode(mode) == "png_fallback":
            raise read_only("PNG 降级模式禁用写入")

    @staticmethod
    def _with_actor(
        command: Any,
        actor: ActorContext | None,
    ) -> Any:
        if actor is not None and actor.actor_id != command.actor.actor_id:
            raise read_only("actor 上下文不一致")
        return command

    @staticmethod
    def _audit(
        session: Session,
        row: Annotation,
        *,
        action: str,
        actor: ActorContext,
        client_mutation_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        batch_id: str | None = None,
    ) -> AnnotationAudit:
        audit = AnnotationAudit(
            id=f"aaudit_{uuid.uuid4().hex}",
            batch_id=batch_id,
            annotation_id=row.id,
            document_id=row.document_id,
            file_id=row.file_id,
            action=action,
            actor_id=actor.actor_id,
            request_id=actor.request_id,
            client_mutation_id=client_mutation_id,
            before_json=copy.deepcopy(before),
            after_json=copy.deepcopy(after),
            created_at=utc_now_iso(),
        )
        session.add(audit)
        session.flush()
        return audit

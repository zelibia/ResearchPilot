"""P0-05 outline editing, revisions, Markdown, AI merge and progress service."""

from __future__ import annotations

import copy
import re
import uuid
import json
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..storage.v116.db import session_scope
from ..agent.client import LLMClient
from ..storage.v116.orm import (
    Annotation,
    FileAsset,
    LibraryDocument,
    OutlineAIRun,
    OutlineMergeConflict,
    OutlineMutationReceipt,
    OutlineNodeAnnotation,
    OutlineNodeEntity,
    OutlineNodeEvidence,
    OutlineRevision,
    OutlineRevisionNode,
    OutlineVersion,
    OutlineNode as LegacyOutlineNode,
    ProjectOutlineNodeState,
    ReadingOutline,
)
from .contracts import (
    ActorContext,
    AddOutlineNodeCommand,
    ApplyOutlineMergeCommand,
    AttachOutlineAnnotationsCommand,
    CompleteOutlineAIRunRequest,
    DeleteOutlineNodesCommand,
    IndentOutlineNodesCommand,
    MarkdownPreviewRequest,
    MoveOutlineNodesCommand,
    OutlineAIResult,
    OutlineAIRunRequest,
    OutlineEditCommand,
    OutlineTreeNode,
    RenameOutlineNodeCommand,
    ResolveOutlineConflictCommand,
    RestoreOutlineRevisionCommand,
    SaveOutlineMarkdownCommand,
    SetOutlineNodeCompletedCommand,
    SetOutlineNodeLockCommand,
    UpdateOutlineNodeContentCommand,
)
from .errors import OutlineError, revision_conflict
from .events import canonical_hash, emit_outline_event, event_out, utc_now_iso
from .markdown import (
    clone_tree,
    diff_markdown,
    find_node,
    flatten_tree,
    normalize_tree,
    parse_markdown,
    remove_nodes,
    subtree_ids,
    tree_to_markdown,
    validate_tree,
)


def _normal_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _outline_out(row: ReadingOutline) -> dict[str, Any]:
    return {
        "id": row.id,
        "documentId": row.document_id,
        "kind": row.kind,
        "currentRevisionId": row.current_revision_id,
        "currentRevisionNo": row.current_revision_no,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def _revision_out(row: OutlineRevision) -> dict[str, Any]:
    return {
        "id": row.id,
        "outlineId": row.outline_id,
        "revisionNo": row.revision_no,
        "baseRevisionId": row.base_revision_id,
        "baseRevisionNo": row.base_revision_no,
        "status": row.status,
        "source": row.source,
        "tree": row.tree_json,
        "markdown": row.markdown,
        "changeSummary": row.change_summary,
        "createdBy": row.created_by,
        "createdAt": row.created_at,
        "contentHash": row.content_hash,
        "aiRunId": row.ai_run_id,
        "legacySource": row.legacy_source_json,
    }


def _ai_run_out(row: OutlineAIRun) -> dict[str, Any]:
    result = row.result_json or None
    return {
        "id": row.id,
        "outlineId": row.outline_id,
        "baseRevisionId": row.base_revision_id,
        "baseRevisionNo": row.base_revision_no,
        "status": row.status,
        "provider": row.provider,
        "model": row.model,
        "promptVersion": row.prompt_version,
        "inputHash": row.input_hash,
        "inputScope": row.input_scope_json,
        "result": result,
        "errorCode": row.error_code,
        "errorMessage": row.error_message,
        "requestedBy": row.requested_by,
        "startedAt": row.started_at,
        "finishedAt": row.finished_at,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def _conflict_out(row: OutlineMergeConflict) -> dict[str, Any]:
    return {
        "id": row.id,
        "outlineId": row.outline_id,
        "aiRunId": row.ai_run_id,
        "candidateRevisionId": row.candidate_revision_id,
        "baseRevisionNo": row.base_revision_no,
        "currentRevisionNo": row.current_revision_no,
        "nodeId": row.node_id,
        "conflictType": row.conflict_type,
        "baseNode": row.base_node_json or None,
        "currentNode": row.current_node_json or None,
        "aiNode": row.ai_node_json or None,
        "resolution": row.resolution,
        "resolvedBy": row.resolved_by,
        "resolvedAt": row.resolved_at,
        "createdAt": row.created_at,
    }


class OutlineApplicationService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        llm_client: LLMClient | None = None,
    ) -> None:
        self.factory = factory
        self.llm_client = llm_client

    def generate_live(
        self,
        *,
        outline_id: str,
        base_revision_no: int,
        include_annotations: bool,
        actor: ActorContext,
        client_mutation_id: str,
    ) -> dict[str, Any]:
        if self.llm_client is None:
            raise OutlineError("AI_RUN_NOT_READY", "未配置 LLMClient")
        request = OutlineAIRunRequest(
            outlineId=outline_id,
            baseRevisionNo=base_revision_no,
            includeAnnotations=include_annotations,
            actor=actor,
            clientMutationId=client_mutation_id,
        )
        run = self.request_ai_run(request)
        if run.get("status") != "queued":
            return run
        bootstrap = self.get_bootstrap_for_outline(outline_id)
        prompt = {
            "baseRevisionNo": base_revision_no,
            "currentTree": bootstrap["currentRevision"]["tree"],
            "markdown": bootstrap["currentRevision"]["markdown"],
            "instruction": (
                "Return OutlineAIResult JSON with schemaVersion outline.v1, summary, "
                "nodes containing tempId,parentTempId,orderIndex,headingLevel,title,"
                "bodyMarkdown, markdown and warnings."
            ),
        }
        raw, _ = self.llm_client.complete_json(
            "You regenerate a grounded reading outline.",
            json.dumps(prompt, ensure_ascii=False),
        )
        from .contracts import OutlineAIResult

        result = OutlineAIResult.model_validate(raw)
        return self.complete_ai_run(
            outline_id,
            run["id"],
            CompleteOutlineAIRunRequest(result=result, actor=actor),
        )

    def get_bootstrap_for_outline(self, outline_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            outline = self._require_outline(session, outline_id)
            current = self._current_revision(session, outline)
            return {
                "outline": _outline_out(outline),
                "currentRevision": _revision_out(current),
            }

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get_bootstrap(
        self,
        document_id: str,
        project_id: str,
        *,
        kind: str = "reading_outline",
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            self._require_document(session, document_id)
            outline = self._ensure_outline(session, document_id, kind)
            current = self._current_revision(session, outline)
            tree = self._load_tree(session, current.id)
            completed, progress = self._project_progress(
                session, project_id, outline.id, tree
            )
            history = session.scalars(
                sa.select(OutlineRevision)
                .where(OutlineRevision.outline_id == outline.id)
                .order_by(OutlineRevision.revision_no.desc())
            ).all()
            conflicts = session.scalars(
                sa.select(OutlineMergeConflict)
                .where(
                    OutlineMergeConflict.outline_id == outline.id,
                    OutlineMergeConflict.resolution == "pending",
                )
                .order_by(OutlineMergeConflict.created_at.desc())
            ).all()
            return {
                "outline": _outline_out(outline),
                "currentRevision": _revision_out(current),
                "nodes": self._node_views(tree, completed, outline.id),
                "revisionHistory": [
                    {
                        "id": item.id,
                        "revisionNo": item.revision_no,
                        "baseRevisionNo": item.base_revision_no,
                        "status": item.status,
                        "source": item.source,
                        "changeSummary": item.change_summary,
                        "createdBy": item.created_by,
                        "createdAt": item.created_at,
                    }
                    for item in history
                ],
                "projectProgress": progress,
                "pendingConflicts": [_conflict_out(item) for item in conflicts],
                "capabilities": {
                    "canEdit": True,
                    "canRegenerate": True,
                    "canCompare": True,
                    "canRollback": True,
                    "canMarkComplete": True,
                    "canLock": True,
                    "canAttachAnnotation": True,
                },
            }

    def _require_document(self, session: Session, document_id: str) -> LibraryDocument:
        row = session.get(LibraryDocument, document_id)
        if row is None:
            raise OutlineError("DOCUMENT_MISMATCH", f"document not found: {document_id}")
        return row

    def _require_outline(self, session: Session, outline_id: str) -> ReadingOutline:
        row = session.get(ReadingOutline, outline_id)
        if row is None:
            raise OutlineError("OUTLINE_NOT_FOUND", f"outline not found: {outline_id}")
        return row

    def _require_revision(
        self,
        session: Session,
        outline_id: str,
        revision_no: int,
    ) -> OutlineRevision:
        row = session.scalar(
            sa.select(OutlineRevision).where(
                OutlineRevision.outline_id == outline_id,
                OutlineRevision.revision_no == revision_no,
            )
        )
        if row is None:
            raise OutlineError(
                "REVISION_NOT_FOUND",
                f"outline revision not found: {revision_no}",
            )
        return row

    def _current_revision(
        self, session: Session, outline: ReadingOutline
    ) -> OutlineRevision:
        if not outline.current_revision_id:
            raise OutlineError("REVISION_NOT_FOUND", "当前大纲没有 revision")
        revision = session.get(OutlineRevision, outline.current_revision_id)
        if revision is None or revision.outline_id != outline.id:
            raise OutlineError("REVISION_NOT_FOUND", "当前大纲 revision 无效")
        return revision

    def _ensure_outline(
        self, session: Session, document_id: str, kind: str
    ) -> ReadingOutline:
        outline = session.scalar(
            sa.select(ReadingOutline).where(
                ReadingOutline.document_id == document_id,
                ReadingOutline.kind == kind,
            )
        )
        if outline is not None:
            if not outline.current_revision_id:
                self._migrate_legacy(session, outline)
            return outline
        outline = ReadingOutline(
            id=f"outline_{uuid.uuid4().hex}",
            document_id=document_id,
            kind=kind,
            current_revision_id=None,
            current_revision_no=0,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        session.add(outline)
        session.flush()
        self._migrate_legacy(session, outline)
        if not outline.current_revision_id:
            revision = self._append_revision(
                session,
                outline,
                tree=[],
                base=None,
                status="applied",
                source="manual",
                actor_id="system",
                change_summary="创建空大纲",
            )
            outline.current_revision_id = revision.id
            outline.current_revision_no = revision.revision_no
        return outline

    def _migrate_legacy(self, session: Session, outline: ReadingOutline) -> None:
        versions = session.scalars(
            sa.select(OutlineVersion)
            .where(
                OutlineVersion.document_id == outline.document_id,
                OutlineVersion.kind == outline.kind,
            )
            .order_by(OutlineVersion.version)
        ).all()
        if not versions:
            return
        base: OutlineRevision | None = None
        selected_current: OutlineRevision | None = None
        file_asset = session.scalar(
            sa.select(FileAsset)
            .where(FileAsset.document_id == outline.document_id)
            .order_by(FileAsset.created_at)
        )
        for version in versions:
            legacy_nodes = session.scalars(
                sa.select(LegacyOutlineNode)
                .where(LegacyOutlineNode.outline_version_id == version.id)
                .order_by(LegacyOutlineNode.order_index)
            ).all()
            by_id: dict[str, OutlineTreeNode] = {}
            for node in legacy_nodes:
                evidence = session.scalars(
                    sa.select(OutlineNodeEvidence).where(
                        OutlineNodeEvidence.outline_node_id == node.id
                    )
                ).all()
                annotation_ids = [
                    item.annotation_id
                    for item in evidence
                    if item.annotation_id
                ]
                anchor = None
                page_evidence = next(
                    (item for item in evidence if item.page_index is not None),
                    None,
                )
                if page_evidence and file_asset:
                    anchor = {
                        "protocolVersion": "rp.reader.v1",
                        "anchorId": f"legacy_anchor_{node.id}",
                        "documentId": outline.document_id,
                        "fileAssetId": file_asset.id,
                        "start": {
                            "pageIndex": page_evidence.page_index,
                            "charOffset": 0,
                            "x": 0,
                            "y": 0,
                        },
                        "end": {
                            "pageIndex": page_evidence.page_index,
                            "charOffset": len(page_evidence.quote),
                            "x": 0,
                            "y": 0,
                        },
                        "quote": page_evidence.quote,
                        "segments": [],
                        "matchStatus": page_evidence.match_status,
                        "coordinateVersion": "page-normalized-v1",
                        "createdAt": utc_now_iso(),
                    }
                by_id[node.id] = OutlineTreeNode(
                    nodeId=node.id,
                    title=node.title,
                    headingLevel=max(1, min(6, node.heading_level)),
                    orderIndex=node.order_index,
                    bodyMarkdown=node.body_markdown,
                    anchor=anchor,
                    annotationIds=annotation_ids,
                    origin="legacy",
                )
            roots: list[OutlineTreeNode] = []
            for node in legacy_nodes:
                item = by_id[node.id]
                if node.parent_id and node.parent_id in by_id:
                    by_id[node.parent_id].children.append(item)
                else:
                    roots.append(item)
            normalize_tree(roots)
            revision = self._append_revision(
                session,
                outline,
                tree=roots,
                base=base,
                status="applied",
                source="legacy",
                actor_id=version.created_by or "legacy",
                change_summary=f"迁移 P0-01 大纲 v{version.version}",
                legacy_source=version.legacy_source_json,
                legacy_hash=f"p01:{version.id}",
                allocate_only=True,
            )
            if version.is_current:
                selected_current = revision
            base = revision
        selected_current = selected_current or base
        if selected_current:
            outline.current_revision_id = selected_current.id
            outline.current_revision_no = selected_current.revision_no
        outline.updated_at = utc_now_iso()
        session.flush()

    def _load_tree(self, session: Session, revision_id: str) -> list[OutlineTreeNode]:
        rows = session.scalars(
            sa.select(OutlineRevisionNode)
            .where(OutlineRevisionNode.revision_id == revision_id)
            .order_by(OutlineRevisionNode.order_index)
        ).all()
        links: dict[str, list[str]] = {}
        for link in session.scalars(
            sa.select(OutlineNodeAnnotation).where(
                OutlineNodeAnnotation.revision_id == revision_id
            )
        ).all():
            links.setdefault(link.node_id, []).append(link.annotation_id)
        by_id = {
            row.node_id: OutlineTreeNode(
                nodeId=row.node_id,
                title=row.title,
                headingLevel=row.heading_level,
                orderIndex=row.order_index,
                bodyMarkdown=row.body_markdown,
                anchor=row.anchor_json or None,
                annotationIds=sorted(links.get(row.node_id, [])),
                noteMarkdown=row.note_markdown,
                locked=row.locked,
                origin=row.origin,
            )
            for row in rows
        }
        roots: list[OutlineTreeNode] = []
        for row in rows:
            node = by_id[row.node_id]
            if row.parent_node_id and row.parent_node_id in by_id:
                by_id[row.parent_node_id].children.append(node)
            else:
                roots.append(node)
        normalize_tree(roots)
        return roots

    def _node_views(
        self,
        tree: list[OutlineTreeNode],
        completed: dict[str, dict[str, Any]],
        outline_id: str,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for node in flatten_tree(tree):
            state = completed.get(node.node_id, {})
            output.append(
                {
                    "id": node.node_id,
                    "outlineId": outline_id,
                    "parentId": self._parent_id(tree, node.node_id),
                    "orderIndex": node.order_index,
                    "headingLevel": node.heading_level,
                    "title": node.title,
                    "bodyMarkdown": node.body_markdown,
                    "anchor": node.anchor,
                    "annotationIds": node.annotation_ids,
                    "noteMarkdown": node.note_markdown,
                    "locked": node.locked,
                    "userEdited": node.origin == "user",
                    "origin": node.origin,
                    "contentHash": canonical_hash(
                        node.model_dump(by_alias=True, exclude={"children"})
                    ),
                    "createdAt": state.get("createdAt", ""),
                    "updatedAt": state.get("updatedAt", ""),
                    "completed": bool(state.get("completed", False)),
                    "completedAt": state.get("completedAt"),
                    "lastViewedAt": state.get("lastViewedAt"),
                }
            )
        return output

    def _project_progress(
        self,
        session: Session,
        project_id: str,
        outline_id: str,
        tree: list[OutlineTreeNode],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        states = session.scalars(
            sa.select(ProjectOutlineNodeState).where(
                ProjectOutlineNodeState.project_id == project_id,
                ProjectOutlineNodeState.outline_id == outline_id,
            )
        ).all()
        mapping = {
            item.node_id: {
                "completed": item.completed,
                "completedAt": item.completed_at,
                "lastViewedAt": item.last_viewed_at,
                "updatedAt": item.updated_at,
            }
            for item in states
        }
        total = len(flatten_tree(tree))
        return mapping, {
            "completedCount": sum(1 for item in states if item.completed),
            "totalCount": total,
        }

    # ------------------------------------------------------------------
    # Revision append and commands
    # ------------------------------------------------------------------

    def apply_edit(
        self,
        command: OutlineEditCommand,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        if actor is not None and actor.actor_id != command.actor.actor_id:
            raise OutlineError("OUTLINE_INVALID_TREE", "actor 上下文不一致")
        action = f"edit:{command.type}"
        request_hash = canonical_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replayed = self._receipt(
                session, command.client_mutation_id, action, request_hash
            )
            if replayed is not None:
                return replayed
            outline = self._require_outline(session, command.outline_id)
            self._check_base(outline, command.base_revision_no)
            current = self._current_revision(session, outline)
            tree = self._load_tree(session, current.id)
            changed = self._apply_command(session, outline, tree, command)
            validate_tree(tree)
            revision = self._append_revision(
                session,
                outline,
                tree=tree,
                base=current,
                status="applied",
                source="manual",
                actor_id=command.actor.actor_id,
                change_summary=(
                    command.change_summary
                    or self._default_summary(command.type)
                ),
            )
            outline.current_revision_id = revision.id
            outline.current_revision_no = revision.revision_no
            outline.updated_at = utc_now_iso()
            session.flush()
            event = emit_outline_event(
                session,
                event_type="outline_updated",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "revisionId": revision.id,
                    "revisionNo": revision.revision_no,
                    "baseRevisionNo": revision.base_revision_no,
                    "source": revision.source,
                    "changedNodeIds": changed,
                    "changeSummary": revision.change_summary,
                },
            )
            result = self._mutation_result(session, outline, revision, [event])
            self._save_receipt(
                session,
                command.client_mutation_id,
                action,
                request_hash,
                outline.id,
                result,
            )
            return result

    def _apply_command(
        self,
        session: Session,
        outline: ReadingOutline,
        tree: list[OutlineTreeNode],
        command: OutlineEditCommand,
    ) -> list[str]:
        changed: list[str] = []
        if isinstance(command, AddOutlineNodeCommand):
            node = OutlineTreeNode(
                nodeId=f"onode_{uuid.uuid4().hex}",
                title=command.title.strip(),
                headingLevel=command.heading_level,
                orderIndex=0,
                bodyMarkdown=command.body_markdown,
                anchor=command.anchor,
                noteMarkdown=command.note_markdown,
                origin="user",
            )
            if command.parent_node_id:
                parent, _, _ = find_node(tree, command.parent_node_id)
                if parent is None:
                    raise OutlineError("NODE_NOT_FOUND", command.parent_node_id)
                siblings = parent.children
            else:
                siblings = tree
            position = len(siblings)
            if command.insert_after_node_id:
                position = next(
                    (
                        index + 1
                        for index, item in enumerate(siblings)
                        if item.node_id == command.insert_after_node_id
                    ),
                    position,
                )
            siblings.insert(position, node)
            changed.append(node.node_id)
        elif isinstance(command, DeleteOutlineNodesCommand):
            for node_id in command.node_ids:
                node, _, _ = find_node(tree, node_id)
                if node is None:
                    raise OutlineError("NODE_NOT_FOUND", node_id)
                if node.locked:
                    raise OutlineError(
                        "OUTLINE_NODE_LOCKED",
                        f"请先解锁节点：{node.title}",
                    )
            changed.extend(remove_nodes(tree, set(command.node_ids)))
        elif isinstance(command, RenameOutlineNodeCommand):
            node, _, _ = find_node(tree, command.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", command.node_id)
            self._ensure_unlocked(node)
            node.title = command.title.strip()
            node.origin = "user"
            changed.append(node.node_id)
        elif isinstance(command, MoveOutlineNodesCommand):
            changed.extend(self._move_nodes(tree, command))
        elif isinstance(command, IndentOutlineNodesCommand):
            changed.extend(self._indent_nodes(tree, command))
        elif isinstance(command, UpdateOutlineNodeContentCommand):
            node, _, _ = find_node(tree, command.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", command.node_id)
            self._ensure_unlocked(node)
            if command.body_markdown is not None:
                node.body_markdown = command.body_markdown
            if command.note_markdown is not None:
                node.note_markdown = command.note_markdown
            node.origin = "user"
            changed.append(node.node_id)
        elif isinstance(command, SetOutlineNodeLockCommand):
            node, _, _ = find_node(tree, command.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", command.node_id)
            node.locked = command.locked
            changed.append(node.node_id)
        elif isinstance(command, AttachOutlineAnnotationsCommand):
            node, _, _ = find_node(tree, command.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", command.node_id)
            self._ensure_unlocked(node)
            annotation_ids = list(dict.fromkeys(command.annotation_ids))
            for annotation_id in annotation_ids:
                annotation = session.get(Annotation, annotation_id)
                if annotation is None or annotation.document_id != outline.document_id:
                    raise OutlineError(
                        "ANNOTATION_MISMATCH",
                        f"批注不存在或不属于当前文档：{annotation_id}",
                    )
            node.annotation_ids = annotation_ids
            changed.append(node.node_id)
        elif isinstance(command, SaveOutlineMarkdownCommand):
            parsed, warnings, matched = parse_markdown(
                command.markdown,
                existing_tree=tree,
                preserve_node_ids=command.parser_mode == "preserve_node_ids",
            )
            tree[:] = parsed
            changed.extend(matched)
            for node in flatten_tree(parsed):
                if node.node_id not in matched:
                    changed.append(node.node_id)
            if warnings:
                command.change_summary = (
                    command.change_summary
                    or "Markdown 保存：" + "；".join(warnings)
                )
        else:  # pragma: no cover - discriminated union protects this path
            raise OutlineError("OUTLINE_INVALID_TREE", "不支持的大纲命令")
        normalize_tree(tree)
        return list(dict.fromkeys(changed))

    def _move_nodes(
        self, tree: list[OutlineTreeNode], command: MoveOutlineNodesCommand
    ) -> list[str]:
        changed: list[str] = []
        for move in command.moves:
            node, _, _ = find_node(tree, move.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", move.node_id)
            if move.parent_node_id and move.parent_node_id in set(subtree_ids(node)):
                raise OutlineError("OUTLINE_NODE_CYCLE", "节点不能移动到自己的后代")
            remove_nodes(tree, {move.node_id})
            if move.parent_node_id:
                parent, _, _ = find_node(tree, move.parent_node_id)
                if parent is None:
                    raise OutlineError("NODE_NOT_FOUND", move.parent_node_id)
                siblings = parent.children
            else:
                siblings = tree
            siblings.insert(min(move.order_index, len(siblings)), node)
            changed.append(node.node_id)
        normalize_tree(tree)
        return changed

    def _indent_nodes(
        self, tree: list[OutlineTreeNode], command: IndentOutlineNodesCommand
    ) -> list[str]:
        changed: list[str] = []
        ids = command.node_ids
        if command.type == "indent_nodes":
            for node_id in ids:
                node, _, siblings = find_node(tree, node_id)
                if node is None:
                    raise OutlineError("NODE_NOT_FOUND", node_id)
                index = siblings.index(node)
                if index < 1:
                    continue
                previous = siblings[index - 1]
                siblings.pop(index)
                previous.children.append(node)
                node.heading_level = min(6, previous.heading_level + 1)
                changed.append(node_id)
        else:
            for node_id in ids:
                node, parent, siblings = find_node(tree, node_id)
                if node is None or parent is None:
                    continue
                _, grandparent, parent_siblings = find_node(tree, parent.node_id)
                index = siblings.index(node)
                siblings.pop(index)
                parent_index = parent_siblings.index(parent)
                parent_siblings.insert(parent_index + 1, node)
                node.heading_level = max(1, node.heading_level - 1)
                changed.append(node_id)
        normalize_tree(tree)
        return changed

    @staticmethod
    def _ensure_unlocked(node: OutlineTreeNode) -> None:
        if node.locked:
            raise OutlineError("OUTLINE_NODE_LOCKED", f"节点已锁定：{node.title}")

    @staticmethod
    def _default_summary(command_type: str) -> str:
        return {
            "add_node": "新增大纲节点",
            "delete_nodes": "删除大纲节点",
            "rename_node": "重命名大纲节点",
            "move_nodes": "移动大纲节点",
            "indent_nodes": "缩进大纲节点",
            "outdent_nodes": "提升大纲节点",
            "update_node_content": "编辑节点内容",
            "set_lock": "修改节点锁定状态",
            "attach_annotations": "更新节点关联批注",
            "save_markdown": "保存 Markdown 大纲",
        }.get(command_type, "更新大纲")

    def _append_revision(
        self,
        session: Session,
        outline: ReadingOutline,
        *,
        tree: list[OutlineTreeNode],
        base: OutlineRevision | None,
        status: str,
        source: str,
        actor_id: str,
        change_summary: str,
        ai_run_id: str | None = None,
        legacy_source: dict[str, Any] | None = None,
        legacy_hash: str = "",
        allocate_only: bool = False,
    ) -> OutlineRevision:
        validate_tree(tree)
        markdown = tree_to_markdown(tree)
        revision_no = (
            session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(OutlineRevision.revision_no), 0)).where(
                    OutlineRevision.outline_id == outline.id
                )
            )
            or 0
        ) + 1
        now = utc_now_iso()
        content_hash = canonical_hash(
            {
                "tree": [item.model_dump(by_alias=True) for item in tree],
                "markdown": markdown,
            }
        )
        revision = OutlineRevision(
            id=f"orev_{uuid.uuid4().hex}",
            outline_id=outline.id,
            revision_no=int(revision_no),
            base_revision_id=base.id if base else None,
            base_revision_no=base.revision_no if base else None,
            status=status,
            source=source,
            tree_json=[item.model_dump(by_alias=True) for item in tree],
            markdown=markdown,
            change_summary=change_summary,
            created_by=actor_id,
            created_at=now,
            content_hash=content_hash,
            ai_run_id=ai_run_id,
            legacy_source_json=legacy_source or {},
            legacy_source_hash=legacy_hash,
        )
        session.add(revision)
        session.flush()
        self._store_nodes(session, outline, revision, tree, actor_id, now)
        if not allocate_only and status == "applied":
            outline.current_revision_id = revision.id
            outline.current_revision_no = revision.revision_no
            outline.updated_at = now
        return revision

    def _store_nodes(
        self,
        session: Session,
        outline: ReadingOutline,
        revision: OutlineRevision,
        tree: list[OutlineTreeNode],
        actor_id: str,
        now: str,
    ) -> None:
        links: list[tuple[str, str]] = []
        for node in flatten_tree(tree):
            if session.get(OutlineNodeEntity, node.node_id) is None:
                session.add(
                    OutlineNodeEntity(
                        id=node.node_id,
                        outline_id=outline.id,
                        created_at=now,
                        created_by=actor_id,
                    )
                )
                session.flush()
            parent_id = self._parent_id(tree, node.node_id)
            content_hash = canonical_hash(
                node.model_dump(by_alias=True, exclude={"children"})
            )
            session.add(
                OutlineRevisionNode(
                    revision_id=revision.id,
                    node_id=node.node_id,
                    parent_node_id=parent_id,
                    order_index=node.order_index,
                    heading_level=node.heading_level,
                    title=node.title,
                    body_markdown=node.body_markdown,
                    anchor_json=node.anchor or {},
                    note_markdown=node.note_markdown,
                    locked=node.locked,
                    user_edited=node.origin == "user",
                    origin=node.origin,
                    content_hash=content_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
            links.extend((node.node_id, item) for item in node.annotation_ids)
        session.flush()
        for node_id, annotation_id in links:
            session.add(
                OutlineNodeAnnotation(
                    revision_id=revision.id,
                    node_id=node_id,
                    annotation_id=annotation_id,
                    relation_type="evidence",
                    created_at=now,
                )
            )
        session.flush()

    @staticmethod
    def _parent_id(tree: list[OutlineTreeNode], node_id: str) -> str | None:
        _, parent, _ = find_node(tree, node_id)
        return parent.node_id if parent else None

    def _mutation_result(
        self,
        session: Session,
        outline: ReadingOutline,
        revision: OutlineRevision,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tree = self._load_tree(session, revision.id)
        return {
            "outline": _outline_out(outline),
            "revision": _revision_out(revision),
            "nodes": self._node_views(tree, {}, outline.id),
            "events": events,
        }

    # ------------------------------------------------------------------
    # Markdown and versions
    # ------------------------------------------------------------------

    def markdown_preview(
        self,
        outline_id: str,
        request: MarkdownPreviewRequest,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            outline = self._require_outline(session, outline_id)
            source = self._require_revision(session, outline_id, request.revision_no)
            tree = self._load_tree(session, source.id)
            parsed, warnings, matched = parse_markdown(
                request.markdown,
                existing_tree=tree,
                preserve_node_ids=request.parser_mode == "preserve_node_ids",
            )
            existing_ids = {item.node_id for item in flatten_tree(tree)}
            new_ids = {item.node_id for item in flatten_tree(parsed)}
            return {
                "markdown": request.markdown,
                "tree": [item.model_dump(by_alias=True) for item in parsed],
                "warnings": warnings,
                "matchedNodeIds": matched,
                "newNodeCount": len(new_ids - existing_ids),
                "removedNodeCount": len(existing_ids - new_ids),
            }

    def list_revisions(self, outline_id: str) -> list[dict[str, Any]]:
        with session_scope(self.factory) as session:
            self._require_outline(session, outline_id)
            rows = session.scalars(
                sa.select(OutlineRevision)
                .where(OutlineRevision.outline_id == outline_id)
                .order_by(OutlineRevision.revision_no.desc())
            ).all()
            output: list[dict[str, Any]] = []
            for row in rows:
                item = _revision_out(row)
                item["tree"] = [
                    node.model_dump(by_alias=True)
                    for node in self._load_tree(session, row.id)
                ]
                output.append(item)
            return output

    def get_revision(self, outline_id: str, revision_no: int) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            revision = self._require_revision(session, outline_id, revision_no)
            payload = _revision_out(revision)
            payload["tree"] = [
                item.model_dump(by_alias=True)
                for item in self._load_tree(session, revision.id)
            ]
            return payload

    def compare_revisions(
        self,
        outline_id: str,
        left_no: int,
        right_no: int,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            left = self._require_revision(session, outline_id, left_no)
            right = self._require_revision(session, outline_id, right_no)
            left_tree = self._load_tree(session, left.id)
            right_tree = self._load_tree(session, right.id)
            left_map = {item.node_id: item for item in flatten_tree(left_tree)}
            right_map = {item.node_id: item for item in flatten_tree(right_tree)}
            diffs: list[dict[str, Any]] = []
            counts = {
                key: 0
                for key in (
                    "added",
                    "removed",
                    "renamed",
                    "body_changed",
                    "moved",
                    "anchor_changed",
                    "annotation_links_changed",
                    "note_changed",
                    "locked_changed",
                    "unchanged",
                )
            }
            for node_id in sorted(set(left_map) | set(right_map)):
                before = left_map.get(node_id)
                after = right_map.get(node_id)
                status = self._diff_status(before, after, left_tree, right_tree)
                counts[status] += 1
                diffs.append(
                    {
                        "nodeId": node_id,
                        "status": status,
                        "before": (
                            before.model_dump(by_alias=True) if before else None
                        ),
                        "after": after.model_dump(by_alias=True) if after else None,
                    }
                )
            return {
                "outlineId": outline_id,
                "leftRevisionNo": left_no,
                "rightRevisionNo": right_no,
                "nodeDiffs": diffs,
                "markdownDiff": diff_markdown(left.markdown, right.markdown),
                "counts": counts,
            }

    def _diff_status(
        self,
        before: OutlineTreeNode | None,
        after: OutlineTreeNode | None,
        left_tree: list[OutlineTreeNode],
        right_tree: list[OutlineTreeNode],
    ) -> str:
        if before is None:
            return "added"
        if after is None:
            return "removed"
        if before.title != after.title:
            return "renamed"
        if before.body_markdown != after.body_markdown:
            return "body_changed"
        if (
            self._parent_id(left_tree, before.node_id)
            != self._parent_id(right_tree, after.node_id)
            or before.order_index != after.order_index
        ):
            return "moved"
        if before.anchor != after.anchor:
            return "anchor_changed"
        if before.annotation_ids != after.annotation_ids:
            return "annotation_links_changed"
        if before.note_markdown != after.note_markdown:
            return "note_changed"
        if before.locked != after.locked:
            return "locked_changed"
        return "unchanged"

    def restore_revision(self, command: RestoreOutlineRevisionCommand) -> dict[str, Any]:
        request_hash = canonical_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replayed = self._receipt(
                session,
                command.client_mutation_id,
                "restore",
                request_hash,
            )
            if replayed is not None:
                return replayed
            outline = self._require_outline(session, command.outline_id)
            self._check_base(outline, command.base_revision_no)
            source = self._require_revision(
                session, outline.id, command.source_revision_no
            )
            current = self._current_revision(session, outline)
            tree = self._load_tree(session, source.id)
            revision = self._append_revision(
                session,
                outline,
                tree=tree,
                base=current,
                status="applied",
                source="rollback",
                actor_id=command.actor.actor_id,
                change_summary=(
                    command.change_summary
                    or f"回滚到 v{command.source_revision_no}"
                ),
            )
            event = emit_outline_event(
                session,
                event_type="outline_updated",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "revisionId": revision.id,
                    "revisionNo": revision.revision_no,
                    "baseRevisionNo": revision.base_revision_no,
                    "source": "rollback",
                    "changedNodeIds": [node.node_id for node in flatten_tree(tree)],
                    "changeSummary": revision.change_summary,
                },
            )
            result = self._mutation_result(session, outline, revision, [event])
            self._save_receipt(
                session,
                command.client_mutation_id,
                "restore",
                request_hash,
                outline.id,
                result,
            )
            return result

    # ------------------------------------------------------------------
    # AI candidate / deterministic merge
    # ------------------------------------------------------------------

    def request_ai_run(self, request: OutlineAIRunRequest) -> dict[str, Any]:
        request_hash = canonical_hash(request.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            replayed = self._receipt(
                session, request.client_mutation_id, "ai_run", request_hash
            )
            if replayed is not None:
                return replayed
            outline = self._require_outline(session, request.outline_id)
            self._check_base(outline, request.base_revision_no)
            current = self._current_revision(session, outline)
            prompt = request.prompt_version or "outline.v1"
            scope = {"includeAnnotations": request.include_annotations}
            run = OutlineAIRun(
                id=f"oai_{uuid.uuid4().hex}",
                outline_id=outline.id,
                base_revision_id=current.id,
                base_revision_no=current.revision_no,
                status="queued",
                provider=request.provider,
                model=request.model,
                prompt_version=prompt,
                input_hash=canonical_hash(
                    {"revision": current.content_hash, **scope}
                ),
                input_scope_json=scope,
                result_json={},
                error_code="",
                error_message="",
                requested_by=request.actor.actor_id,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
            )
            session.add(run)
            session.flush()
            event1 = emit_outline_event(
                session,
                event_type="ai_regenerate_request",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "baseRevisionNo": request.base_revision_no,
                    "promptVersion": prompt,
                    "includeAnnotations": request.include_annotations,
                },
            )
            event2 = emit_outline_event(
                session,
                event_type="outline_generation_state",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "aiRunId": run.id,
                    "state": "queued",
                    "message": "大纲候选已排队",
                },
            )
            result = _ai_run_out(run)
            self._save_receipt(
                session,
                request.client_mutation_id,
                "ai_run",
                request_hash,
                outline.id,
                result,
            )
            return result

    def complete_ai_run(
        self,
        outline_id: str,
        run_id: str,
        request: CompleteOutlineAIRunRequest,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            outline = self._require_outline(session, outline_id)
            run = session.get(OutlineAIRun, run_id)
            if run is None or run.outline_id != outline_id:
                raise OutlineError("AI_RUN_NOT_READY", f"AI run not found: {run_id}")
            if run.status not in {"queued", "running"}:
                raise OutlineError("AI_RUN_NOT_READY", "AI run 已结束")
            current = self._current_revision(session, outline)
            base = (
                session.get(OutlineRevision, run.base_revision_id)
                if run.base_revision_id
                else current
            )
            base_tree = self._load_tree(session, base.id) if base else []
            current_tree = self._load_tree(session, current.id)
            candidate_tree = self._tree_from_ai_result(
                request.result,
                current_tree,
            )
            run.status = "succeeded"
            run.result_json = request.result.model_dump(by_alias=True)
            run.started_at = run.started_at or utc_now_iso()
            run.finished_at = utc_now_iso()
            run.updated_at = run.finished_at
            candidate = self._append_revision(
                session,
                outline,
                tree=candidate_tree,
                base=base,
                status="ai_candidate",
                source="ai",
                actor_id=request.actor.actor_id,
                change_summary=request.result.summary or "AI 大纲候选",
                ai_run_id=run.id,
            )
            conflicts = self._build_conflicts(
                session,
                outline,
                run,
                candidate,
                base,
                base_tree,
                current,
                current_tree,
                candidate_tree,
            )
            event1 = emit_outline_event(
                session,
                event_type="outline_generation_state",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "aiRunId": run.id,
                    "state": "succeeded",
                    "message": "候选大纲已生成，等待合并确认",
                },
            )
            event2 = emit_outline_event(
                session,
                event_type="outline_merge_ready",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "aiRunId": run.id,
                    "candidateRevisionId": candidate.id,
                    "conflictCount": len(conflicts),
                },
            )
            result = _ai_run_out(run)
            result["candidateRevisionId"] = candidate.id
            result["events"] = [event1, event2]
            session.flush()
            return result

    def _tree_from_ai_result(
        self,
        result: OutlineAIResult,
        current_tree: list[OutlineTreeNode],
    ) -> list[OutlineTreeNode]:
        existing = {
            (_normal_title(node.title), node.heading_level): node
            for node in flatten_tree(current_tree)
        }
        by_temp: dict[str, OutlineTreeNode] = {}
        for item in result.nodes:
            match = existing.get((_normal_title(item.title), item.heading_level))
            anchor = self._anchor_from_hint(item.anchor_hint)
            by_temp[item.temp_id] = OutlineTreeNode(
                nodeId=match.node_id if match else f"onode_{uuid.uuid4().hex}",
                title=item.title.strip(),
                headingLevel=item.heading_level,
                orderIndex=item.order_index,
                bodyMarkdown=item.body_markdown,
                anchor=anchor if anchor else (match.anchor if match else None),
                annotationIds=list(match.annotation_ids) if match else [],
                noteMarkdown=match.note_markdown if match else "",
                locked=match.locked if match else False,
                origin=match.origin if match else "ai",
            )
        roots: list[OutlineTreeNode] = []
        for item in result.nodes:
            node = by_temp[item.temp_id]
            if item.parent_temp_id and item.parent_temp_id in by_temp:
                by_temp[item.parent_temp_id].children.append(node)
            else:
                roots.append(node)
        normalize_tree(roots)
        return roots

    @staticmethod
    def _anchor_from_hint(hint: dict[str, Any] | None) -> dict[str, Any] | None:
        if not hint or "pageIndex" not in hint:
            return None
        page_index = int(hint.get("pageIndex", 0))
        char_start = int(hint.get("charStart") or 0)
        char_end = int(hint.get("charEnd") or char_start)
        return {
            "protocolVersion": "rp.reader.v1",
            "documentId": "",
            "fileAssetId": "",
            "start": {
                "pageIndex": page_index,
                "charOffset": char_start,
                "x": 0,
                "y": 0,
            },
            "end": {
                "pageIndex": page_index,
                "charOffset": char_end,
                "x": 0,
                "y": 0,
            },
            "quote": str(hint.get("quote") or ""),
            "segments": [],
            "matchStatus": "partial",
            "coordinateVersion": "page-normalized-v1",
            "createdAt": utc_now_iso(),
        }

    def _build_conflicts(
        self,
        session: Session,
        outline: ReadingOutline,
        run: OutlineAIRun,
        candidate: OutlineRevision,
        base: OutlineRevision,
        base_tree: list[OutlineTreeNode],
        current: OutlineRevision,
        current_tree: list[OutlineTreeNode],
        candidate_tree: list[OutlineTreeNode],
    ) -> list[OutlineMergeConflict]:
        base_map = {item.node_id: item for item in flatten_tree(base_tree)}
        current_map = {item.node_id: item for item in flatten_tree(current_tree)}
        candidate_map = {
            item.node_id: item for item in flatten_tree(candidate_tree)
        }
        conflicts: list[OutlineMergeConflict] = []
        for node_id in sorted(set(base_map) | set(current_map) | set(candidate_map)):
            base_node = base_map.get(node_id)
            current_node = current_map.get(node_id)
            ai_node = candidate_map.get(node_id)
            if current_node and current_node.locked:
                continue
            current_changed = current_node != base_node
            ai_changed = ai_node != base_node
            conflict_type: str | None = None
            if current_node is None and ai_node is not None and current_changed:
                conflict_type = "user_deleted_ai_modified"
            elif ai_node is None and current_node is not None and current_changed:
                conflict_type = "ai_deleted_user_modified"
            elif current_changed and ai_changed:
                conflict_type = "both_modified"
            if not conflict_type:
                continue
            row = OutlineMergeConflict(
                id=f"oconf_{uuid.uuid4().hex}",
                outline_id=outline.id,
                ai_run_id=run.id,
                candidate_revision_id=candidate.id,
                base_revision_no=base.revision_no,
                current_revision_no=current.revision_no,
                node_id=node_id if session.get(OutlineNodeEntity, node_id) else None,
                conflict_type=conflict_type,
                base_node_json=(
                    base_node.model_dump(by_alias=True) if base_node else {}
                ),
                current_node_json=(
                    current_node.model_dump(by_alias=True) if current_node else {}
                ),
                ai_node_json=ai_node.model_dump(by_alias=True) if ai_node else {},
                resolution="pending",
                resolved_by="",
                resolved_at=None,
                created_at=utc_now_iso(),
            )
            session.add(row)
            conflicts.append(row)
        session.flush()
        return conflicts

    def merge_preview(self, outline_id: str, run_id: str) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            run = session.get(OutlineAIRun, run_id)
            if run is None or run.outline_id != outline_id:
                raise OutlineError("AI_RUN_NOT_READY", f"AI run not found: {run_id}")
            candidate = session.scalar(
                sa.select(OutlineRevision).where(
                    OutlineRevision.ai_run_id == run.id,
                    OutlineRevision.status == "ai_candidate",
                )
            )
            if candidate is None:
                raise OutlineError("AI_RUN_NOT_READY", "AI 候选版本尚未生成")
            outline = self._require_outline(session, outline_id)
            current = self._current_revision(session, outline)
            base = (
                session.get(OutlineRevision, run.base_revision_id)
                if run.base_revision_id
                else current
            )
            current_tree = self._load_tree(session, current.id)
            base_tree = self._load_tree(session, base.id) if base else []
            candidate_tree = self._load_tree(session, candidate.id)
            base_map = {item.node_id: item for item in flatten_tree(base_tree)}
            current_map = {
                item.node_id: item for item in flatten_tree(current_tree)
            }
            candidate_map = {
                item.node_id: item for item in flatten_tree(candidate_tree)
            }
            conflicts = session.scalars(
                sa.select(OutlineMergeConflict)
                .where(OutlineMergeConflict.candidate_revision_id == candidate.id)
                .order_by(OutlineMergeConflict.created_at)
            ).all()
            nodes: list[dict[str, Any]] = []
            counts = {
                "autoAdded": 0,
                "autoUpdated": 0,
                "preserved": 0,
                "suggestedRemoval": 0,
            }
            for node_id in sorted(set(base_map) | set(current_map) | set(candidate_map)):
                base_node = base_map.get(node_id)
                current_node = current_map.get(node_id)
                candidate_node = candidate_map.get(node_id)
                status = "unchanged"
                reason = ""
                if current_node and current_node.locked:
                    status, reason = "kept_locked", "锁定节点完整保留"
                    counts["preserved"] += 1
                elif current_node and current_node.origin == "user":
                    status, reason = "kept_user_edited", "用户编辑节点保留"
                    counts["preserved"] += 1
                elif current_node is None and candidate_node is not None:
                    status, reason = "added_from_ai", "AI 新增节点"
                    counts["autoAdded"] += 1
                elif current_node is not None and candidate_node is None:
                    status, reason = "ai_removed_suggested", "建议删除但不自动删除"
                    counts["suggestedRemoval"] += 1
                elif candidate_node and candidate_node != base_node:
                    status, reason = "updated_from_ai", "current 相对 base 未修改"
                    counts["autoUpdated"] += 1
                nodes.append(
                    {
                        "nodeId": node_id,
                        "status": status,
                        "current": (
                            current_node.model_dump(by_alias=True)
                            if current_node
                            else None
                        ),
                        "candidate": (
                            candidate_node.model_dump(by_alias=True)
                            if candidate_node
                            else None
                        ),
                        "base": (
                            base_node.model_dump(by_alias=True)
                            if base_node
                            else None
                        ),
                        "reason": reason,
                    }
                )
            return {
                "aiRun": _ai_run_out(run),
                "candidateRevision": _revision_out(candidate),
                "baseRevisionNo": base.revision_no if base else 0,
                "currentRevisionNo": current.revision_no,
                "mergeNodes": nodes,
                "conflicts": [_conflict_out(item) for item in conflicts],
                "autoAddedCount": counts["autoAdded"],
                "autoUpdatedCount": counts["autoUpdated"],
                "preservedCount": counts["preserved"],
                "suggestedRemovalCount": counts["suggestedRemoval"],
            }

    def resolve_conflict(
        self,
        outline_id: str,
        command: ResolveOutlineConflictCommand,
    ) -> dict[str, Any]:
        action = f"resolve:{command.conflict_id}"
        request_hash = canonical_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replayed = self._receipt(
                session, command.client_mutation_id, action, request_hash
            )
            if replayed is not None:
                return replayed
            outline = self._require_outline(session, outline_id)
            self._check_base(outline, command.base_revision_no)
            conflict = session.get(OutlineMergeConflict, command.conflict_id)
            if conflict is None or conflict.outline_id != outline_id:
                raise OutlineError("NODE_NOT_FOUND", command.conflict_id)
            conflict.resolution = command.resolution
            conflict.resolved_by = command.actor.actor_id
            conflict.resolved_at = utc_now_iso()
            if command.merged_node:
                conflict.current_node_json = command.merged_node.model_dump(
                    by_alias=True
                )
            session.flush()
            run_id = conflict.ai_run_id
        preview = self.merge_preview(outline_id, run_id)
        with session_scope(self.factory) as session:
            self._save_receipt(
                session,
                command.client_mutation_id,
                action,
                request_hash,
                outline_id,
                preview,
            )
        return preview

    def apply_merge(self, command: ApplyOutlineMergeCommand) -> dict[str, Any]:
        request_hash = canonical_hash(command.model_dump(by_alias=True))
        with session_scope(self.factory) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            replayed = self._receipt(
                session, command.client_mutation_id, "merge_apply", request_hash
            )
            if replayed is not None:
                return replayed
            outline = self._require_outline(session, command.outline_id)
            self._check_base(outline, command.base_revision_no)
            candidate = session.get(
                OutlineRevision, command.candidate_revision_id
            )
            if (
                candidate is None
                or candidate.outline_id != outline.id
                or candidate.status != "ai_candidate"
            ):
                raise OutlineError("REVISION_NOT_FOUND", "AI 候选版本无效")
            current = self._current_revision(session, outline)
            current_tree = self._load_tree(session, current.id)
            candidate_tree = self._load_tree(session, candidate.id)
            conflicts = session.scalars(
                sa.select(OutlineMergeConflict).where(
                    OutlineMergeConflict.candidate_revision_id == candidate.id
                )
            ).all()
            conflict_by_node = {
                item.node_id: item for item in conflicts if item.node_id
            }
            merged = clone_tree(current_tree)
            current_map = {item.node_id: item for item in flatten_tree(merged)}
            for ai_node in flatten_tree(candidate_tree):
                current_node = current_map.get(ai_node.node_id)
                if current_node is None:
                    parent_id = self._parent_id(candidate_tree, ai_node.node_id)
                    siblings = merged
                    if parent_id and parent_id in current_map:
                        siblings = current_map[parent_id].children
                    siblings.append(clone_tree([ai_node])[0])
                    current_map = {
                        item.node_id: item for item in flatten_tree(merged)
                    }
                    continue
                conflict = conflict_by_node.get(ai_node.node_id)
                if current_node.locked:
                    continue
                if conflict and conflict.resolution == "pending":
                    continue
                if conflict and conflict.resolution == "accept_ai":
                    current_node.title = ai_node.title
                    current_node.body_markdown = ai_node.body_markdown
                    current_node.anchor = ai_node.anchor
                    current_node.note_markdown = ai_node.note_markdown
                elif conflict and conflict.resolution == "manual_merge":
                    manual = conflict.current_node_json
                    if manual:
                        current_node.title = str(manual.get("title", current_node.title))
                        current_node.body_markdown = str(
                            manual.get("bodyMarkdown", current_node.body_markdown)
                        )
                        current_node.anchor = manual.get("anchor")
                        current_node.note_markdown = str(
                            manual.get("noteMarkdown", current_node.note_markdown)
                        )
                elif current_node.origin != "user":
                    current_node.title = ai_node.title
                    current_node.body_markdown = ai_node.body_markdown
                    current_node.anchor = ai_node.anchor
                    current_node.note_markdown = ai_node.note_markdown
            validate_tree(merged)
            revision = self._append_revision(
                session,
                outline,
                tree=merged,
                base=current,
                status="applied",
                source="ai_merge",
                actor_id=command.actor.actor_id,
                change_summary="应用 AI 大纲候选",
                ai_run_id=candidate.ai_run_id,
            )
            event = emit_outline_event(
                session,
                event_type="outline_updated",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "revisionId": revision.id,
                    "revisionNo": revision.revision_no,
                    "baseRevisionNo": revision.base_revision_no,
                    "source": "ai_merge",
                    "changedNodeIds": [
                        item.node_id for item in flatten_tree(merged)
                    ],
                    "changeSummary": revision.change_summary,
                },
            )
            result = self._mutation_result(session, outline, revision, [event])
            self._save_receipt(
                session,
                command.client_mutation_id,
                "merge_apply",
                request_hash,
                outline.id,
                result,
            )
            return result

    # ------------------------------------------------------------------
    # Project progress, Reader and events
    # ------------------------------------------------------------------

    def set_completed(
        self, command: SetOutlineNodeCompletedCommand
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            outline = self._require_outline(session, command.outline_id)
            current = self._current_revision(session, outline)
            tree = self._load_tree(session, current.id)
            node, _, _ = find_node(tree, command.node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", command.node_id)
            state = session.get(
                ProjectOutlineNodeState,
                (command.project_id, command.outline_id, command.node_id),
            )
            now = utc_now_iso()
            if state is None:
                state = ProjectOutlineNodeState(
                    project_id=command.project_id,
                    outline_id=command.outline_id,
                    node_id=command.node_id,
                    completed=command.completed,
                    completed_at=now if command.completed else None,
                    last_viewed_at=now,
                    updated_at=now,
                )
                session.add(state)
            else:
                state.completed = command.completed
                state.completed_at = now if command.completed else None
                state.last_viewed_at = now
                state.updated_at = now
            session.flush()
            emit_outline_event(
                session,
                event_type="outline_progress_updated",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "projectId": command.project_id,
                    "nodeId": command.node_id,
                    "completed": command.completed,
                },
            )
            return {
                "projectId": state.project_id,
                "outlineId": state.outline_id,
                "nodeId": state.node_id,
                "completed": state.completed,
                "completedAt": state.completed_at,
                "lastViewedAt": state.last_viewed_at,
                "updatedAt": state.updated_at,
            }

    def node_clicked(
        self,
        outline_id: str,
        node_id: str,
        actor: ActorContext,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            outline = self._require_outline(session, outline_id)
            current = self._current_revision(session, outline)
            node, _, _ = find_node(self._load_tree(session, current.id), node_id)
            if node is None:
                raise OutlineError("NODE_NOT_FOUND", node_id)
            event = emit_outline_event(
                session,
                event_type="outline_node_clicked",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "nodeId": node.node_id,
                    "revisionNo": current.revision_no,
                    "anchor": node.anchor,
                    "requestedReaderCommandSeq": None,
                },
            )
            event["actorId"] = actor.actor_id
            return event

    def switch_revision_view(
        self,
        outline_id: str,
        revision_no: int,
        *,
        mode: str = "view",
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.factory) as session:
            outline = self._require_outline(session, outline_id)
            target = self._require_revision(session, outline_id, revision_no)
            event = emit_outline_event(
                session,
                event_type="revision_switch",
                document_id=outline.document_id,
                outline_id=outline.id,
                payload={
                    "fromRevisionNo": outline.current_revision_no or None,
                    "toRevisionNo": target.revision_no,
                    "mode": "compare" if mode == "compare" else "view",
                    "becameCurrent": False,
                },
            )
            if actor:
                event["actorId"] = actor.actor_id
            return event

    def events_after(
        self,
        *,
        after_sequence: int = 0,
        document_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        from ..storage.v116.orm import OutlineEventOutbox

        with session_scope(self.factory) as session:
            statement = sa.select(OutlineEventOutbox).where(
                OutlineEventOutbox.sequence > after_sequence
            )
            if document_id:
                statement = statement.where(
                    OutlineEventOutbox.document_id == document_id
                )
            rows = session.scalars(
                statement.order_by(OutlineEventOutbox.sequence).limit(limit)
            ).all()
            return [event_out(row) for row in rows]

    # ------------------------------------------------------------------
    # Idempotency and integrity
    # ------------------------------------------------------------------

    def _receipt(
        self,
        session: Session,
        mutation_id: str,
        action: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = session.get(OutlineMutationReceipt, mutation_id)
        if row is None:
            return None
        if row.action != action or row.request_hash != request_hash:
            raise OutlineError(
                "REVISION_CONFLICT",
                "clientMutationId 已被不同请求使用",
            )
        return copy.deepcopy(row.response_json)

    @staticmethod
    def _save_receipt(
        session: Session,
        mutation_id: str,
        action: str,
        request_hash: str,
        outline_id: str,
        response: dict[str, Any],
    ) -> None:
        session.add(
            OutlineMutationReceipt(
                client_mutation_id=mutation_id,
                action=action,
                request_hash=request_hash,
                outline_id=outline_id,
                response_json=copy.deepcopy(response),
                created_at=utc_now_iso(),
            )
        )
        session.flush()

    @staticmethod
    def _check_base(outline: ReadingOutline, base_revision_no: int) -> None:
        if outline.current_revision_no != base_revision_no:
            raise revision_conflict(outline)

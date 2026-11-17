"""P0-02 candidate inbox rendered inside the Streamlit literature workspace."""

from __future__ import annotations

import uuid
from typing import Any

import streamlit as st

from ..config import PATHS
from ..screening.contracts import (
    BulkCandidateDecisionCommand,
    CandidateListQuery,
    RequestScreeningCommand,
    SubmitCandidateDecisionCommand,
)
from ..screening.db import deploy_screening_schema
from ..screening.errors import ScreeningError
from ..screening.service import CandidateService
from ..screening.worker import ScreeningWorker
from ..storage.v116.db import deploy_schema, engine_for, session_factory
from . import pdf_workspace, theme

STATUS_LABEL = {
    "uploading": "上传中",
    "ready": "待评估",
    "screening_queued": "评估排队",
    "screening": "评估中",
    "screened": "已评估",
    "extraction_failed": "解析失败",
    "screening_failed": "评估失败",
    "confirming": "入库中",
    "confirmed": "已入库",
    "skipped": "已跳过",
    "deleted": "已删除",
}


@st.cache_resource
def _runtime():
    engine = engine_for(PATHS.db_path)
    deploy_schema(engine, note="P0-02 Streamlit candidate inbox")
    deploy_screening_schema(engine)
    factory = session_factory(engine)
    service = CandidateService(factory)
    return factory, service, ScreeningWorker(service, factory)


def _decision(
    service: CandidateService,
    candidate: dict[str, Any],
    decision: str,
    *,
    duplicate_action: str | None = None,
    duplicate_document_id: str | None = None,
) -> dict[str, Any]:
    key = f"ui-{candidate['id']}-{decision}-{uuid.uuid4().hex}"
    return service.submit_decision(
        SubmitCandidateDecisionCommand(
            candidateId=candidate["id"],
            expectedRevision=candidate["revision"],
            decision=decision,
            idempotencyKey=key,
            clientMutationId=key,
            baseRevisionNo=candidate["revision"],
            duplicateAction=duplicate_action,
            duplicateDocumentId=duplicate_document_id,
            actorId="streamlit-user",
        )
    )


def _open_confirmed(candidate: dict[str, Any], project_id: str, *, read: bool) -> None:
    document_id = candidate.get("confirmedDocumentId")
    if document_id:
        st.session_state["selected_global_document_id"] = document_id
        st.session_state["selected_paper_project"] = project_id
    if read:
        st.session_state["pdf_workspace_mode"] = "read"
    st.session_state["screening_notice"] = (
        "success",
        "已入库并打开文献" if read else "已入库并进入文献详情",
    )
    st.rerun()


def _candidate_row(candidate: dict[str, Any], selected: str | None) -> None:
    status = STATUS_LABEL.get(candidate["status"], candidate["status"])
    duplicate = candidate.get("duplicateStatus", "none")
    cols = st.columns([0.2, 4, 1.2])
    checked = cols[0].checkbox(
        "选择", key=f"screen_sel_{candidate['id']}", label_visibility="collapsed"
    )
    st.session_state.setdefault("screening_selected_ids", [])
    ids = set(st.session_state["screening_selected_ids"])
    if checked:
        ids.add(candidate["id"])
    else:
        ids.discard(candidate["id"])
    st.session_state["screening_selected_ids"] = sorted(ids)
    if cols[1].button(
        f"**{candidate['displayTitle'] or candidate['originalFilename']}**",
        key=f"screen_open_{candidate['id']}",
        width="stretch",
    ):
        st.session_state["screening_selected_candidate"] = candidate["id"]
        st.rerun()
    cols[2].caption(status)
    if duplicate != "none":
        st.caption(f"重复提示：{duplicate}")


def _detail(service: CandidateService, candidate_id: str, project_id: str) -> None:
    detail = service.detail(candidate_id)
    candidate = detail["candidate"]
    st.markdown(f"### {candidate['displayTitle'] or candidate['originalFilename']}")
    st.caption(
        f"{STATUS_LABEL.get(candidate['status'], candidate['status'])} · "
        f"版本 {candidate['revision']} · {candidate.get('pageCount') or '页数未知'} 页"
    )
    if candidate.get("errorMessage"):
        st.error(candidate["errorMessage"])
    evaluation = detail.get("latestEvaluation")
    if evaluation:
        scores = st.columns(4)
        for column, label, key in zip(
            scores,
            ["主题", "学术", "引用", "阅读"],
            [
                "topicMatchScore",
                "academicValueScore",
                "citationPotentialScore",
                "readingPriorityScore",
            ],
        ):
            column.metric(label, evaluation[key])
        st.write(evaluation["conclusionSummary"])
        st.caption(
            f"置信度 {evaluation['confidenceAdjusted']:.0%} · "
            f"{evaluation['readRecommendation']} · {evaluation['citationRecommendation']}"
        )
        for reason in evaluation["reasons"]:
            st.markdown(f"- **{reason['dimension']}**：{reason['reasonText']}")
    elif candidate["status"] in {"ready", "screening_failed"}:
        if st.button("开始 AI 初筛", type="primary", key=f"screen_run_{candidate_id}"):
            try:
                service.request_screening(
                    RequestScreeningCommand(
                        candidateId=candidate_id,
                        clientMutationId=f"screen-{uuid.uuid4().hex}",
                        baseRevisionNo=candidate["revision"],
                        actorId="streamlit-user",
                    )
                )
                _runtime()[2].process_one(worker_id="streamlit-worker")
                st.rerun()
            except ScreeningError as exc:
                st.error(f"{exc.code}: {exc}")
    duplicates = detail.get("duplicateMatches") or []
    if duplicates:
        st.markdown("#### 重复文献")
        for match in duplicates:
            st.warning(
                f"{match['matchBasis']} · 相似度 {match['titleSimilarity']:.0%} · "
                f"document {match['documentId']}"
            )
        mode = st.radio(
            "重复处理",
            ["merge", "add_attachment", "keep_independent"],
            horizontal=True,
            key=f"dup_mode_{candidate_id}",
        )
        st.session_state[f"dup_mode_value_{candidate_id}"] = mode
    actions = st.columns(4)
    if actions[0].button("确认入库", key=f"confirm_{candidate_id}"):
        result = _decision(
            service,
            candidate,
            "confirmed_store",
            duplicate_action=st.session_state.get(f"dup_mode_value_{candidate_id}"),
            duplicate_document_id=candidate.get("duplicateDocumentId"),
        )
        _open_confirmed(result["candidate"], project_id, read=False)
    if actions[1].button("标记精读", key=f"deep_{candidate_id}"):
        result = _decision(
            service,
            candidate,
            "confirmed_deep_read",
            duplicate_action=st.session_state.get(f"dup_mode_value_{candidate_id}"),
            duplicate_document_id=candidate.get("duplicateDocumentId"),
        )
        _open_confirmed(result["candidate"], project_id, read=True)
    if actions[2].button("跳过", key=f"skip_{candidate_id}"):
        _decision(service, candidate, "skipped")
        st.rerun()
    if actions[3].button("删除", key=f"delete_{candidate_id}"):
        _decision(service, candidate, "deleted")
        st.rerun()


def render(project) -> None:
    _factory, service, worker = _runtime()
    notice = st.session_state.pop("screening_notice", None)
    if notice:
        {"success": st.success, "error": st.error}[notice[0]](notice[1])
    st.markdown("#### 待筛选候选")
    upload = st.file_uploader(
        "上传候选文献",
        type=["pdf", "png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key=f"candidate_upload_{project.id}",
    )
    if upload and st.button("上传并评估", type="primary"):
        batch = f"batch-{uuid.uuid4().hex}"
        for item in upload:
            try:
                created = service.upload_candidate(
                    project_id=project.id,
                    batch_id=batch,
                    file_name=item.name,
                    mime_type=item.type or "application/octet-stream",
                    chunks=[bytes(item.getbuffer())],
                    created_by="streamlit-user",
                    client_mutation_id=f"upload-{project.id}-{item.name}-{uuid.uuid4().hex}",
                )
                candidate = created["candidate"]
                service.request_screening(
                    RequestScreeningCommand(
                        candidateId=candidate["id"],
                        clientMutationId=f"screen-{candidate['id']}",
                        baseRevisionNo=candidate["revision"],
                        actorId="streamlit-user",
                    )
                )
                worker.process_one(worker_id="streamlit-worker")
            except ScreeningError as exc:
                st.error(f"{exc.code}: {exc}")
        st.rerun()

    query = CandidateListQuery(projectId=project.id, limit=100)
    page = service.list(query)
    selected_id = st.session_state.get("screening_selected_candidate")
    left, right = st.columns([1.1, 1])
    with left:
        counts = page.get("countsByStatus", {})
        st.caption(
            " · ".join(
                f"{STATUS_LABEL.get(key, key)} {value}"
                for key, value in sorted(counts.items())
            )
        )
        for candidate in page["items"]:
            _candidate_row(candidate, selected_id)
        selected_ids = st.session_state.get("screening_selected_ids", [])
        if selected_ids:
            st.caption(f"已选 {len(selected_ids)} 条")
            bulk = st.columns(3)
            if bulk[0].button("批量确认入库"):
                service.submit_bulk(
                    BulkCandidateDecisionCommand(
                        candidateIds=selected_ids,
                        decision="confirmed_store",
                        idempotencyPrefix=f"bulk-{uuid.uuid4().hex}",
                        actorId="streamlit-user",
                    )
                )
                st.rerun()
            if bulk[1].button("批量跳过"):
                service.submit_bulk(
                    BulkCandidateDecisionCommand(
                        candidateIds=selected_ids,
                        decision="skipped",
                        idempotencyPrefix=f"bulk-{uuid.uuid4().hex}",
                        actorId="streamlit-user",
                    )
                )
                st.rerun()
            if bulk[2].button("批量删除"):
                service.submit_bulk(
                    BulkCandidateDecisionCommand(
                        candidateIds=selected_ids,
                        decision="deleted",
                        idempotencyPrefix=f"bulk-{uuid.uuid4().hex}",
                        actorId="streamlit-user",
                    )
                )
                st.rerun()
    with right:
        if selected_id:
            _detail(service, selected_id, project.id)
        else:
            st.info("选择一条候选查看评分、理由和重复信息。")

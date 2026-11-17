"""Confirmation-gated write tools for the project assistant.

Only one write capability is exposed in this increment: propose a text-field edit
to an existing plan draft. Proposal generation is side-effect free. Applying a
proposal requires a fresh UI confirmation and an optimistic before-value check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..planning import draft as plan_store
from ..storage import repository as repo
from ..storage.models import (
    DecisionKind,
    DecisionRecord,
    DecisionStatus,
    Paper,
    PaperClaim,
    PlanDraft,
    PlanDraftStatus,
)
from . import chat
from .client import LLMClient
from .providers import LLMError

log = logging.getLogger(__name__)

WRITE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "propose_plan_edit": {
        "kind": "write",
        "description": "为现有计划草稿的单个文本字段生成修改建议",
        "params": {
            "draft_id": {"type": "string", "required": True},
            "field": {
                "type": "enum",
                "required": True,
                "values": [
                    "question",
                    "objective",
                    "data_requirements",
                    "success_criteria",
                    "user_notes",
                ],
            },
            "instruction": {"type": "string", "required": True, "max": 2000},
        },
        "requires_confirmation": True,
    }
}

PLAN_FIELD_LABELS: dict[str, str] = {
    "question": "研究问题",
    "objective": "研究目标",
    "data_requirements": "数据需求",
    "success_criteria": "成功判据",
    "user_notes": "用户补充",
}
MAX_AFTER_CHARS = 4000

PLAN_EDIT_SYSTEM = """你是 ResearchPilot 的计划草稿编辑助手。

你的任务是根据用户要求和给定的已核实文献条目，为**一个允许修改的文本字段**提出完整替换文本。
这只是建议，不会自动生效。

规则：
1. 只允许修改 question / objective / data_requirements / success_criteria / user_notes 之一。
2. 不得编造文献、DOI、页码、实验数值、运行结果或尚未完成的验证。
3. 可引用的证据只有用户给出的条目；claim_ids 必须是给定编号的子集。
4. after 必须是可直接替换的完整文本，不写“建议考虑……”这类半成品。
5. why 说明改动依据与影响；没有依据时明确写“用户要求”。
6. 不修改已确认版本，只针对草稿。
7. 只输出 JSON。

JSON 结构：
{"after": "完整替换文本", "why": "改动理由", "claim_ids": ["claim_x"]}
"""

PLAN_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "after": {"type": "string"},
        "why": {"type": "string"},
        "claim_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["after", "why", "claim_ids"],
}


@dataclass
class PlanEditProposal:
    draft_id: str
    project_id: str
    field_name: str
    field_label: str
    before: str
    after: str
    why: str
    claim_ids: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "project_id": self.project_id,
            "field_name": self.field_name,
            "field_label": self.field_label,
            "before": self.before,
            "after": self.after,
            "why": self.why,
            "claim_ids": list(self.claim_ids),
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanEditProposal":
        return cls(**{
            "draft_id": str(value.get("draft_id") or ""),
            "project_id": str(value.get("project_id") or ""),
            "field_name": str(value.get("field_name") or ""),
            "field_label": str(value.get("field_label") or ""),
            "before": str(value.get("before") or ""),
            "after": str(value.get("after") or ""),
            "why": str(value.get("why") or ""),
            "claim_ids": [str(item) for item in value.get("claim_ids") or []],
            "provider": str(value.get("provider") or ""),
            "model": str(value.get("model") or ""),
            "created_at": str(value.get("created_at") or ""),
        })


def latest_editable_draft(project_id: str) -> PlanDraft | None:
    drafts = [
        item for item in repo.list_all(PlanDraft, project_id)
        if item.status is PlanDraftStatus.DRAFT
    ]
    return max(
        drafts,
        key=lambda item: (item.updated_at or item.created_at, item.id),
        default=None,
    )


def build_plan_edit_proposal(
    draft: PlanDraft,
    field_name: str,
    instruction: str,
    *,
    client: LLMClient,
    claims: list[PaperClaim] | None = None,
) -> PlanEditProposal:
    if draft.status is not PlanDraftStatus.DRAFT:
        raise ValueError("只能修改草稿；已确认版本不可覆盖")
    if field_name not in PLAN_FIELD_LABELS:
        raise ValueError("不允许修改这个字段")
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("请说明希望怎样修改")
    claims = claims if claims is not None else [
        item for item in repo.list_all(PaperClaim, draft.project_id)
        if item.quote_verified
    ]
    papers = {item.id: item for item in repo.list_all(Paper, draft.project_id)}
    evidence_lines = []
    for claim in claims[:16]:
        title = papers.get(claim.paper_id).title if claim.paper_id in papers else ""
        page = f"第 {claim.page} 页" if claim.page else (claim.locator or "位置未定")
        evidence_lines.append(
            f"- [{claim.id}] {claim.claim}｜{title}｜{page}｜引文：{claim.quote[:180]}"
        )
    before = str(getattr(draft, field_name, "") or "")
    user = (
        f"当前字段：{PLAN_FIELD_LABELS[field_name]}\n"
        f"当前内容：\n{before or '（空）'}\n\n"
        f"用户修改要求：\n{instruction}\n\n"
        f"当前草稿摘要：\n{chat.draft_summary(draft)}\n\n"
        "可引用的已核实条目：\n"
        + ("\n".join(evidence_lines) if evidence_lines else "（无）")
        + "\n\n只输出 JSON。"
    )
    payload, response = client.complete_json(
        PLAN_EDIT_SYSTEM, user, json_schema=PLAN_EDIT_SCHEMA,
        max_tokens=1800, temperature=0.2,
    )
    after = str(payload.get("after") or "").strip()
    why = str(payload.get("why") or "").strip()
    if not after:
        raise LLMError("模型没有给出替换文本")
    if len(after) > MAX_AFTER_CHARS:
        raise LLMError(f"替换文本超过 {MAX_AFTER_CHARS} 字上限")
    if not why:
        raise LLMError("模型没有说明修改理由")
    known = {item.id for item in claims}
    claim_ids = [str(item) for item in payload.get("claim_ids") or []]
    unknown = [item for item in claim_ids if item not in known]
    if unknown:
        raise LLMError("模型引用了未提供的证据条目")
    credentials = client.store.current()
    return PlanEditProposal(
        draft_id=draft.id,
        project_id=draft.project_id,
        field_name=field_name,
        field_label=PLAN_FIELD_LABELS[field_name],
        before=before,
        after=after,
        why=why,
        claim_ids=list(dict.fromkeys(claim_ids)),
        provider=getattr(credentials, "provider", ""),
        model=response.model,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def apply_plan_edit_proposal(proposal: PlanEditProposal) -> PlanDraft:
    draft = repo.get(PlanDraft, proposal.draft_id)
    if draft is None or draft.project_id != proposal.project_id:
        raise ValueError("目标草稿不存在或不属于当前项目")
    if draft.status is not PlanDraftStatus.DRAFT:
        raise ValueError("草稿已经确认，不能再应用修改")
    current = str(getattr(draft, proposal.field_name, "") or "")
    if current != proposal.before:
        raise ValueError("草稿内容在确认前已经变化，请重新生成建议")
    updated = plan_store.apply_edits(
        draft, text={proposal.field_name: proposal.after}
    )
    repo.save(DecisionRecord(
        id=repo.new_id("dec"),
        project_id=draft.project_id,
        series_id=plan_store.series_of(draft),
        kind=DecisionKind.AI_SUGGESTION,
        text=f"{proposal.field_label}：{proposal.after[:80]}",
        status=DecisionStatus.ACCEPTED,
        evidence_claim_ids=list(proposal.claim_ids),
        draft_id=draft.id,
        note=proposal.why,
        decided_at=datetime.now().isoformat(timespec="seconds"),
    ))
    log.info(
        "project assistant applied plan edit project=%s draft=%s field=%s",
        draft.project_id, draft.id, proposal.field_name,
    )
    return updated


def reject_plan_edit_proposal(proposal: PlanEditProposal) -> None:
    repo.save(DecisionRecord(
        id=repo.new_id("dec"),
        project_id=proposal.project_id,
        kind=DecisionKind.AI_SUGGESTION,
        text=f"{proposal.field_label}（未采纳）：{proposal.after[:80]}",
        status=DecisionStatus.REJECTED,
        evidence_claim_ids=list(proposal.claim_ids),
        draft_id=proposal.draft_id,
        note=proposal.why,
        decided_at=datetime.now().isoformat(timespec="seconds"),
    ))
    log.info(
        "project assistant rejected plan edit project=%s draft=%s field=%s",
        proposal.project_id, proposal.draft_id, proposal.field_name,
    )

"""科研对话与决策台账（P0-B）。

对话持久化在 ``discussion_messages``，决策持久化在 ``decision_records``，
两者都带 ``project_id + series_id``，跨项目与跨主题不会串。

三条纪律：

1. **建议不等于修改**：AI 的建议默认 ``pending``，只有用户点"应用到草稿"
   才通过 ``planning.draft.apply_edits`` 改草稿；已确认版本拒绝覆盖。
2. **对话不是证据**：助手消息带"AI 讨论，未核实"标记，永远不当文献证据用。
3. **采纳与否都留痕**：驳回也写进决策台账（``rejected``），不删除。
"""

from __future__ import annotations

from datetime import datetime

from ..agent import chat as chat_mod
from ..agent import chat_schemas
from ..agent.client import LLMClient
from ..storage import repository as repo
from ..storage.models import (
    DecisionKind,
    DecisionRecord,
    DecisionStatus,
    DiscussionMessage,
    MessageRole,
    PlanVersion,
    SuggestionStatus,
)
from . import draft as plan_store


def _now() -> str:
    # 微秒精度：一轮对话里"先问后答"往往在同一秒内完成，秒级时间戳会让
    # 历史顺序变得不确定（同秒内只能靠随机 id 排）。
    return datetime.now().isoformat(timespec="microseconds")


def conversation(project_id: str, *, series_id: str | None = None) -> list[DiscussionMessage]:
    return repo.list_messages(project_id, series_id=series_id)


def record_user(
    project_id: str,
    *,
    series_id: str,
    draft_id: str,
    text: str,
    context: dict | None = None,
) -> DiscussionMessage:
    message = DiscussionMessage(
        id=repo.new_id("msg"),
        project_id=project_id,
        series_id=series_id,
        draft_id=draft_id,
        role=MessageRole.USER,
        content=text,
        context=dict(context or {}),
        created_at=_now(),
    )
    return repo.save(message)


def ask_and_record(
    client: LLMClient,
    *,
    project,
    question: str,
    claims: list,
    item=None,
    step_order: int | None = None,
    role: str = "discuss",
    series_id: str = "",
) -> tuple[chat_mod.ChatOutcome, DiscussionMessage]:
    """问一次并把用户问题与助手回答都落库。失败也落一条助手消息（带原因）。"""
    history = conversation(project.id, series_id=series_id or None)
    user_message = record_user(
        project.id,
        series_id=series_id,
        draft_id=getattr(item, "id", "") if item is not None else "",
        text=question,
        context={
            "claim_ids": [claim.id for claim in claims],
            "step_order": step_order,
            "item_id": getattr(item, "id", "") if item is not None else "",
        },
    )

    outcome = chat_mod.ask(
        client,
        project=project,
        question=question,
        claims=claims,
        item=item,
        step_order=step_order,
        role=role,
        history=[*history, user_message],
    )

    labels = [chat_schemas.ASSISTANT_LABEL]
    if not outcome.ok:
        labels.append("本次未得到回答")
    assistant = repo.save(DiscussionMessage(
        id=repo.new_id("msg"),
        project_id=project.id,
        series_id=series_id,
        draft_id=getattr(item, "id", "") if item is not None else "",
        role=MessageRole.ASSISTANT,
        content=outcome.reply,
        context={
            "claim_ids": [claim.id for claim in claims],
            "step_order": step_order,
            "suggestion_count": len(outcome.suggestions),
        },
        suggestions=list(outcome.suggestions),
        labels=labels,
        provider=outcome.provider,
        model=outcome.model,
        error="" if outcome.ok else outcome.reason,
        created_at=_now(),
    ))

    # AI 自己提的建议/质疑先记成"待定"，采纳与否由用户决定
    for item_decision in outcome.decisions:
        repo.save(DecisionRecord(
            id=repo.new_id("dec"),
            project_id=project.id,
            series_id=series_id,
            kind=(
                DecisionKind.AI_OBJECTION
                if item_decision["kind"] == "ai_objection"
                else DecisionKind.AI_SUGGESTION
            ),
            text=item_decision["text"],
            status=DecisionStatus.PROPOSED,
            evidence_claim_ids=list(item_decision.get("claim_ids") or []),
            source_message_id=assistant.id,
            draft_id=assistant.draft_id,
            created_at=_now(),
        ))
    return outcome, assistant


def _find_suggestion(message: DiscussionMessage, suggestion_id: str) -> dict | None:
    for suggestion in message.suggestions:
        if suggestion.get("id") == suggestion_id:
            return suggestion
    return None


def _save_suggestion(message: DiscussionMessage, updated: dict) -> DiscussionMessage:
    message.suggestions = [
        updated if item.get("id") == updated.get("id") else item
        for item in message.suggestions
    ]
    return repo.save(message)


def _suggestion_targets(message: DiscussionMessage, draft) -> bool:
    """这条建议能不能落到这份草稿上。

    P0-A 之前只有一条通路：建议是**针对这份草稿**提的，所以要求 ``draft.id ==
    message.draft_id``。于是"在已确认版本上继续讨论"提的建议没有落点——版本不可改，
    输入框也就被连带封死了。

    现在多一条通路：建议针对已确认版本提出，用户点"派生草稿并应用"时把它落到
    **同一系列、由该版本派生出来的新草稿**上。限制依然严格：

    - 项目必须一致；
    - 系列必须一致（不同主题的修订历史不混）；
    - 草稿必须确实派生自那条建议所属的版本。
    """
    if draft is None:
        return False
    draft_id = getattr(draft, "id", "")
    if draft_id and draft_id == message.draft_id:
        return True
    if getattr(draft, "project_id", "") != message.project_id:
        return False
    if not message.series_id or plan_store.series_of(draft) != message.series_id:
        return False
    source = repo.get(PlanVersion, message.draft_id)
    if source is None or source.project_id != message.project_id:
        return False
    derived = (
        getattr(draft, "derived_from_version_id", "")
        or getattr(draft, "confirmed_version_id", "")
    )
    return derived == source.id


def _accept_decision_for_message(
    message: DiscussionMessage, *, text: str, claim_ids: list[str], draft_id: str
) -> DecisionRecord:
    existing = next(
        (
            item for item in repo.list_decisions(message.project_id, series_id=message.series_id)
            if item.source_message_id == message.id and item.status is DecisionStatus.PROPOSED
        ),
        None,
    )
    now = _now()
    if existing is not None:
        existing.status = DecisionStatus.ACCEPTED
        existing.text = existing.text or text
        existing.evidence_claim_ids = existing.evidence_claim_ids or list(claim_ids)
        existing.draft_id = draft_id
        existing.decided_at = now
        existing.updated_at = now
        return repo.save(existing)
    return repo.save(DecisionRecord(
        id=repo.new_id("dec"),
        project_id=message.project_id,
        series_id=message.series_id,
        kind=DecisionKind.AI_SUGGESTION,
        text=text,
        status=DecisionStatus.ACCEPTED,
        evidence_claim_ids=list(claim_ids),
        source_message_id=message.id,
        draft_id=draft_id,
        decided_at=now,
    ))


def apply_suggestion(
    message_id: str, suggestion_id: str, *, draft
) -> tuple[dict, DecisionRecord]:
    """把一条建议应用到草稿。**这是"建议变成计划"的唯一入口。**"""
    message = repo.get(DiscussionMessage, message_id)
    if message is None:
        raise ValueError("这条建议所属的对话记录不存在")
    suggestion = _find_suggestion(message, suggestion_id)
    if suggestion is None:
        raise ValueError("建议不存在")
    if suggestion.get("status") == SuggestionStatus.APPLIED.value:
        raise ValueError("这条建议已经应用过了")
    if not _suggestion_targets(message, draft):
        raise ValueError("这条建议属于另一份草稿，或当前没有可修改的草稿")

    field_name = suggestion["field"]
    if suggestion["target"] == "draft":
        plan_store.apply_edits(draft, text={field_name: suggestion["after"]})
    else:
        order = int(suggestion.get("step_order") or 1)
        plan_store.apply_edits(draft, steps={order - 1: {field_name: suggestion["after"]}})

    suggestion["status"] = SuggestionStatus.APPLIED.value
    suggestion["applied_at"] = _now()
    _save_suggestion(message, suggestion)
    decision = _accept_decision_for_message(
        message,
        text=f"{suggestion.get('field_label') or field_name}：{suggestion['after'][:60]}",
        claim_ids=list(suggestion.get("claim_ids") or []),
        draft_id=draft.id,
    )
    return suggestion, decision


def reject_suggestion(
    message_id: str, suggestion_id: str, *, reason: str = ""
) -> dict:
    message = repo.get(DiscussionMessage, message_id)
    if message is None:
        raise ValueError("这条建议所属的对话记录不存在")
    suggestion = _find_suggestion(message, suggestion_id)
    if suggestion is None:
        raise ValueError("建议不存在")
    suggestion["status"] = SuggestionStatus.REJECTED.value
    suggestion["reject_reason"] = reason
    suggestion["rejected_at"] = _now()
    _save_suggestion(message, suggestion)

    now = _now()
    for item in repo.list_decisions(message.project_id, series_id=message.series_id):
        if item.source_message_id == message.id and item.status is DecisionStatus.PROPOSED:
            item.status = DecisionStatus.REJECTED
            item.note = reason or item.note
            item.decided_at = now
            item.updated_at = now
            repo.save(item)
    return suggestion


def log_user_decision(
    project_id: str,
    *,
    series_id: str,
    text: str,
    kind: DecisionKind = DecisionKind.USER_IDEA,
    claim_ids: list[str] | None = None,
    draft_id: str = "",
) -> DecisionRecord:
    """用户自己的想法/决定。默认直接记为已采纳（这就是用户的态度）。"""
    now = _now()
    return repo.save(DecisionRecord(
        id=repo.new_id("dec"),
        project_id=project_id,
        series_id=series_id,
        kind=kind,
        text=text,
        status=DecisionStatus.ACCEPTED,
        evidence_claim_ids=list(claim_ids or []),
        draft_id=draft_id,
        decided_at=now,
    ))


def decisions(project_id: str, *, series_id: str | None = None,
              status: DecisionStatus | None = None) -> list[DecisionRecord]:
    return repo.list_decisions(project_id, series_id=series_id, status=status)

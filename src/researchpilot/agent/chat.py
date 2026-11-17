"""AI 多轮科研协作调用（P0-B）。

一次调用拿到三样东西：讨论回复、**待用户确认**的修改建议、值得记录的决策条目。
所有校验都在本地做：

- 建议只能落在白名单字段上，步骤序号必须在当前计划范围内；
- 建议里的 ``before`` 一律由本地从当前草稿/版本取，**不采信模型自报的原值**
  （否则界面上会出现一个不存在的"改动前"）；
- 引用的编号必须在给定的条目里，对不上的编号丢弃并记原因；
- AI 只能提 ``ai_suggestion`` / ``ai_objection`` 两类决策，"采纳"由用户决定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..storage import repository as repo
from ..storage.models import PaperClaim
from . import chat_prompts
from . import chat_schemas
from .client import LLMClient
from .providers import LLMBadResponseError, LLMError

log = logging.getLogger(__name__)

MAX_CONTEXT_CLAIMS = 60


@dataclass
class ChatOutcome:
    status: str                     # ok | failed
    reply: str = ""
    suggestions: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    raw_text_len: int = 0
    finish_reason: str = ""
    reason: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def draft_summary(draft) -> str:
    """把当前方案压成一段给模型看的文字。

    讨论上下文既可能是草稿（PlanDraft，有 user_notes），也可能是已确认版本（PlanVersion，
    用 notes 存用户设定）。这里做字段兜底，避免"已确认版本不能讨论"。
    """
    if draft is None:
        return ""
    notes = getattr(draft, "user_notes", "") or getattr(draft, "notes", "")
    lines = [
        f"研究问题：{draft.question or '（空）'}",
        f"研究目标：{draft.objective or '（空）'}",
        f"数据需求：{draft.data_requirements or '（空）'}",
        f"成功判据：{draft.success_criteria or '（空）'}",
        f"用户设定：{notes or '（空）'}",
        "步骤：",
    ]
    for index, step in enumerate(draft.steps, start=1):
        if isinstance(step, dict):
            order = step.get("order") or index
            evidence = step.get("evidence") or []
            refs = "、".join(item.get("claim_id", "") for item in evidence) or "无"
            lines.append(
                f"  {order}. {step.get('title', '')}"
                f"｜做法：{step.get('method', '') or '（空）'}"
                f"｜预期指标：{step.get('expected_metric', '') or '（空）'}"
                f"｜引用条目：{refs}"
            )
        else:
            lines.append(f"  {index}. {step}")
    return "\n".join(lines)


def current_value(item, *, target: str, field_name: str, step_order: int | None) -> str:
    """取字段当前值（建议里的"改动前"以它为准）。"""
    if item is None:
        return ""
    if target == "draft":
        return str(getattr(item, field_name, "") or "")
    steps = list(getattr(item, "steps", []) or [])
    index = (step_order or 1) - 1
    if not (0 <= index < len(steps)):
        return ""
    step = steps[index]
    if not isinstance(step, dict):
        return str(step) if field_name == "title" else ""
    return str(step.get(field_name, "") or "")


def _filter_claim_ids(raw_ids: list[str], known: dict[str, PaperClaim]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for raw in raw_ids or []:
        key = str(raw).strip()
        if key in known:
            if key not in kept:
                kept.append(key)
        else:
            dropped.append(key)
    return kept, dropped


def validate_suggestions(
    raw_items: list, *, item, known_claims: dict[str, PaperClaim]
) -> tuple[list[dict], list[dict]]:
    """把模型给的建议整理成可展示、可应用的候选。返回 (建议, 丢弃原因)。"""
    suggestions: list[dict] = []
    dropped: list[dict] = []
    step_count = len(getattr(item, "steps", []) or [])

    for raw in raw_items:
        target = str(getattr(raw, "target", "draft")).strip()
        field_name = str(getattr(raw, "field", "")).strip()
        after = str(getattr(raw, "after", "") or "").strip()
        step_order = getattr(raw, "step_order", None)

        if target not in chat_schemas.SUGGESTION_FIELDS:
            dropped.append({"field": field_name, "reason": f"未知的建议对象：{target!r}"})
            continue
        if field_name not in chat_schemas.SUGGESTION_FIELDS[target]:
            dropped.append({"field": field_name, "reason": "这个字段不允许由 AI 直接建议修改"})
            continue
        if not after:
            dropped.append({"field": field_name, "reason": "建议内容为空"})
            continue
        if target == "step":
            if not isinstance(step_order, int) or not (1 <= step_order <= step_count):
                dropped.append({
                    "field": field_name,
                    "reason": f"步骤序号 {step_order!r} 不在当前计划的 1–{step_count} 范围内",
                })
                continue

        before = current_value(item, target=target, field_name=field_name, step_order=step_order)
        if before == after:
            dropped.append({"field": field_name, "reason": "建议内容与当前值相同，无需修改"})
            continue

        kept_ids, bad_ids = _filter_claim_ids(getattr(raw, "claim_ids", []) or [], known_claims)
        suggestions.append({
            "id": repo.new_id("sug"),
            "target": target,
            "field": field_name,
            "field_label": chat_schemas.field_label(field_name),
            "step_order": step_order if target == "step" else None,
            "before": before,
            "after": after,
            "why": str(getattr(raw, "why", "") or ""),
            "claim_ids": kept_ids,
            "dropped_claim_ids": bad_ids,
            "status": "pending",
        })
    return suggestions, dropped


def validate_decisions(
    raw_items: list, *, known_claims: dict[str, PaperClaim]
) -> list[dict]:
    decisions: list[dict] = []
    for raw in raw_items:
        kind = str(getattr(raw, "kind", "") or "").strip()
        text = str(getattr(raw, "text", "") or "").strip()
        if kind not in chat_schemas.DECISION_KINDS or not text:
            continue
        kept_ids, _ = _filter_claim_ids(getattr(raw, "claim_ids", []) or [], known_claims)
        decisions.append({"kind": kind, "text": text, "claim_ids": kept_ids})
    return decisions


def ask(
    client: LLMClient,
    *,
    project,
    question: str,
    claims: list[PaperClaim],
    item=None,
    step_order: int | None = None,
    role: str = "discuss",
    history: list | None = None,
    max_tokens: int = 3000,
) -> ChatOutcome:
    """问一次。失败如实返回，不抛给界面。"""
    credentials = client.store.current()
    base = {
        "provider": getattr(credentials, "provider", "") if credentials else "",
    }
    if not question.strip():
        return ChatOutcome(status="failed", reason="问题为空", **base)

    chosen = [claim for claim in claims if (claim.quote or "").strip()][:MAX_CONTEXT_CLAIMS]
    known = {claim.id: claim for claim in chosen}
    claim_blocks = [
        (claim.id, f"{claim.claim}（引文：{(claim.quote or '')[:100]}）") for claim in chosen
    ]
    context_lines = [
        f"项目：{project.title}（{project.id}）",
        f"当前是在改：「{getattr(item, 'series_title', '') or '未命名方案'}」"
        + (f"的第 {step_order} 步" if step_order else ""),
        f"可引用条目 {len(chosen)} 条",
    ]
    if item is not None and getattr(item, "id", ""):
        context_lines.append(f"当前草稿编号：{item.id}")
    history_lines = [
        f"{'用户' if message.role.value == 'user' else '助手'}：{message.content[:200]}"
        for message in (history or [])
    ]

    user = chat_prompts.chat_user(
        role_label=chat_schemas.DISCUSSION_ROLES.get(role, "科研讨论"),
        question=question,
        context_lines=context_lines,
        history_lines=history_lines,
        claim_blocks=claim_blocks,
        draft_summary=draft_summary(item),
    )

    try:
        payload, response = client.complete_json(
            chat_prompts.CHAT_SYSTEM, user,
            json_schema=chat_schemas.CHAT_JSON_SCHEMA,
            max_tokens=max_tokens,
        )
    except LLMBadResponseError as exc:
        return ChatOutcome(status="failed", reason=f"模型返回的内容不是合法 JSON：{exc}", **base)
    except LLMError as exc:
        return ChatOutcome(status="failed", reason=str(exc), **base)

    try:
        parsed = chat_schemas.ChatPayload.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 结构不符也要有明确终态
        return ChatOutcome(
            status="failed",
            reason=f"模型返回的 JSON 结构不符合约定：{type(exc).__name__}: {str(exc)[:200]}",
            model=response.model, elapsed_s=response.elapsed_s, **base,
        )

    suggestions, dropped = validate_suggestions(
        list(parsed.suggestions), item=item, known_claims=known
    )
    decisions = validate_decisions(list(parsed.decisions), known_claims=known)
    return ChatOutcome(
        status="ok",
        reply=parsed.reply,
        suggestions=suggestions,
        decisions=decisions,
        dropped=dropped,
        model=response.model,
        elapsed_s=response.elapsed_s,
        raw_text_len=len(response.text or ""),
        finish_reason=response.finish_reason,
        finished_at=_now(),
        **base,
    )

"""P0-1：把"已核实的文献条目"变成一份可追溯的计划草稿。

与 ``agent/extract.py`` 同一个套路，每一段都能单独验证：

1. **没有条目就不调用模型**——没有证据的计划只能是空想，直接拒；
2. 调模型做结构化规划（提示词里明确 JSON、只能引用给定编号、不许写实测结果）；
3. **本地补出处**：模型只允许给 ``claim_id``；逐字引文、页码、文献标题全部
   由本地从 ``PaperClaim`` 取。编号对不上就丢弃并记录原因。
4. **没有出处的步骤标成"AI 推断"**，有出处的标"文献证据"——两者绝不混。
5. 扫一遍自由文本，出现"实测/结果表明/达到 X"这类像已完成实验的措辞就记提醒。
6. 草稿落 ``plan_drafts`` 表：刷新与重跑都还在，但**确认之前不算结论**。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..storage import repository as repo
from ..storage.models import Certainty, Paper, PaperClaim, PlanDraft, PlanDraftStatus, PlanMode
from . import plan_prompts
from . import plan_schemas
from .client import LLMClient
from .providers import LLMBadResponseError, LLMError
from .schemas import KIND_LABELS

log = logging.getLogger(__name__)

_RESULT_RE = re.compile("|".join(plan_schemas.RESULT_CLAIM_PATTERNS))


@dataclass
class DraftOutcome:
    """一次草稿生成的终态。**不允许"生成失败"被当成"生成了空计划"**。"""

    status: str                       # ok | refused | failed
    reason: str = ""
    draft: PlanDraft | None = None
    dropped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    raw_text_len: int = 0
    finish_reason: str = ""
    claim_count: int = 0
    evidence_count: int = 0
    ai_step_count: int = 0
    timeout_s: float = 0.0
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def verdict(self) -> str:
        if self.status == "refused":
            return "refused"
        if self.status == "failed":
            return "failed"
        if self.draft is None or not self.draft.steps:
            return "empty"
        if self.evidence_count == 0:
            return "no_evidence"      # 全是 AI 推断，没有一条挂得上出处
        if self.ai_step_count:
            return "partial"          # 有证据，也有推断
        return "success"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "elapsed_s": self.elapsed_s,
            "timeout_s": self.timeout_s,
            "raw_text_len": self.raw_text_len,
            "finish_reason": self.finish_reason,
            "claim_count": self.claim_count,
            "evidence_count": self.evidence_count,
            "ai_step_count": self.ai_step_count,
            "draft_id": self.draft.id if self.draft else "",
            "warnings": self.warnings,
            "dropped": self.dropped[:30],
            "finished_at": self.finished_at,
        }


def _certainty_label(certainty: Certainty) -> str:
    return {
        Certainty.STATED: "原文事实（逐字命中）",
        Certainty.IMPLIED: "疑似转述（待人工核查）",
        Certainty.INFERRED: "AI 推断（未核实）",
    }.get(certainty, "未核实")


def usable_claims(claims: list[PaperClaim]) -> list[PaperClaim]:
    """能用于规划生成的条目：必须带逐字引文，否则等于没有出处。"""
    return [claim for claim in claims if (claim.quote or "").strip()]


def plan_readiness(claims: list[PaperClaim]) -> tuple[bool, str]:
    """能不能生成计划。返回 (可以, 原因)。"""
    if not claims:
        return False, "还没有勾选文献条目"
    if not usable_claims(claims):
        return False, "勾选的条目里没有可用的逐字引文，无法作为计划依据"
    return True, ""


def _scan_results(text: str, where: str) -> list[str]:
    """扫出看起来像"已经做完实验"的措辞。计划里只允许建议参数与预期指标。"""
    found = _RESULT_RE.search(text or "")
    if not found:
        return []
    return [
        f"{where}出现「{found.group(0)}」这类已完成实验的说法："
        "计划里只能写建议参数与预期指标，这一句需要人工确认或改成预期表述"
    ]


def _claim_block(claim: PaperClaim, title: str) -> str:
    page = f"第 {claim.page} 页" if claim.page else (claim.locator or "出处未定位")
    return (
        f"{claim.claim}"
        f"（类型：{KIND_LABELS.get(claim.kind, claim.kind or '未分类')}；"
        f"引文：{(claim.quote or '')[:120]}；{page}；"
        f"{_certainty_label(claim.certainty)}）"
    )


def _evidence_of(claim: PaperClaim, title: str) -> plan_schemas.DraftEvidence:
    """从本地条目构造出处。引文与页码一律取库里那条，模型给的一律不采信。"""
    return plan_schemas.DraftEvidence(
        claim_id=claim.id,
        paper_id=claim.paper_id,
        paper_title=title,
        kind=claim.kind,
        kind_label=KIND_LABELS.get(claim.kind, claim.kind or "未分类"),
        claim=claim.claim,
        quote=claim.quote,
        page=claim.page,
        certainty=claim.certainty.value,
        certainty_label=_certainty_label(claim.certainty),
    )


def build_draft(
    *,
    project_id: str,
    mode: PlanMode,
    payload: plan_schemas.PlanDraftPayload,
    claims_by_id: dict[str, PaperClaim],
    titles: dict[str, str],
    provider: str,
    model: str,
    user_notes: str = "",
    persist: bool = True,
    into: PlanDraft | None = None,
) -> tuple[PlanDraft, list[dict], list[str]]:
    """把模型输出整理成草稿。返回 (草稿, 丢弃的引用, 提醒)。

    **这是草稿入库前的唯一闸门**：编号对不上就丢弃；出处一律本地补；
    没有出处的步骤标成 AI 推断。
    """
    dropped: list[dict] = []
    warnings: list[str] = []
    steps: list[dict] = []
    used_claims: dict[str, plan_schemas.DraftEvidence] = {}

    for index, raw_step in enumerate(payload.steps, start=1):
        valid_evidence: list[plan_schemas.DraftEvidence] = []
        bad_ids: list[str] = []
        for claim_id in raw_step.claim_ids:
            key = str(claim_id).strip()
            claim = claims_by_id.get(key)
            if claim is None:
                bad_ids.append(key)
                dropped.append({
                    "claim_id": key,
                    "step": raw_step.title or f"第 {index} 步",
                    "reason": "编号对不上任何已勾选的已核实条目",
                })
                continue
            if claim.id not in used_claims:
                used_claims[claim.id] = _evidence_of(claim, titles.get(claim.paper_id, ""))
            valid_evidence.append(used_claims[claim.id])

        text_blob = " ".join(
            part for part in (
                raw_step.title, raw_step.purpose, raw_step.method,
                raw_step.expected_metric, raw_step.risk,
            ) if part
        )
        warnings.extend(
            _scan_results(text_blob, f"第 {index} 步「{raw_step.title or '未命名'}」")
        )

        params = [
            plan_schemas.DraftParam(
                name=item.name, suggested_value=item.suggested_value,
                unit=item.unit, reason=item.reason, origin="suggested",
            )
            for item in raw_step.suggested_params
        ]
        steps.append(plan_schemas.DraftStep(
            order=raw_step.order or index,
            title=raw_step.title,
            purpose=raw_step.purpose,
            method=raw_step.method,
            origin="evidence" if valid_evidence else "ai",
            evidence=valid_evidence,
            params=params,
            expected_metric=raw_step.expected_metric,
            risk=raw_step.risk,
            dropped_claim_ids=bad_ids,
        ).model_dump())

    for field_name, value in (
        ("研究问题", payload.question),
        ("研究目标", payload.objective),
        ("数据需求", payload.data_requirements),
        ("成功判据", payload.success_criteria),
    ):
        warnings.extend(_scan_results(str(value or ""), field_name))
    for item in payload.expected_outputs:
        warnings.extend(_scan_results(str(item), "预期产出"))

    global_params = [
        plan_schemas.DraftParam(
            name=item.name, suggested_value=item.suggested_value,
            unit=item.unit, reason=item.reason, origin="suggested",
        ).model_dump()
        for item in payload.parameters
    ]

    now = datetime.now().isoformat(timespec="seconds")
    if into is not None:
        # 生成到已有草稿（"另起新方案"先建了系列）时保留它自己的 id 与系列信息，
        # 只替换内容——否则新主题会被写进一个没有系列的空草稿里。
        draft = into
        draft.mode = mode
        draft.status = PlanDraftStatus.DRAFT
        draft.origin = "llm"
    else:
        draft = PlanDraft(
            id=repo.new_id("draft"),
            project_id=project_id,
            mode=mode,
            status=PlanDraftStatus.DRAFT,
            created_at=now,
        )
    draft.question = payload.question
    draft.objective = payload.objective or draft.objective
    draft.data_requirements = payload.data_requirements
    draft.steps = steps
    draft.parameters = global_params
    draft.metrics = list(payload.metrics)
    draft.success_criteria = payload.success_criteria
    draft.expected_outputs = list(payload.expected_outputs)
    draft.risks = list(payload.risks)
    draft.evidence = [item.model_dump() for item in used_claims.values()]
    if user_notes:
        draft.user_notes = user_notes
    draft.warnings = warnings
    draft.dropped = dropped
    draft.claim_count = len(claims_by_id)
    draft.evidence_count = sum(1 for step in steps if step["origin"] == "evidence")
    draft.ai_step_count = sum(1 for step in steps if step["origin"] == "ai")
    draft.provider = provider
    draft.model = model
    draft.generated_at = now
    draft.updated_at = now
    draft.revision += 1
    if persist:
        repo.save(draft)
    return draft, dropped, warnings


def generate_draft(
    client: LLMClient,
    project,
    mode: PlanMode,
    claims: list[PaperClaim],
    *,
    papers: dict[str, Paper] | None = None,
    user_notes: str = "",
    persist: bool = True,
    max_tokens: int = 4000,
    into: PlanDraft | None = None,
) -> DraftOutcome:
    """生成一份草稿。被拒或失败都如实返回，不把异常抛给界面。"""
    credentials = client.store.current()
    base = {
        "provider": getattr(credentials, "provider", "") if credentials else "",
        "timeout_s": float(getattr(credentials.config(), "timeout", 0.0)) if credentials else 0.0,
    }
    claims = usable_claims(claims)
    ready, reason = plan_readiness(claims)
    if not ready:
        return _stamp(DraftOutcome(status="refused", reason=reason, **base))

    titles: dict[str, str] = {}
    for claim in claims:
        if claim.paper_id in titles:
            continue
        paper = (papers or {}).get(claim.paper_id) or repo.get(Paper, claim.paper_id)
        titles[claim.paper_id] = getattr(paper, "title", "") if paper else ""

    blocks = [
        (claim.id, titles.get(claim.paper_id, ""), _claim_block(claim, titles.get(claim.paper_id, "")))
        for claim in claims
    ]
    user = plan_prompts.plan_user(blocks, plan_schemas.mode_label(mode), user_notes)

    try:
        payload, response = client.complete_json(
            plan_prompts.PLAN_SYSTEM, user,
            json_schema=plan_schemas.PLAN_JSON_SCHEMA,
            max_tokens=max_tokens,
        )
    except LLMBadResponseError as exc:
        return _stamp(DraftOutcome(
            status="failed", reason=f"模型返回的内容不是合法 JSON：{exc}",
            claim_count=len(claims), **base,
        ))
    except LLMError as exc:
        return _stamp(DraftOutcome(
            status="failed", reason=str(exc), claim_count=len(claims), **base,
        ))

    facts = {
        **base,
        "model": response.model,
        "elapsed_s": response.elapsed_s,
        "raw_text_len": len(response.text or ""),
        "finish_reason": response.finish_reason,
    }

    try:
        parsed = plan_schemas.PlanDraftPayload.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 结构不符也要变成可见终态
        return _stamp(DraftOutcome(
            status="failed",
            reason=f"模型返回的 JSON 结构不符合约定：{type(exc).__name__}: {str(exc)[:200]}",
            claim_count=len(claims), **facts,
        ))

    claims_by_id = {claim.id: claim for claim in claims}
    draft, dropped, warnings = build_draft(
        project_id=project.id, mode=mode, payload=parsed,
        claims_by_id=claims_by_id, titles=titles,
        provider=base["provider"], model=response.model,
        user_notes=user_notes, persist=persist, into=into,
    )
    return _stamp(DraftOutcome(
        status="ok", draft=draft, dropped=dropped, warnings=warnings,
        claim_count=len(claims), evidence_count=draft.evidence_count,
        ai_step_count=draft.ai_step_count, **facts,
    ))


def _stamp(outcome: DraftOutcome) -> DraftOutcome:
    outcome.finished_at = datetime.now().isoformat(timespec="seconds")
    return outcome

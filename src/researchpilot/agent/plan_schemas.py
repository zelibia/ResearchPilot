"""计划草稿的结构化模型与 JSON Schema（P0-1）。

两条纪律写在类型里，而不是靠提示词自觉：

1. **模型只能引用编号**。草稿里的每条文献证据都是 ``claim_id``，逐字引文与
   页码由本地从 ``PaperClaim`` 取——模型改不动引文，也就没法把出处"润色"成
   它想要的样子（与 ``agent/evidence.py`` 同一条底线）。
2. **没有"实测结果"字段**。参数只叫 ``suggested_params``（建议参数），
   指标只叫 ``expected_metric``（预期指标）。计划里根本没有地方放实验数字，
   模型想编也没有字段可填。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 一条计划内容的来源：能追到 claim 的文献证据 / 模型推断 / 用户设定。
Origin = Literal["evidence", "ai", "user"]

ORIGIN_LABELS: dict[str, str] = {
    "evidence": "文献证据",
    "ai": "AI 推断",
    "user": "用户设定",
}

# 参数只有两种来源：模型给的建议值（未执行）、用户自己设定的值。
PARAM_ORIGIN_LABELS: dict[str, str] = {
    "suggested": "建议参数（未执行）",
    "user": "用户设定",
}

MODE_LABELS: dict[str, str] = {
    "reproduce": "A 原文复现",
    "improve": "B 文献改进对比",
    "own": "C 自主研究设想",
}

CONFIRMED_LABEL = "已确认"
DRAFT_LABEL = "草稿"


class LLMParam(BaseModel):
    """模型给出的建议参数。值本身只是建议，执行与否由用户决定。"""

    name: str = ""
    suggested_value: str = ""
    unit: str = ""
    reason: str = ""


class LLMStep(BaseModel):
    order: int | None = None
    title: str = ""
    purpose: str = ""
    method: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    suggested_params: list[LLMParam] = Field(default_factory=list)
    expected_metric: str = ""
    risk: str = ""


class PlanDraftPayload(BaseModel):
    """模型输出的计划结构。字段刻意保持少而硬。"""

    question: str = ""
    objective: str = ""
    data_requirements: str = ""
    steps: list[LLMStep] = Field(default_factory=list)
    parameters: list[LLMParam] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    success_criteria: str = ""
    expected_outputs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class DraftEvidence(BaseModel):
    """草稿里挂着的一条出处。引文与页码来自本地库，不来自模型。"""

    claim_id: str
    paper_id: str = ""
    paper_title: str = ""
    kind: str = ""
    kind_label: str = ""
    claim: str = ""
    quote: str = ""
    page: int | None = None
    certainty: str = ""
    certainty_label: str = ""


class DraftParam(BaseModel):
    name: str = ""
    suggested_value: str = ""
    unit: str = ""
    reason: str = ""
    origin: str = "suggested"


class DraftStep(BaseModel):
    order: int = 0
    title: str = ""
    purpose: str = ""
    method: str = ""
    origin: str = "ai"                 # evidence | ai | user
    evidence: list[DraftEvidence] = Field(default_factory=list)
    params: list[DraftParam] = Field(default_factory=list)
    expected_metric: str = ""
    risk: str = ""
    edited_by_user: bool = False
    # 模型引用了但本地对不上的编号；丢弃，但留下原因
    dropped_claim_ids: list[str] = Field(default_factory=list)


def origin_label(origin: str) -> str:
    return ORIGIN_LABELS.get(origin, "AI 推断")


def param_origin_label(origin: str) -> str:
    return PARAM_ORIGIN_LABELS.get(origin, "建议参数（未执行）")


def mode_label(mode: Any) -> str:
    """接受 PlanMode 或字符串。"""
    value = getattr(mode, "value", mode)
    return MODE_LABELS.get(str(value), str(value))


# 给模型看的 JSON Schema（能力强的服务商用它是硬约束，弱一点的至少能看懂结构）
PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "objective": {"type": "string"},
        "data_requirements": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "purpose": {"type": "string"},
                    "method": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                    "suggested_params": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "suggested_value": {"type": "string"},
                                "unit": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                    },
                    "expected_metric": {"type": "string"},
                    "risk": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "suggested_value": {"type": "string"},
                    "unit": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "metrics": {"type": "array", "items": {"type": "string"}},
        "success_criteria": {"type": "string"},
        "expected_outputs": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["objective", "steps"],
}


# 看起来像"已经做过的实测结果"的说法。计划里只能写建议参数与预期指标，
# 出现这些措辞就记一条提醒，让用户知道这一句不是证据。
RESULT_CLAIM_PATTERNS: tuple[str, ...] = (
    r"实测",
    r"已测得",
    r"实际测得",
    r"已经跑出",
    r"已完成实验",
    r"实验(结果|表明|显示)",
    r"实测(结果|值)",
    r"达到[了]?\s*\d",
    r"提升[了]?\s*\d",
    r"降至\s*\d",
)

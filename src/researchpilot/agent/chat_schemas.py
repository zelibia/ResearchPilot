"""科研对话的结构化输出（P0-B）。

一个受限接口：模型一次返回「讨论回复 + 待确认的修改建议 + 决策条目」。
建议**不是修改**——它只是候选，落库时状态是 ``pending``，
必须用户点"应用到草稿"才生效（见 ``planning/discussion.apply_suggestion``）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 允许 AI 建议修改的字段：只开放这些，别的字段一律拒绝。
SUGGESTION_FIELDS: dict[str, tuple[str, ...]] = {
    "draft": (
        "question",
        "objective",
        "data_requirements",
        "success_criteria",
        "user_notes",
    ),
    "step": (
        "title",
        "purpose",
        "method",
        "expected_metric",
        "risk",
    ),
}

FIELD_LABELS: dict[str, str] = {
    "question": "研究问题",
    "objective": "研究目标",
    "data_requirements": "数据需求",
    "success_criteria": "成功判据",
    "user_notes": "用户设定",
    "title": "步骤标题",
    "purpose": "要解决的问题",
    "method": "做法",
    "expected_metric": "预期指标",
    "risk": "风险",
}

# 助手消息与前端的固定标记：对话内容不是已验证的科研事实。
ASSISTANT_LABEL = "AI 讨论，未核实"

# AI 只能提出这两类决策条目；"用户拍板"由用户在界面上产生。
DECISION_KINDS = ("ai_suggestion", "ai_objection")

DISCUSSION_ROLES: dict[str, str] = {
    "discuss": "科研讨论",
    "workflow": "工作流工具",
}


class ChatSuggestion(BaseModel):
    target: Literal["draft", "step"] = "draft"
    field: str = ""
    step_order: int | None = None
    after: str = ""
    why: str = ""
    claim_ids: list[str] = Field(default_factory=list)


class ChatDecision(BaseModel):
    kind: str = "ai_suggestion"
    text: str = ""
    claim_ids: list[str] = Field(default_factory=list)


class ChatPayload(BaseModel):
    reply: str = ""
    suggestions: list[ChatSuggestion] = Field(default_factory=list)
    decisions: list[ChatDecision] = Field(default_factory=list)


CHAT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"enum": ["draft", "step"]},
                    "field": {"type": "string"},
                    "step_order": {"type": ["integer", "null"]},
                    "after": {"type": "string"},
                    "why": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["target", "field", "after"],
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"enum": list(DECISION_KINDS)},
                    "text": {"type": "string"},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "text"],
            },
        },
    },
    "required": ["reply"],
}


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)

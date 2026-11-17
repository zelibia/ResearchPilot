"""结构化输出的数据模型与 JSON Schema。

字段刻意保持少而硬：每条结论必须有 `quote`（逐字引文）与 `paper_ref`（文献编号）。
没有引文的条目在本地校验阶段就会被丢掉。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ClaimKind = Literal["problem", "method", "assumption", "limitation", "conclusion"]

KIND_LABELS: dict[str, str] = {
    "problem": "研究问题",
    "method": "核心方法",
    "assumption": "假设",
    "limitation": "局限",
    "conclusion": "关键结论",
}


class ExtractedItem(BaseModel):
    kind: ClaimKind
    text: str = ""
    quote: str = ""
    paper_ref: str = ""
    page_hint: int | None = None


class ExtractionPayload(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)


class PathStep(BaseModel):
    title: str = ""
    why: str = ""
    paper_refs: list[str] = Field(default_factory=list)
    quote: str = ""
    quote_paper_ref: str = ""


class LearningPathPayload(BaseModel):
    steps: list[PathStep] = Field(default_factory=list)


# 给模型看的 JSON Schema（能力强的服务商会用它做结构约束）
EXTRACTION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"enum": list(KIND_LABELS)},
                    "text": {
                        "type": "string",
                        "description": (
                            "短篇略读概括，必须改写而非摘抄；不得包含引号、页码或原文整句"
                        ),
                    },
                    "quote": {"type": "string"},
                    "paper_ref": {"type": "string"},
                    "page_hint": {"type": ["integer", "null"]},
                },
                "required": ["kind", "text", "quote", "paper_ref"],
            },
        }
    },
    "required": ["items"],
}


LEARNING_PATH_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "paper_refs": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                    "quote_paper_ref": {"type": "string"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["steps"],
}

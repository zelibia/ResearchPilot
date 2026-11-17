"""AI 解读三种模式的结构化模型与 JSON Schema。

设计原则（对应"解析内容必须可信"这条要求）：

1. quick、audit、deep 使用三套互斥 JSON Schema；
2. 所有正式 evidence 的 quote 必须能在原文中逐字定位；
3. 深读固定七维，不再要求模型生成文献类型或目录字段；
4. ``GuideSection`` 仅保留用于读取旧版导览。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 相关文献的分类：按"为什么值得读"分，而不是按模型自由发挥。
RELATED_CATEGORIES: tuple[str, ...] = ("理论基础", "方法来源", "应用案例", "改进与争议")

# 文献解读固定七个维度。前六项由模型基于核心原文输出，最后一项由本地目录生成。
INSIGHT_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("background", "研究背景"),
    ("method", "核心方法"),
    ("assumptions", "理论假设"),
    ("data", "实验数据"),
    ("findings", "结果发现"),
    ("limitations", "局限争议"),
    ("outlook", "研究展望"),
)
INSIGHT_DIMENSION_KEYS: tuple[str, ...] = tuple(key for key, _ in INSIGHT_DIMENSIONS)
MODEL_DIMENSION_KEYS: tuple[str, ...] = INSIGHT_DIMENSION_KEYS
INSIGHT_DIMENSION_LABELS: dict[str, str] = dict(INSIGHT_DIMENSIONS)

LEGACY_INSIGHT_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("background", "研究背景与目的"),
    ("method", "核心方法与技术"),
    ("type_domain", "文献类型与领域"),
    ("structure", "论文结构与目录"),
    ("content", "核心内容概要"),
    ("findings", "主要结论与发现"),
    ("limitations", "研究局限与展望"),
)

# 讲解块的中文标签（界面直接用这些标题分段）
BLOCK_LABELS: dict[str, str] = {
    "points": "原文依据",
    "summary": "这一节解决什么问题",
    "steps": "方法步骤",
    "params": "参数与口径",
    "assumptions": "关键假设",
    "scope": "适用条件",
    "limits": "局限与偏差",
    "reproducibility": "复现所需",
    "relation": "与前文的衔接",
}


class GuidePoint(BaseModel):
    """一条**原文事实**：text 是概括，quote 是逐字引文（本地校验的对象）。"""

    text: str
    quote: str = ""
    page_hint: int | None = None


class GuideBackground(BaseModel):
    """背景补充：论文里没有、但对理解有帮助的知识。source 必填。"""

    text: str
    source: str = ""


class InsightDimension(BaseModel):
    """七维解读中的一个维度；evidence 必须能在原文里精确定位。"""

    summary: str = ""
    bullets: list[str] = Field(default_factory=list)
    evidence: list[GuidePoint] = Field(default_factory=list)


class InsightDimensions(BaseModel):
    """标准详细版的固定七维。"""

    background: InsightDimension
    method: InsightDimension
    assumptions: InsightDimension
    data: InsightDimension
    findings: InsightDimension
    limitations: InsightDimension
    outlook: InsightDimension


class QuickGuide(BaseModel):
    """精简摘要版：单段判断，不承载步骤和参数。"""

    text: str


class MethodParameter(BaseModel):
    """方法重点版的参数行，可映射到复现方案的建议参数。"""

    name: str
    value: str
    unit: str = ""
    note: str = ""


class MethodStep(BaseModel):
    """方法重点版的一个可导出步骤。"""

    order: int = Field(ge=1)
    title: str
    purpose: str = ""
    input: str = ""
    method: str
    output: str = ""
    params: list[MethodParameter] = Field(default_factory=list)
    metric: str = ""
    risk: str = ""
    evidence: list[GuidePoint] = Field(min_length=1, max_length=2)


class AuditGuide(BaseModel):
    """方法重点版：目标、逐步流程、参数表和适用边界。"""

    core_goal: str
    steps: list[MethodStep] = Field(min_length=1, max_length=10)
    boundaries: list[str] = Field(default_factory=list)


class GuideSection(BaseModel):
    title: str
    level: int = 2
    summary: str = ""
    points: list[GuidePoint] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    params: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    scope: str = ""
    limits: str = ""
    reproducibility: list[str] = Field(default_factory=list)
    relation: str = ""
    background: list[GuideBackground] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class RelatedWork(BaseModel):
    """一条推荐文献。``doi`` 是本地核验的入口——没有 DOI 就用标题核对，
    核对不上的界面标成「未能核实」，且不允许加入文献库。"""

    category: str = "理论基础"
    title: str
    authors: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    why: str = ""


class GuideChunkPayload(BaseModel):
    """一次请求的返回：只覆盖本次送进去的那些页。"""

    sections: list[GuideSection] = Field(default_factory=list)
    related: list[RelatedWork] = Field(default_factory=list)


class PaperInsightPayload(BaseModel):
    """标准详细版响应。"""

    mode: Literal["deep"] = "deep"
    dimensions: InsightDimensions
    related: list[RelatedWork] = Field(default_factory=list)


class QuickGuidePayload(BaseModel):
    mode: Literal["quick"] = "quick"
    quick: QuickGuide
    related: list[RelatedWork] = Field(default_factory=list)


class AuditGuidePayload(BaseModel):
    mode: Literal["audit"] = "audit"
    audit: AuditGuide
    related: list[RelatedWork] = Field(default_factory=list)


MODE_PAYLOAD_MODELS = {
    "quick": QuickGuidePayload,
    "audit": AuditGuidePayload,
    "deep": PaperInsightPayload,
}

_DIMENSION_ALIASES: dict[str, str] = {
    "background": "background",
    "研究背景": "background",
    "研究背景与目的": "background",
    "method": "method",
    "核心方法": "method",
    "核心方法与技术": "method",
    "assumptions": "assumptions",
    "assumption": "assumptions",
    "理论假设": "assumptions",
    "关键假设": "assumptions",
    "data": "data",
    "实验数据": "data",
    "content": "data",
    "核心内容概要": "data",
    "findings": "findings",
    "results": "findings",
    "结果发现": "findings",
    "主要结论与发现": "findings",
    "limitations": "limitations",
    "局限争议": "limitations",
    "研究局限与展望": "limitations",
    "outlook": "outlook",
    "研究展望": "outlook",
}


def _canonical_dimension_key(value: str) -> str:
    text = str(value or "").strip()
    if text in _DIMENSION_ALIASES:
        return _DIMENSION_ALIASES[text]
    lowered = text.lower()
    for alias, key in _DIMENSION_ALIASES.items():
        if alias.lower() == lowered:
            return key
    return text


def _normalize_dimension(value: dict) -> dict:
    item = dict(value or {})
    bullets = item.get("bullets")
    if not isinstance(bullets, list):
        bullets = item.get("points") if isinstance(item.get("points"), list) else []
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        evidence = item.get("quotes") if isinstance(item.get("quotes"), list) else []
    return {
        "summary": str(item.get("summary") or item.get("content") or "").strip(),
        "bullets": [
            str(point).strip() for point in bullets if str(point).strip()
        ],
        "evidence": [point for point in evidence if isinstance(point, dict)],
    }


def normalize_mode_payload(payload: dict, mode: str) -> dict:
    """兼容不支持 JSON Schema 的模型返回，统一到当前模式的字段名。

    只做结构别名归一化，不补造缺失内容。是否合格仍由后面的证据与长度校验决定。
    """
    data = dict(payload or {})
    if isinstance(data.get("result"), dict):
        data = dict(data["result"])

    if mode == "quick":
        quick = data.get("quick")
        if not isinstance(quick, dict):
            text = data.get("text") or data.get("summary") or ""
            quick = {"text": text} if text else {}
        return {
            "mode": "quick",
            "quick": quick,
            "related": data.get("related") or [],
        }

    if mode == "audit":
        audit = data.get("audit")
        if not isinstance(audit, dict):
            if any(key in data for key in ("core_goal", "steps", "boundaries")):
                audit = {
                    "core_goal": data.get("core_goal") or "",
                    "steps": data.get("steps") or [],
                    "boundaries": data.get("boundaries") or [],
                }
            else:
                audit = {}
        return {
            "mode": "audit",
            "audit": audit,
            "related": data.get("related") or [],
        }

    container = data.get("deep")
    if isinstance(container, dict):
        data = dict(container)
    dimensions = data.get("dimensions")
    if isinstance(dimensions, list):
        converted: dict = {}
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            key = _canonical_dimension_key(
                str(item.get("key") or item.get("name") or item.get("title") or "")
            )
            if key:
                converted[key] = _normalize_dimension(item)
        dimensions = converted
    elif not isinstance(dimensions, dict):
        candidates = {
            key: value for key, value in data.items()
            if _canonical_dimension_key(key) in {
                "background", "method", "assumptions", "data",
                "findings", "limitations", "outlook",
            }
        }
        dimensions = candidates if candidates else {}
    else:
        dimensions = {
            _canonical_dimension_key(key): _normalize_dimension(value)
            for key, value in dimensions.items()
            if isinstance(value, dict)
        }
    return {
        "mode": "deep",
        "dimensions": dimensions,
        "related": data.get("related") or data.get("references") or [],
    }


def _insight_dimension_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "quote": {"type": "string"},
                        "page_hint": {"type": ["integer", "null"]},
                    },
                    "required": ["text", "quote"],
                },
            },
        },
        "required": ["summary", "evidence"],
    }


DEEP_GUIDE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "mode": {"const": "deep"},
        "dimensions": {
            "type": "object",
            "properties": {
                key: _insight_dimension_schema() for key in MODEL_DIMENSION_KEYS
            },
            "required": list(MODEL_DIMENSION_KEYS),
        },
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "level": {"type": "integer"},
                    "summary": {"type": "string"},
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "quote": {"type": "string"},
                                "page_hint": {"type": ["integer", "null"]},
                            },
                            "required": ["text", "quote"],
                        },
                    },
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "params": {"type": "array", "items": {"type": "string"}},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": "string"},
                    "limits": {"type": "string"},
                    "reproducibility": {"type": "array", "items": {"type": "string"}},
                    "relation": {"type": "string"},
                    "background": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "source": {"type": "string"},
                            },
                            "required": ["text", "source"],
                        },
                    },
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
        "related": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"enum": list(RELATED_CATEGORIES)},
                    "title": {"type": "string"},
                    "authors": {"type": "string"},
                    "year": {"type": "string"},
                    "venue": {"type": "string"},
                    "doi": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["category", "title", "why"],
            },
        },
    },
    "required": ["mode", "dimensions"],
}

QUICK_GUIDE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "mode": {"const": "quick"},
        "quick": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
        "related": DEEP_GUIDE_JSON_SCHEMA["properties"]["related"],
    },
    "required": ["mode", "quick"],
}

AUDIT_GUIDE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "mode": {"const": "audit"},
        "audit": {
            "type": "object",
            "properties": {
                "core_goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "order": {"type": "integer", "minimum": 1},
                            "title": {"type": "string"},
                            "purpose": {"type": "string"},
                            "input": {"type": "string"},
                            "method": {"type": "string"},
                            "output": {"type": "string"},
                            "params": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "value": {"type": "string"},
                                        "unit": {"type": "string"},
                                        "note": {"type": "string"},
                                    },
                                    "required": ["name", "value"],
                                },
                            },
                            "metric": {"type": "string"},
                            "risk": {"type": "string"},
                            "evidence": _insight_dimension_schema()["properties"]["evidence"],
                        },
                        "required": [
                            "order", "title", "method", "params", "evidence",
                        ],
                    },
                },
                "boundaries": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["core_goal", "steps", "boundaries"],
        },
        "related": DEEP_GUIDE_JSON_SCHEMA["properties"]["related"],
    },
    "required": ["mode", "audit"],
}

MODE_JSON_SCHEMAS = {
    "quick": QUICK_GUIDE_JSON_SCHEMA,
    "audit": AUDIT_GUIDE_JSON_SCHEMA,
    "deep": DEEP_GUIDE_JSON_SCHEMA,
}

# 兼容旧导入路径；深读模式是原有默认输出。
GUIDE_JSON_SCHEMA = DEEP_GUIDE_JSON_SCHEMA

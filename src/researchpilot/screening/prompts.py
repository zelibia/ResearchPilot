"""Prompts and JSON schema for one-call screening evaluation."""

from __future__ import annotations

from .contracts import ScreeningCandidateInput

PROMPT_VERSION = "candidate-screening-v1"

SYSTEM_PROMPT = """你是科研项目的文献初筛助手。

只根据用户给出的项目背景、候选文献元数据和正文节选判断。
不得使用外部知识，不得臆测论文未提供的内容。

请完成四项独立评估：
1. topic_match：候选文献与本项目研究对象、方法链和目标的匹配程度；
2. academic_value：方法、数据、验证、结论的具体性与可信度；
3. citation_potential：项目规划或成果中引用该文的可能性；
4. reading_priority：项目当前阶段是否值得立即精读。

评分规则：
- 每项为 0–100 整数；
- 80 以上必须已有关键证据；
- 输入只有元数据时，学术价值和引用潜力分数必须保守；
- 期刊名、作者名和引用量不能替代正文证据；
- 不能因为论文新就判为更好，也不能因为标题相似就判为高相关。

理由规则：
- 输出 3–6 条核心理由；
- 每条理由必须注明来源 metadata / abstract / fulltext；
- fulltext 或 abstract 理由应尽量给出逐字 quote 与 0-based pageIndex；
- 找不到原文依据时不要写引文，不得伪造。

置信度表示你对本次判断的把握，不是论文质量为真的概率。
只输出 JSON，不使用 Markdown。"""


def build_user_prompt(candidate: ScreeningCandidateInput) -> str:
    project = candidate.project_context
    metadata = candidate.metadata
    lines = [
        "# 项目",
        f"标题：{project.title}",
        f"领域：{project.domain}",
        f"描述：{project.description}",
        f"当前目标：{project.active_goal}",
        f"成功标准：{'；'.join(project.success_criteria)}",
        f"已有方法：{'；'.join(project.target_method_names)}",
        f"已有文献：{'；'.join(project.existing_document_titles[:60])}",
        "",
        "# 候选元数据",
        f"标题：{metadata.title}",
        f"作者：{'; '.join(metadata.authors)}",
        f"年份：{metadata.year}",
        f"期刊：{metadata.venue}",
        f"DOI：{metadata.doi}",
        f"摘要：{candidate.abstract}",
        "",
        "# 正文节选",
    ]
    for section in candidate.fulltext_sections:
        lines.extend(
            [
                f"[第 {section.page_start + 1}-{section.page_end + 1} 页 {section.title}]",
                section.text,
                "",
            ]
        )
    return "\n".join(lines)[:24000]


CORRECTION_PROMPT = """上一次输出无法通过 JSON/Schema 校验。
请严格只返回符合 screening.v1 的 JSON 对象：
- schemaVersion 固定为 "screening.v1"；
- 四个 score 必须是 0–100 整数；
- reasons 为 1–6 条，每条含 dimension/polarity/text/sourceKind，可选 pageIndex/quote；
- modelConfidence 为 0–1；
- 不输出 Markdown 或解释。
校验错误：{validation_error}"""


SCREENING_JSON_SCHEMA = {
    "type": "object",
    "required": [
        "schemaVersion",
        "conclusionSummary",
        "scores",
        "reasons",
        "modelConfidence",
        "confidenceRationale",
    ],
    "properties": {
        "schemaVersion": {"const": "screening.v1"},
        "conclusionSummary": {"type": "string"},
        "scores": {
            "type": "object",
            "required": [
                "topic_match",
                "academic_value",
                "citation_potential",
                "reading_priority",
            ],
            "properties": {
                key: {
                    "type": "object",
                    "required": ["score", "rationale"],
                    "properties": {
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "rationale": {"type": "string"},
                    },
                }
                for key in (
                    "topic_match",
                    "academic_value",
                    "citation_potential",
                    "reading_priority",
                )
            },
        },
        "reasons": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "required": ["dimension", "polarity", "text", "sourceKind"],
                "properties": {
                    "dimension": {
                        "enum": [
                            "topic_match",
                            "academic_value",
                            "citation_potential",
                            "reading_priority",
                        ]
                    },
                    "polarity": {"enum": ["positive", "negative", "risk", "neutral"]},
                    "text": {"type": "string"},
                    "sourceKind": {"enum": ["metadata", "abstract", "fulltext"]},
                    "pageIndex": {"type": ["integer", "null"], "minimum": 0},
                    "quote": {"type": "string"},
                },
            },
        },
        "modelConfidence": {"type": "number", "minimum": 0, "maximum": 1},
        "confidenceRationale": {"type": "string"},
    },
}

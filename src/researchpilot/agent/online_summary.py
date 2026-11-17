"""Abstract-level overview for a paper whose full text is not available."""

from __future__ import annotations

from datetime import datetime

from ..storage.models import Paper
from .client import LLMClient
from .providers import LLMError

ONLINE_ABSTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "background": {"type": "string"},
        "methods_and_data": {"type": "string"},
        "results": {"type": "string"},
        "limitations": {"type": "string"},
    },
    "required": [
        "background",
        "methods_and_data",
        "results",
        "limitations",
    ],
}

ONLINE_ABSTRACT_SYSTEM = """你是严谨的科研文献助手。
用户只提供论文的公开摘要与元数据，没有提供全文。

规则：
1. 只能依据摘要与元数据概括，不得补充外部知识或推测未出现的方法、数据、数字。
2. 固定输出四段：研究背景与问题、核心方法与数据、主要结果与结论、局限与展望。
3. 摘要没有交代的内容写“摘要未说明”，不得写“论文未提及”或自行补全。
4. 不使用逐字长引文，不标页码；不得声称已阅读全文。
5. 只输出合法 JSON：
{"background":"...","methods_and_data":"...","results":"...","limitations":"..."}
"""


def build_abstract_overview(
    client: LLMClient,
    paper: Paper,
    abstract: str,
    *,
    source_url: str = "",
    provider: str = "openalex",
) -> dict:
    """Ask the model for a clearly abstract-scoped four-section overview."""
    text = " ".join(str(abstract or "").split()).strip()
    if not text:
        raise LLMError("没有可供阅读的公开摘要")
    user = "\n".join([
        f"标题：{paper.title or '（未提供）'}",
        f"作者：{paper.authors or '（未提供）'}",
        f"年份：{paper.year or '（未提供）'}",
        f"期刊：{paper.venue or '（未提供）'}",
        f"DOI：{paper.doi or '（未提供）'}",
        f"来源：{source_url or '（未提供）'}",
        "",
        "公开摘要：",
        text,
        "",
        "请按给定 JSON 输出。",
    ])
    payload, response = client.complete_json(
        ONLINE_ABSTRACT_SYSTEM,
        user,
        json_schema=ONLINE_ABSTRACT_SCHEMA,
    )
    overview = {
        key: " ".join(str(payload.get(key) or "").split()).strip()
        or "摘要未说明"
        for key in ("background", "methods_and_data", "results", "limitations")
    }
    return {
        "status": "abstract",
        "provider": provider,
        "source_url": source_url,
        "abstract_chars": len(text),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": getattr(response, "model", "") or paper.summary.get(
            "online_source", {}
        ).get("model", ""),
        "overview": overview,
    }

"""AI 解读：按阅读目标生成三种互斥且有原文依据的输出。

生成入口优先提取摘要、引言、方法、结果/讨论和结论，统一清理编码与控制字符；
- quick：单段价值判断，不含步骤、参数或逐页角标；
- audit：可映射复现方案的步骤、输入输出、参数和边界；
- deep：固定七维，每个维度都必须带可逐字定位的原文引文。

空响应、缺维度、套话、缺引文或类型判定缺失时自动重试一次；模型服务
不支持 JSON Schema 时降级为提示词约束的普通 JSON。失败终态必须说明具体原因。

相关文献一律先本地核验（DOI → OpenAlex → Crossref），核验不上的会被标记成
「未能核实」，界面不允许把它加进文献库——宁可少推荐，也不许把编造的文献写进项目。

产物存进 `Paper.summary["reading_guide"]`，不新增数据库表，也不改任何既有字段。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from ..literature import crossref as crossref_mod
from ..literature import fulltext
from ..literature import openalex as openalex_mod
from ..literature.normalize import normalize_doi
from ..literature.text_quality import body_is_reliable
from ..storage import repository as repo
from ..storage.models import (
    Certainty,
    Paper,
    QueryKind,
    QueryStatus,
    SourceRecord,
    VerificationStatus,
)
from . import evidence as evidence_mod
from . import guide_prompts, guide_schemas
from .client import LLMClient, extract_json
from .providers import LLMBadResponseError, LLMError

log = logging.getLogger(__name__)

GUIDE_KEY = "reading_guide"
MAX_RELATED = 24            # 一次导览最多留多少条推荐文献
MAX_RELATED_LOOKUPS = 12    # 一次生成最多做多少次外部核验（联网有成本）
CORE_INPUT_CHARS = 30000    # 摘要/引言/方法/结果/结论的核心原文预算
AUTO_RETRY_LIMIT = 1

_GENERIC_PHRASES = (
    "该领域发展迅速", "具有重要的理论意义", "具有重要的现实意义",
    "方法先进", "研究价值很高", "值得深入研究", "意义重大",
)
_DIMENSION_TITLES = dict(guide_schemas.INSIGHT_DIMENSIONS)
_CORE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("摘要", ("abstract", "摘要")),
    ("引言", ("introduction", "background", "引言", "背景", "研究目的")),
    ("方法", ("method", "methodology", "data", "processing", "approach",
              "方法", "数据", "处理", "流程")),
    ("结果与讨论", ("result", "discussion", "结果", "讨论")),
    ("结论", ("conclusion", "conclusions", "summary", "结论", "总结", "展望")),
)


def _schema_mode_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "response_format" in message
        or "json_schema" in message
        or ("http 400" in message and "schema" in message)
    )


@dataclass
class GuideOutcome:
    paper_id: str
    status: str                       # ok | refused | failed
    reason: str = ""
    guide: dict = field(default_factory=dict)
    model: str = ""
    elapsed_s: float = 0.0
    requests: int = 0
    failed_chunks: int = 0
    dropped_points: int = 0           # 引文对不上原文、被丢掉的条目
    related_total: int = 0
    related_verified: int = 0
    page_count: int = 0
    pages_analyzed: int = 0
    attempts: int = 0
    raw_points: int = 0
    kept_points: int = 0
    input_sections: list[str] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    # 已经收到的思考内容（过程性内容）。即使这次生成失败也要交回给界面，
    # 不然用户白等一场、连线索都看不到。
    thinking_text: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "model": self.model,
            "elapsed_s": self.elapsed_s,
            "requests": self.requests,
            "failed_chunks": self.failed_chunks,
            "dropped_points": self.dropped_points,
            "related_total": self.related_total,
            "related_verified": self.related_verified,
            "page_count": self.page_count,
            "pages_analyzed": self.pages_analyzed,
            "attempts": self.attempts,
            "raw_points": self.raw_points,
            "kept_points": self.kept_points,
            "input_sections": self.input_sections[:12],
            "coverage_notes": self.coverage_notes[:20],
            "thinking_chars": len(self.thinking_text or ""),
            "finished_at": self.finished_at,
        }


@dataclass
class GuideEvent:
    """生成过程中的一个可观察事件（给界面用，不落库）。

    ``thinking`` / ``answering`` 是**增量文本**，界面按到达顺序追加；
    ``done`` / ``failed`` / ``refused`` 时 ``outcome`` 才是最终结果。
    思考内容只是过程性内容——它既不是回答，也不参与证据校验。
    """

    stage: str
    text: str = ""
    detail: str = ""
    outcome: "GuideOutcome | None" = None
    thinking_chars: int = 0
    answer_chars: int = 0


@dataclass(frozen=True)
class CoreExcerpt:
    text: str
    pages: tuple[int, ...]
    labels: tuple[str, ...]
    truncated: bool = False


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def load(paper: Paper) -> dict | None:
    """已保存的导览。没有模型连接时，它照样能读。"""
    value = (paper.summary or {}).get(GUIDE_KEY)
    return value if isinstance(value, dict) and value.get("sections") is not None else None


def store(paper: Paper, guide: dict) -> Paper:
    paper.summary = {**(paper.summary or {}), GUIDE_KEY: guide}
    return repo.save(paper)


def clean_model_text(value: str | None) -> str:
    """把从 PDF 正文读出的文本整理成可读、可送入模型的纯文本。

    只处理编码与排版噪声，不改写词语：统一 NFKC、去掉零宽字符、把控制字符、
    替换符和私用区字形换成空格，再压缩空白。原文文件本身永远不改。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    cleaned: list[str] = []
    for char in text:
        code = ord(char)
        category = unicodedata.category(char)
        if char in "\n\r\t" or not (
            code < 32 or code == 127 or category in {"Cc", "Cf", "Co"}
            or char == "\ufffd"
        ):
            cleaned.append(char)
        else:
            cleaned.append(" ")
    value = "".join(cleaned).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\u00a0]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def readiness(paper: Paper) -> tuple[bool, str]:
    """能不能给这篇文献生成导览（与抽取同一道正文可靠性闸门）。"""
    pages, reason = evidence_mod.load_pages_with_reason(paper)
    if not pages:
        return False, reason
    cleaned = "\n".join(clean_model_text(page) for page in pages)
    reliable, why = body_is_reliable(cleaned)
    if not reliable:
        return False, f"原文核心段落解析不完整：{why}"
    return True, ""


def _core_label(title: str) -> str | None:
    lowered = clean_model_text(title).lower()
    for label, needles in _CORE_GROUPS:
        if any(needle in lowered for needle in needles):
            return label
    return None


def core_excerpt(
    index: fulltext.PaperIndex,
    *,
    max_chars: int = CORE_INPUT_CHARS,
) -> CoreExcerpt:
    """优先取摘要、引言、方法、结果/讨论、结论，再补首页与末页兜底。

    这是给"文献解读"的输入，不是全文抽取：每段保留真实页码，并对控制字符做无损清洗。
    选取结果会在界面统计中如实展示，不能假装读过未选入的页面。
    """
    candidates: list[tuple[int, str, int, int, str]] = []
    for section in index.sections:
        label = _core_label(section.title)
        if label is None:
            continue
        body = "\n".join(
            clean_model_text(index.pages[page - 1])
            for page in range(section.start_page, section.end_page + 1)
            if 1 <= page <= index.page_count
        ).strip()
        if body:
            priority = next(
                position for position, (name, _) in enumerate(_CORE_GROUPS)
                if name == label
            )
            candidates.append((priority, label, section.start_page, section.end_page, body))

    # 论文没有可识别标题时，首页/中段/末页仍然是最小可信阅读面。
    if index.page_count:
        fallback_pages = list(dict.fromkeys([
            1,
            max(1, index.page_count // 2),
            index.page_count,
        ]))
        covered = {
            page for _priority, _label, start, end, _body in candidates
            for page in range(start, end + 1)
        }
        found_labels = {item[1] for item in candidates}
        needs_problem_context = not ({"摘要", "引言"} & found_labels)
        needs_method_or_result = not ({"方法", "结果与讨论", "结论"} & found_labels)
        if not covered or needs_problem_context or needs_method_or_result:
            for page in fallback_pages:
                if page in covered:
                    continue
                body = clean_model_text(index.pages[page - 1])
                if body:
                    candidates.append((9, "正文补充页", page, page, body))

    candidates.sort(key=lambda item: (item[0], item[2], item[3]))
    parts: list[str] = []
    labels: list[str] = []
    pages: list[int] = []
    used = 0
    seen_ranges: set[tuple[int, int]] = set()
    truncated = False
    for _priority, label, start, end, body in candidates:
        if (start, end) in seen_ranges:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        excerpt = body[:remaining].strip()
        if not excerpt:
            continue
        seen_ranges.add((start, end))
        header = f"### {label}｜第 {start}–{end} 页" if start != end else (
            f"### {label}｜第 {start} 页"
        )
        parts.append(f"{header}\n{excerpt}")
        labels.append(f"{label}（第 {start}–{end} 页）" if start != end else f"{label}（第 {start} 页）")
        pages.extend(range(start, end + 1))
        used += len(excerpt)
        truncated = truncated or len(excerpt) < len(body)

    return CoreExcerpt(
        text="\n\n".join(parts),
        pages=tuple(dict.fromkeys(pages)),
        labels=tuple(dict.fromkeys(labels)),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# 本地核验
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    return evidence_mod.normalize_for_match(value or "")


def match_section(index: fulltext.PaperIndex | None, title: str) -> dict | None:
    """把模型给的章节标题对回原文目录。对不上就不给页码（不猜）。"""
    if index is None or not title:
        return None
    target = _norm(title)
    if not target:
        return None
    best = None
    for section in index.sections:
        candidate = _norm(section.title)
        if not candidate:
            continue
        if candidate == target or target in candidate or candidate in target:
            # 越接近越优先（避免"1 Introduction"和"1.1 …"互相抢）
            score = abs(len(candidate) - len(target))
            if best is None or score < best[0]:
                best = (score, section)
    if best is None:
        return None
    section = best[1]
    return {
        "page_start": section.start_page,
        "page_end": section.end_page,
        "title_matched": True,
        "section_source": section.source,
    }


def verify_points(points: list[dict], pages: list[str]) -> tuple[list[dict], int]:
    """逐条核对引文。返回 (通过, 丢弃数)。

    规则与 C3-B 抽取一致：引文必须能在原文里定位；定位不到就不进
    "原文依据"，**不采信模型自报的页码**。
    """
    kept: list[dict] = []
    dropped = 0
    for item in points:
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not text and not quote:
            dropped += 1
            continue
        if not quote:
            dropped += 1
            continue
        match = evidence_mod.locate_quote(quote, pages)
        if not match.accepted:
            dropped += 1
            continue
        kept.append(
            {
                "text": text or quote[:120],
                "quote": quote,
                "page": match.page,
                # stated = 逐字命中（原文事实）；implied = 疑似转述，界面标"待人工核查"
                "certainty": match.certainty.value,
                "detail": match.detail,
            }
        )
    return kept, dropped


def _verify_exact_points(points: list[dict], pages: list[str]) -> tuple[list[dict], int]:
    """正式解读只接受逐字命中的引文，保证每条解释都有可直接跳转的原文。"""
    kept: list[dict] = []
    dropped = 0
    for item in points:
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not quote:
            dropped += 1
            continue
        match = evidence_mod.locate_quote(quote, pages)
        if match.certainty is not Certainty.STATED or match.page is None:
            dropped += 1
            continue
        kept.append({
            "text": text or quote[:120],
            "quote": quote,
            "page": match.page,
            "verification": "exact",
        })
    return kept, dropped


def _dimension_text(dimension: guide_schemas.InsightDimension) -> str:
    return "\n".join(
        [dimension.summary.strip(), *[
            str(item).strip() for item in dimension.bullets if str(item).strip()
        ]]
    ).strip()


def validate_insight_dimensions(
    dimensions: guide_schemas.InsightDimensions,
    pages: list[str],
    *,
    mode: str,
) -> tuple[dict, int, int, str]:
    """校验标准详细版固定七维与逐字原文依据。"""
    normalized: dict[str, dict] = {}
    raw_points = 0
    kept_points = 0
    empty_keys: list[str] = []
    no_evidence: list[str] = []
    generic_hits: list[str] = []

    for key in guide_schemas.MODEL_DIMENSION_KEYS:
        dimension = getattr(dimensions, key)
        label = _DIMENSION_TITLES[key]
        body = _dimension_text(dimension)
        if not body:
            empty_keys.append(label)
        hits = [phrase for phrase in _GENERIC_PHRASES if phrase in body]
        if hits:
            generic_hits.extend(f"{label}：{phrase}" for phrase in hits)
        raw_items = [item.model_dump() for item in dimension.evidence]
        raw_points += len(raw_items)
        evidence, _dropped = _verify_exact_points(raw_items, pages)
        kept_points += len(evidence)
        if not evidence:
            no_evidence.append(label)
        normalized[key] = {
            "title": label,
            "summary": dimension.summary.strip(),
            "points": [
                str(item).strip() for item in dimension.bullets if str(item).strip()
            ][:8],
            "evidence": evidence,
            "source": "model",
        }

    if empty_keys:
        return (
            normalized, raw_points, kept_points,
            "模型响应结构不完整：" + "、".join(empty_keys) + "为空",
        )
    if generic_hits:
        return (
            normalized, raw_points, kept_points,
            "模型响应内容空泛：" + "；".join(generic_hits[:3]),
        )
    if no_evidence:
        return (
            normalized, raw_points, kept_points,
            "原文校验未通过：" + "、".join(no_evidence) + "没有可逐字定位的原文依据",
        )
    if kept_points == 0:
        return (
            normalized, raw_points, kept_points,
            "原文校验未通过：模型没有返回可定位的逐字引文",
        )
    return (
        {"kind": "deep", "dimensions": normalized},
        raw_points, kept_points, "",
    )


def validate_quick_guide(
    quick: guide_schemas.QuickGuide,
) -> tuple[dict, str]:
    """精简版只判断价值，不允许提前泄露参数或步骤。"""
    text = re.sub(r"\s+", " ", quick.text or "").strip()
    if not text:
        return {}, "精简摘要版正文为空"
    if "\n" in (quick.text or "") or re.search(r"(^|\s)#{1,6}\s", quick.text or ""):
        return {}, "精简摘要版必须是一个连贯自然段，不能使用标题或分点"
    if re.search(r"(?:第\s*\d+\s*页|式\s*\(?\d|公式|[A-Za-z]\s*=)", text):
        return {}, "精简摘要版不得出现逐页页码、公式或数学表达式"
    if re.search(
        r"\d+(?:\.\d+)?\s*(?:Hz|kHz|MHz|s|ms|秒|分钟|小时|km|m|cm|%|°|度)",
        text,
        re.IGNORECASE,
    ):
        return {}, "精简摘要版不得出现参数值和单位"
    if any(phrase in text for phrase in _GENERIC_PHRASES):
        return {}, "精简摘要版出现空泛套话"
    return {"kind": "quick", "text": text}, ""


def validate_audit_guide(
    audit: guide_schemas.AuditGuide,
    pages: list[str],
) -> tuple[dict, int, int, str]:
    """校验方法重点版：方法占比、逐步骤证据与页码。"""
    normalized_steps: list[dict] = []
    raw_points = 0
    kept_points = 0
    missing_evidence: list[str] = []
    generic_hits: list[str] = []

    for step in audit.steps:
        raw = [item.model_dump() for item in step.evidence]
        raw_points += len(raw)
        evidence, dropped = _verify_exact_points(raw, pages)
        kept_points += len(evidence)
        if not evidence:
            missing_evidence.append(step.title)
        step_text = " ".join(
            [step.title, step.purpose, step.input, step.method, step.output,
             step.metric, step.risk]
        )
        generic_hits.extend(
            phrase for phrase in _GENERIC_PHRASES if phrase in step_text
        )
        normalized_steps.append({
            "order": step.order,
            "title": step.title.strip(),
            "purpose": step.purpose.strip(),
            "input": step.input.strip(),
            "method": step.method.strip(),
            "output": step.output.strip(),
            "params": [item.model_dump() for item in step.params],
            "metric": step.metric.strip(),
            "risk": step.risk.strip(),
            "page": evidence[0]["page"] if evidence else None,
            "evidence": evidence,
        })

    normalized_steps.sort(key=lambda item: item["order"])
    boundaries = [item.strip() for item in audit.boundaries if item.strip()]
    core_goal = audit.core_goal.strip()
    method_text = "\n".join(
        " ".join([
            step["title"], step["purpose"], step["input"], step["method"],
            step["output"], step["metric"], step["risk"],
            *[
                f"{param.get('name', '')} {param.get('value', '')} "
                f"{param.get('unit', '')} {param.get('note', '')}"
                for param in step["params"]
            ],
        ])
        for step in normalized_steps
    )
    boundary_text = "\n".join(boundaries)
    total_chars = len(core_goal) + len(method_text) + len(boundary_text)
    method_share = len(method_text) / max(total_chars, 1)
    if method_share < 0.70:
        return (
            {}, raw_points, kept_points,
            f"方法重点版方法内容仅占 {method_share:.0%}，必须至少 70%",
        )
    if generic_hits:
        return (
            {}, raw_points, kept_points,
            "方法重点版出现空泛套话：" + "；".join(generic_hits[:3]),
        )
    if missing_evidence:
        return (
            {}, raw_points, kept_points,
            "方法步骤缺少可逐字定位的页码依据：" + "、".join(missing_evidence),
        )
    parameters = [
        {**param, "step": step["order"], "page": step["page"]}
        for step in normalized_steps
        for param in step["params"]
    ]
    plan_steps = [
        {
            "order": step["order"],
            "title": step["title"],
            "purpose": step["purpose"],
            "method": step["method"],
            "suggested_params": [
                {
                    "name": param.get("name", ""),
                    "suggested_value": param.get("value", ""),
                    "unit": param.get("unit", ""),
                    "reason": param.get("note", ""),
                }
                for param in step["params"]
            ],
            "expected_metric": step["metric"],
            "risk": step["risk"],
            "source_page": step["page"],
        }
        for step in normalized_steps
    ]
    return (
        {
            "kind": "audit",
            "core_goal": core_goal,
            "steps": normalized_steps,
            "parameters": parameters,
            "plan_steps": plan_steps,
            "boundaries": boundaries,
            "method_share": round(method_share, 3),
        },
        raw_points, kept_points, "",
    )


def structure_dimension(index: fulltext.PaperIndex | None) -> dict:
    """论文结构与目录：只使用本地索引，不让模型生成页码。"""
    if index is None:
        items: list[dict] = []
        source = "未建立原文索引"
    else:
        level_one = [section for section in index.sections if section.level <= 1]
        selected = level_one or list(index.sections)
        items = [
            {
                "title": clean_model_text(section.title),
                "page_start": section.start_page,
                "page_end": section.end_page,
                "level": section.level,
                "source": section.source,
            }
            for section in selected[:16]
        ]
        source = {
            "toc": "PDF 目录书签",
            "heading": "原文标题排版",
            "pages": "按页分组的候选结构",
        }.get(index.section_source, "原文索引")
    summary = (
        f"从{source}提取 {len(items)} 个一级章节，页码为程序读取的原文页码。"
        if items else "这篇论文没有可读取的章节目录。"
    )
    return {
        "title": "论文结构与目录",
        "summary": summary,
        "points": [],
        "evidence": [],
        "items": items,
        "source": "local",
    }


def _merge_sections(existing: dict[str, dict], incoming: list[dict]) -> None:
    """同一章节可能被多个请求讲到（章节跨页），按标题合并，不重复堆叠。"""
    for section in incoming:
        title = str(section.get("title") or "").strip()
        if not title:
            continue
        key = _norm(title)
        target = existing.get(key)
        if target is None:
            target = {
                "title": title,
                "level": int(section.get("level") or 2),
                "summary": "", "steps": [], "params": [], "scope": "", "limits": "",
                "assumptions": [], "reproducibility": [], "relation": "",
                "points": [], "background": [], "open_questions": [],
            }
            existing[key] = target
        for name in ("summary", "scope", "limits", "relation"):
            value = str(section.get(name) or "").strip()
            if value and value not in target[name]:
                target[name] = (target[name] + " " + value).strip() if target[name] else value
        for name in (
            "steps",
            "params",
            "assumptions",
            "reproducibility",
            "open_questions",
        ):
            for item in section.get(name) or []:
                text = str(item).strip()
                if text and text not in target[name]:
                    target[name].append(text)
        target["points"].extend(section.get("points") or [])
        target["background"].extend(section.get("background") or [])


def _merge_related(store_map: dict[str, dict], incoming: list[dict]) -> None:
    for item in incoming:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        doi = normalize_doi(item.get("doi") or "")
        key = f"doi:{doi}" if doi else f"title:{_norm(title)}"
        if key in store_map:
            # 同一篇被推荐两次：保留信息更全的一条（补 DOI / 推荐理由）
            current = store_map[key]
            if not current.get("doi") and doi:
                current["doi"] = doi
            if len(str(item.get("why") or "")) > len(str(current.get("why") or "")):
                current["why"] = item["why"]
            continue
        store_map[key] = {
            "category": item.get("category") or "理论基础",
            "title": title,
            "authors": str(item.get("authors") or "").strip(),
            "year": str(item.get("year") or "").strip(),
            "venue": str(item.get("venue") or "").strip(),
            "doi": doi,
            "why": str(item.get("why") or "").strip(),
            "verified": False,
            "verification": "unmatched",
            "external_ids": {},
        }


def legacy_dimensions_from_sections(
    sections: list[dict],
    paper: Paper,
) -> dict[str, dict]:
    """把旧版按章节导览映射成七维结构，保证已有内容仍能阅读。"""
    def matching(*needles: str) -> list[dict]:
        found = [
            section for section in sections
            if any(needle in clean_model_text(section.get("title") or "").lower()
                   for needle in needles)
        ]
        return found or sections[:1]

    def collect(groups: list[dict], *, summary_limit: int = 4) -> tuple[str, list[str], list[dict]]:
        summaries = [
            str(section.get("summary") or "").strip()
            for section in groups if str(section.get("summary") or "").strip()
        ]
        bullets: list[str] = []
        for section in groups:
            for name in ("steps", "params", "assumptions", "reproducibility"):
                bullets.extend(str(item).strip() for item in section.get(name) or [] if str(item).strip())
            bullets.extend(
                str(section.get(name) or "").strip()
                for name in ("scope", "limits")
                if str(section.get(name) or "").strip()
            )
        evidence = [
            point for section in groups for point in section.get("points") or []
        ]
        return " ".join(summaries[:summary_limit]).strip(), list(dict.fromkeys(bullets))[:8], evidence

    background_groups = matching("introduction", "background", "引言", "背景", "abstract")
    method_groups = matching("method", "data", "processing", "方法", "数据", "流程")
    result_groups = matching("result", "discussion", "conclusion", "结果", "讨论", "结论")
    data_groups = matching(
        "data", "dataset", "sample", "experiment", "数据", "样本", "实验"
    )

    output: dict[str, dict] = {}
    for key, groups in (
        ("background", background_groups),
        ("method", method_groups),
        ("assumptions", method_groups),
        ("data", data_groups),
        ("findings", result_groups),
        ("limitations", sections),
        ("outlook", sections),
    ):
        summary, bullets, evidence = collect(groups)
        output[key] = {
            "title": _DIMENSION_TITLES[key],
            "summary": summary,
            "points": bullets,
            "evidence": evidence,
            "source": "legacy",
        }

    output["limitations"]["points"] = list(dict.fromkeys([
        *output["limitations"]["points"],
        *[
            question for section in sections
            for question in section.get("open_questions") or []
        ],
    ]))[:8]
    output["outlook"]["summary"] = (
        "旧版导览没有单独保存研究展望；可结合作者明示的后续工作重新生成。"
    )
    return output


def sections_from_dimensions(dimensions: dict[str, dict]) -> list[dict]:
    """给现有 Agent 与旧调用保留一个章节兼容视图，不改变七维主结构。"""
    output: list[dict] = []
    for key in guide_schemas.INSIGHT_DIMENSION_KEYS:
        dimension = dimensions.get(key) or {}
        output.append({
            "title": dimension.get("title") or _DIMENSION_TITLES[key],
            "level": 1,
            "summary": dimension.get("summary") or "",
            "points": dimension.get("evidence") or [],
            "steps": dimension.get("points") or [],
            "params": [],
            "assumptions": [],
            "scope": "",
            "limits": dimension.get("summary") if key == "limitations" else "",
            "reproducibility": [],
            "relation": "",
            "background": [],
            "open_questions": [],
            "title_matched": False,
        })
    return output


def verify_related(
    items: list[dict],
    http_client,
    *,
    max_lookups: int = MAX_RELATED_LOOKUPS,
    record: bool = True,
) -> list[dict]:
    """按 DOI 核验推荐文献：OpenAlex 优先，其次 Crossref。

    核验通过的用外部权威元数据**覆盖**模型给的标题/年份（模型的记忆不可信），
    核验不上的保留模型版本但标 `unmatched`——界面据此禁止加入文献库。
    """
    lookups = 0
    for item in items:
        doi = normalize_doi(item.get("doi") or "")
        if not doi:
            item["verification"] = "no_doi"
            continue
        if lookups >= max_lookups:
            item["verification"] = "skipped"
            continue
        lookups += 1
        work = None
        source = ""
        try:
            work, _ = openalex_mod.fetch_by_doi(doi, http_client)
            if work is not None:
                source = "openalex"
        except Exception as exc:  # noqa: BLE001 - 网络失败要如实降级，不抛给界面
            log.warning("OpenAlex 核验失败 %s：%s", doi, exc)
            work = None
        if work is None:
            try:
                cross, _ = crossref_mod.fetch_doi(doi, http_client)
            except Exception as exc:  # noqa: BLE001
                log.warning("Crossref 核验失败 %s：%s", doi, exc)
                cross = None
            if cross is not None:
                item.update({
                    "title": cross.title or item["title"],
                    "authors": "; ".join(cross.authors) or item.get("authors", ""),
                    "year": cross.year or item.get("year", ""),
                    "venue": cross.venue or item.get("venue", ""),
                    "doi": normalize_doi(cross.doi) or doi,
                    "verified": True,
                    "verification": "crossref",
                    "external_ids": {"doi": normalize_doi(cross.doi) or doi},
                    "external_metadata": {"crossref": cross.summary()},
                })
                continue
        if work is not None:
            item.update({
                "title": work.title or item["title"],
                "authors": "; ".join(work.authors) or item.get("authors", ""),
                "year": work.year or item.get("year", ""),
                "venue": work.venue or item.get("venue", ""),
                "doi": normalize_doi(work.doi) or doi,
                "verified": True,
                "verification": source or "openalex",
                "external_ids": {
                    "openalex": work.openalex_id,
                    "doi": normalize_doi(work.doi) or doi,
                },
                "external_metadata": {source or "openalex": work.summary()},
            })
            if record:
                item["_work"] = work      # 供调用方写 SourceRecord（不落库）
            continue
        item["verification"] = "unmatched"
    return items


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def build(
    paper: Paper,
    client: LLMClient,
    *,
    mode: str = "deep",
    http_client=None,
    verify_related_works: bool = True,
    max_requests: int = fulltext.DEFAULT_MAX_REQUESTS,
    max_chars: int = CORE_INPUT_CHARS,
) -> GuideOutcome:
    """生成一份阅读导览（非流式入口）。内部就是 `build_stream` 跑完一遍。"""
    outcome: GuideOutcome | None = None
    for event in build_stream(
        paper, client, mode=mode, http_client=http_client,
        verify_related_works=verify_related_works,
        max_requests=max_requests, max_chars=max_chars,
    ):
        if event.outcome is not None:
            outcome = event.outcome
    return outcome or GuideOutcome(
        paper_id=paper.id, status="failed", reason="生成过程没有返回任何结果",
        finished_at=_now(),
    )


def _prepare_legacy_sections(
    payload: guide_schemas.PaperInsightPayload,
    index: fulltext.PaperIndex | None,
    pages: list[str],
) -> tuple[list[dict], int, int]:
    """兼容旧版 sections 响应；仍按本地引文和目录校验。"""
    sections: dict[str, dict] = {}
    raw_points = 0
    kept_points = 0
    for section in payload.sections:
        item = section.model_dump()
        raw_points += len(item.get("points") or [])
        item["points"], _dropped = verify_points(item.get("points") or [], pages)
        kept_points += len(item["points"])
        kept_background = []
        for entry in item.get("background") or []:
            source = str(entry.get("source") or "").strip()
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            if source:
                kept_background.append({"text": text, "source": source})
            else:
                item["open_questions"].append(text)
        item["background"] = kept_background
        _merge_sections(sections, [item])
    merged = list(sections.values())
    for section in merged:
        match = match_section(index, section["title"])
        if match:
            section.update(match)
        else:
            section["title_matched"] = False
    return merged, raw_points, kept_points


def build_stream(
    paper: Paper,
    client: LLMClient,
    *,
    mode: str = "deep",
    http_client=None,
    verify_related_works: bool = True,
    max_requests: int = fulltext.DEFAULT_MAX_REQUESTS,
    max_chars: int = CORE_INPUT_CHARS,
):
    """**流式**生成阅读导览：逐步 yield `GuideEvent`，最后一个是终态。

    阶段与大图一致：准备文献证据 → 连接模型 → AI 思考（有则实时追加）→
    生成正式导览（实时追加）→ 核验引文与外部文献 → 汇总。
    被拒、失败、部分失败都如实产出终态，不抛异常给界面。
    """
    if mode not in guide_prompts.READING_MODES:
        mode = "deep"
    pages = evidence_mod.load_pages(paper)
    clean_pages = [clean_model_text(page) for page in pages]
    ready, reason = readiness(paper)
    if not ready:
        yield GuideEvent(stage="refused", detail=reason, outcome=GuideOutcome(
            paper_id=paper.id, status="refused", reason=reason,
            page_count=len(pages), finished_at=_now(),
        ))
        return

    index = fulltext.build_index(paper)
    core = core_excerpt(index, max_chars=min(max_chars, CORE_INPUT_CHARS)) if index else None
    if core is None or not core.text.strip():
        text = (
            "原文核心段落解析不完整："
            "未能从摘要、引言、方法、结果或结论中提取到可读正文"
        )
        yield GuideEvent(stage="failed", detail=text, outcome=GuideOutcome(
            paper_id=paper.id, status="failed", reason=text,
            page_count=len(pages), pages_analyzed=0, finished_at=_now(),
        ))
        return

    yield GuideEvent(
        stage="prepare",
        detail=(
            f"从 {len(pages)} 页中提取 {len(core.labels)} 个核心段落"
            f"（{len(core.text)} 字），优先覆盖摘要、引言、方法、结果与结论"
        ),
    )

    model_id = ""
    elapsed = 0.0
    thinking_all: list[str] = []
    answer_chars = 0
    attempts = 0
    mode_content: dict = {}
    related: dict[str, dict] = {}
    raw_points = 0
    kept_points = 0
    accepted = False
    attempt_failures: list[str] = []
    coverage_notes: list[str] = []
    input_scope = "；".join(core.labels)
    core_title = "核心章节：" + "、".join(
        dict.fromkeys(label.split("（", 1)[0] for label in core.labels)
    )
    core_start = min(core.pages) if core.pages else 1
    core_end = max(core.pages) if core.pages else len(pages)

    for attempt in range(AUTO_RETRY_LIMIT + 1):
        attempts += 1
        retry_note = "（自动重试）" if attempt else ""
        yield GuideEvent(
            stage="connect",
            detail=f"{retry_note}核心原文：{core_title}（第 {core_start}–{core_end} 页）",
        )
        user = guide_prompts.guide_user(
            clean_model_text(paper.title), core_title, (core_start, core_end), core.text,
            mode=mode, input_scope=input_scope,
        )
        if attempt and attempt_failures:
            user += (
                "\n\n# 上一次响应未通过校验\n"
                f"{attempt_failures[-1]}\n"
                f"请严格按「{guide_prompts.READING_MODE_LABELS[mode]}」的独立结构重写，"
                "不要把上一次内容缩句复用。"
            )
        content_parts: list[str] = []
        attempt_reason = ""
        schema_fallback = False
        while True:
            try:
                for event in client.stream(
                    guide_prompts.GUIDE_SYSTEM, user,
                    json_schema=(
                        None if schema_fallback
                        else guide_schemas.MODE_JSON_SCHEMAS[mode]
                    ),
                    structured=not schema_fallback,
                    max_tokens=2500 if mode == "quick" else 9000,
                ):
                    if event.kind == "reasoning" and event.text:
                        thinking_all.append(event.text)
                        yield GuideEvent(
                            stage="thinking", text=event.text,
                            thinking_chars=sum(len(item) for item in thinking_all),
                        )
                    elif event.kind == "content" and event.text:
                        content_parts.append(event.text)
                        answer_chars += len(event.text)
                        yield GuideEvent(
                            stage="answering", text=event.text, answer_chars=answer_chars,
                        )
                    elif event.kind == "done":
                        model_id = event.model or model_id
                        elapsed += float(event.elapsed_s or 0.0)
                break
            except LLMBadResponseError as exc:
                attempt_reason = f"模型响应异常：{str(exc)[:240]}"
                break
            except LLMError as exc:
                if (
                    not schema_fallback
                    and not content_parts
                    and _schema_mode_unsupported(exc)
                ):
                    schema_fallback = True
                    yield GuideEvent(
                        stage="fallback",
                        detail="模型不接受 JSON 结构约束，改用提示词约束重新请求",
                    )
                    continue
                attempt_reason = f"模型连接或服务响应异常：{str(exc)[:240]}"
                break
        if schema_fallback:
            coverage_notes.append("模型不支持 JSON 结构约束，已自动改用提示词约束")

        raw_answer = "".join(content_parts).strip()
        if not attempt_reason and not raw_answer:
            attempt_reason = (
                "模型响应异常：未返回正文"
                + ("（只收到思考内容）" if thinking_all else "")
            )
        payload = extract_json(raw_answer) if raw_answer and not attempt_reason else None
        if raw_answer and not attempt_reason and payload is None:
                attempt_reason = "模型响应异常：返回内容无法解析为当前模式的结构化解读"

        parsed = None
        if payload is not None and not attempt_reason:
            try:
                normalized_payload = guide_schemas.normalize_mode_payload(payload, mode)
                parsed = guide_schemas.MODE_PAYLOAD_MODELS[mode].model_validate(
                    normalized_payload
                )
            except Exception as exc:  # noqa: BLE001
                details = []
                errors = getattr(exc, "errors", None)
                if callable(errors):
                    for item in errors()[:3]:
                        location = ".".join(str(part) for part in item.get("loc", []))
                        details.append(f"{location}: {item.get('msg', '')}")
                attempt_reason = (
                    f"模型响应不符合「{guide_prompts.READING_MODE_LABELS[mode]}」结构："
                    f"{type(exc).__name__}"
                    + (f"（{'；'.join(details)}）" if details else "")
                )

        if parsed is not None and not attempt_reason:
            if mode == "quick":
                mode_content, attempt_reason = validate_quick_guide(parsed.quick)
            elif mode == "audit":
                mode_content, raw_points, kept_points, attempt_reason = validate_audit_guide(
                    parsed.audit, clean_pages
                )
            else:
                mode_content, raw_points, kept_points, attempt_reason = (
                    validate_insight_dimensions(parsed.dimensions, clean_pages, mode=mode)
                )

            if not attempt_reason:
                _merge_related(related, [item.model_dump() for item in parsed.related])
                accepted = True
                break

        attempt_failures.append(attempt_reason or "未知生成错误")
        coverage_notes.append(
            f"第 {attempt + 1} 次生成未通过：{attempt_failures[-1]}"
        )
        if attempt < AUTO_RETRY_LIMIT:
            yield GuideEvent(
                stage="retry",
                detail=f"{attempt_failures[-1]}；正在自动重试 1 次",
            )

    if not accepted:
        reason_text = attempt_failures[-1] if attempt_failures else "模型响应异常"
        failure = (
            f"已自动重试 {AUTO_RETRY_LIMIT} 次；{reason_text}"
            if attempts > 1 else reason_text
        )
        yield GuideEvent(stage="failed", detail=failure, outcome=GuideOutcome(
            paper_id=paper.id, status="failed", reason=failure,
            model=model_id, elapsed_s=round(elapsed, 2), requests=attempts,
            failed_chunks=max(attempts - 1, 0),
            page_count=len(pages), pages_analyzed=len(core.pages),
            attempts=attempts, raw_points=raw_points, kept_points=kept_points,
            input_sections=list(core.labels),
            coverage_notes=coverage_notes,
            thinking_text="".join(thinking_all), finished_at=_now(),
        ))
        return

    related_list_values = list(related.values())
    if http_client is not None and verify_related_works and related_list_values:
        yield GuideEvent(
            stage="verify",
            detail=f"核验 {len(related_list_values)} 篇推荐文献的元数据（OpenAlex → Crossref）",
        )
        verify_related(related_list_values, http_client)
    else:
        for item in related_list_values:
            item["verification"] = "skipped"

    related_list = sorted(
        related_list_values,
        key=lambda item: (item.get("verification") != "openalex"
                          and item.get("verification") != "crossref",
                          item.get("category", ""), item.get("title", "")),
    )[:MAX_RELATED]
    for item in related_list:
        item.pop("_work", None)

    analyzed = set(core.pages)
    if index is not None and analyzed:
        cover = fulltext.coverage(index, analyzed)
        coverage_notes.append(
            f"输入覆盖 {cover['pages_analyzed']}/{cover['page_count']} 个核心页、"
            f"{cover['sections_covered']}/{cover['sections_total']} 个章节块，"
            f"共 {attempts} 次模型请求"
        )
    if core.truncated:
        coverage_notes.append("核心原文超过输入预算，已按摘要→引言→方法→结果→结论顺序截取")
    if attempts > 1 and accepted:
        coverage_notes.append("首次响应未通过质量校验，第二次响应通过")
    dimensions = mode_content.get("dimensions", {})
    guide_sections = sections_from_dimensions(dimensions) if mode == "deep" else []
    guide = {
        "schema_version": 3,
        "paper_id": paper.id,
        "title": paper.title or "",
        "model": model_id,
        "mode": mode,
        "mode_content": mode_content,
        "generated_at": _now(),
        "dimensions": dimensions,
        "sections": guide_sections,
        "related": related_list,
        "notes": coverage_notes[:20],
        "legacy": False,
        "core_input": {
            "sections": list(core.labels),
            "pages": list(core.pages),
            "chars": len(core.text),
            "truncated": core.truncated,
        },
        "stats": {
            "requests": attempts,
            "attempts": attempts,
            "raw_points": raw_points,
            "kept_points": kept_points,
            "dropped_points": max(raw_points - kept_points, 0),
            "page_count": len(pages),
            "pages_analyzed": len(analyzed),
            "input_sections": len(core.labels),
            "input_truncated": core.truncated,
        },
        # 过程性内容：与正式导览分开存，界面单独折叠展示，不作为证据
        "thinking": {
            "text": "".join(thinking_all),
            "model": model_id,
            "generated_at": _now(),
            "note": "模型生成的过程性内容，不作为原文证据或科学结论",
        } if thinking_all else {},
    }
    thinking_text = "".join(thinking_all)
    status = "ok"
    yield GuideEvent(stage="done", outcome=GuideOutcome(
        paper_id=paper.id, status=status,
        guide=guide, model=model_id, elapsed_s=round(elapsed, 2),
        requests=attempts, failed_chunks=max(attempts - 1, 0),
        dropped_points=max(raw_points - kept_points, 0),
        related_total=len(related_list),
        related_verified=sum(1 for item in related_list if item.get("verified")),
        page_count=len(pages), pages_analyzed=len(analyzed),
        attempts=attempts, raw_points=raw_points, kept_points=kept_points,
        input_sections=list(core.labels), coverage_notes=coverage_notes,
        thinking_text=thinking_text,
        finished_at=_now(),
    ))


# ---------------------------------------------------------------------------
# 相关文献 → 当前项目文献库
# ---------------------------------------------------------------------------


def existing_related_ids(project_id: str, items: list[dict]) -> dict[str, str]:
    """这批推荐里，哪些已经在当前项目里。返回 {条目 key: 已有 Paper.id}。

    判定顺序与 ingest 一致：OpenAlex ID → DOI。项目隔离由 project_id 保证。
    """
    from ..literature.ingest import find_paper_by_doi

    found: dict[str, str] = {}
    for index, item in enumerate(items):
        key = related_key(item)
        openalex_id = (item.get("external_ids") or {}).get("openalex") or ""
        paper = repo.find_paper_by_openalex_id(project_id, openalex_id) if openalex_id else None
        if paper is None and item.get("doi"):
            paper = find_paper_by_doi(project_id, item["doi"])
        if paper is None:
            # 标题兜底：同一篇文献在别处被导入过、但没有 DOI 时也能识别出来
            target = _norm(item.get("title") or "")
            if target:
                for candidate in repo.list_all(Paper, project_id):
                    if _norm(candidate.title or "") == target:
                        paper = candidate
                        break
        if paper is not None:
            found[key] = paper.id
    return found


def related_key(item: dict) -> str:
    doi = normalize_doi(item.get("doi") or "")
    return f"doi:{doi}" if doi else f"title:{_norm(item.get('title') or '')}"


def add_related_to_project(project_id: str, item: dict) -> tuple[Paper | None, bool, str]:
    """把一条**已核验**的推荐文献加入当前项目文献库。

    返回 (paper, 是否新增, 说明)。未核验的条目一律拒绝：不能把编造的文献写进库。
    """
    if not item.get("verified"):
        return None, False, "这条推荐没能通过外部核验，不能加入文献库"
    title = str(item.get("title") or "").strip()
    if not title:
        return None, False, "缺少标题，无法加入"

    openalex_id = (item.get("external_ids") or {}).get("openalex") or ""
    existing = repo.find_paper_by_openalex_id(project_id, openalex_id) if openalex_id else None
    if existing is None and item.get("doi"):
        from ..literature.ingest import find_paper_by_doi

        existing = find_paper_by_doi(project_id, item["doi"])
    if existing is not None:
        return existing, False, "已在文献库里"

    paper = repo.save(
        Paper(
            id=repo.new_id("paper"),
            project_id=project_id,
            title=title,
            authors=str(item.get("authors") or ""),
            year=str(item.get("year") or ""),
            venue=str(item.get("venue") or ""),
            doi=normalize_doi(item.get("doi") or ""),
            verified=False,      # 没有本机 PDF 可核对，不冒充"已核实"
            verification_status=VerificationStatus.UNVERIFIED,
            external_ids=dict(item.get("external_ids") or {}),
            external_metadata=dict(item.get("external_metadata") or {}),
            origin="guide",
        )
    )
    _record_related_source(project_id, paper, item)
    log.info("阅读导览加入文献库：%s（项目 %s，来源 %s）",
             paper.id, project_id, item.get("verification"))
    return paper, True, "已加入文献库"


def _record_related_source(project_id: str, paper: Paper, item: dict) -> None:
    """外部查询留痕：这一条是从哪次核验、哪个来源来的。"""
    verification = str(item.get("verification") or "unmatched")
    provider = {
        "openalex": openalex_mod.PROVIDER,
        "crossref": crossref_mod.PROVIDER,
    }.get(verification, verification or "unknown")
    kind = QueryKind.WORK_LOOKUP if provider == openalex_mod.PROVIDER else QueryKind.DOI_LOOKUP
    repo.save(
        SourceRecord(
            id=repo.new_id("src"),
            project_id=project_id,
            paper_id=paper.id,
            provider=provider,
            query_kind=kind,
            query_value=normalize_doi(item.get("doi") or ""),
            external_id=(item.get("external_ids") or {}).get("openalex", "")
            or normalize_doi(item.get("doi") or ""),
            status=QueryStatus.OK,
            note=f"阅读导览推荐（{item.get('category', '')}）：{item.get('why', '')[:120]}",
        )
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

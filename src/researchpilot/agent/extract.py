"""C3-B：从已上传的 PDF 里抽取有出处的研究要素。

流程（每一步都能单独验证）：

1. **正文可靠性**先过闸：乱码正文直接拒绝，**不调用模型**——
   纯文本模型没有能力把字形索引错位读回来，让它猜等于生产伪造结论；
2. 调模型做结构化抽取（提示词里明确要求 JSON 与逐字引文）；
3. **逐条证据校验**：引文在原文里精确定位才算 `stated`；疑似转述记 `implied`；
   找不到的整条丢弃；
4. 通过校验的条目落 `PaperClaim`，带 paper_id / quote / 页码 / 校验状态 / 模型标识；
5. 未通过校验的条目不落库，但作为 dropped 返回，供界面如实说明。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..literature.text_quality import body_is_reliable
from ..literature import fulltext
from ..storage import repository as repo
from ..storage.models import Certainty, Paper, PaperClaim
from . import evidence as evidence_mod
from . import prompts
from . import schemas
from .client import LLMClient
from .providers import LLMBadResponseError, LLMError

log = logging.getLogger(__name__)


@dataclass
class ExtractionOutcome:
    paper_id: str
    status: str                       # ok | refused | failed
    reason: str = ""
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    model: str = ""
    elapsed_s: float = 0.0
    refused_by_quality: bool = False  # 因正文乱码被拒（没有调用模型）
    # --- 反馈所需的事实：用户必须能看到"到底发生了什么" ---
    provider: str = ""
    raw_item_count: int = 0           # 模型返回的条目数（未校验前）
    raw_text_len: int = 0             # 模型返回文本长度
    excerpt_pages: int = 0            # 本次分析用了多少页
    excerpt_chars: int = 0            # 本次分析用了多少字符
    # --- P0-B：全文覆盖的事实 ---
    page_count: int = 0               # 这篇文献一共有多少页正文
    pages_analyzed: int = 0           # 实际送到模型的页数（去重后）
    requests: int = 0                 # 实际发出的模型请求数
    sections_total: int = 0
    sections_analyzed: int = 0
    failed_sections: int = 0          # 有请求失败/被跳过的块数
    duplicate_skipped: int = 0        # 跨章重复的条目（同一句被多章提到）
    section_source: str = ""          # toc | heading | pages
    section_runs: list[dict] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    timeout_s: float = 0.0
    usage: dict = field(default_factory=dict)
    finished_at: str = ""
    finish_reason: str = ""
    has_reasoning: bool = False
    reasoning_len: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def verdict(self) -> str:
        """给界面用的终态。**不允许"零条结论"被当成成功。**"""
        if self.status == "refused":
            return "refused"
        if self.status == "failed":
            return "failed"
        if not self.kept and self.failed_sections:
            return "failed"
        if self.raw_item_count == 0:
            return "empty"                 # 模型没给出任何条目
        if not self.kept:
            return "all_rejected"          # 模型给了，但一条都没通过证据校验
        if self.dropped or self.failed_sections:
            return "partial"
        return "success"

    def to_dict(self) -> dict:
        """持久化形式（不含任何密钥）。"""
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "model": self.model,
            "provider": self.provider,
            "elapsed_s": self.elapsed_s,
            "timeout_s": self.timeout_s,
            "raw_item_count": self.raw_item_count,
            "raw_text_len": self.raw_text_len,
            "excerpt_pages": self.excerpt_pages,
            "excerpt_chars": self.excerpt_chars,
            "page_count": self.page_count,
            "pages_analyzed": self.pages_analyzed,
            "requests": self.requests,
            "sections_total": self.sections_total,
            "sections_analyzed": self.sections_analyzed,
            "failed_sections": self.failed_sections,
            "duplicate_skipped": self.duplicate_skipped,
            "section_source": self.section_source,
            "section_runs": self.section_runs,
            "coverage_notes": self.coverage_notes,
            "kept_count": len(self.kept),
            "dropped_count": len(self.dropped),
            "kept": self.kept[:50],
            "dropped": [
                {"kind": item.get("kind"), "text": item.get("text", ""),
                 "reason": item.get("drop_reason", "")}
                for item in self.dropped[:30]
            ],
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "has_reasoning": self.has_reasoning,
            "reasoning_len": self.reasoning_len,
            "finished_at": self.finished_at,
        }


def paper_pages(paper: Paper) -> list[str]:
    return evidence_mod.load_pages(paper)


def extraction_readiness(paper: Paper) -> tuple[bool, str]:
    """能不能对这篇文献做 AI 抽取。返回 (可以, 原因)。"""
    pages, reason = evidence_mod.load_pages_with_reason(paper)
    if not pages:
        return False, reason
    reliable, reason = body_is_reliable("\n".join(pages))
    if not reliable:
        return False, reason
    return True, ""


def _certainty_label(certainty: Certainty) -> str:
    return {
        Certainty.STATED: "原文事实（逐字命中）",
        Certainty.IMPLIED: "疑似转述（待人工核查）",
        Certainty.INFERRED: "AI 推断（未核实）",
    }.get(certainty, "未核实")


def extract_paper(
    client: LLMClient,
    paper: Paper,
    *,
    max_chars: int = fulltext.DEFAULT_SECTION_CHARS,
    persist: bool = True,
    section_wise: bool = True,
    max_requests: int = fulltext.DEFAULT_MAX_REQUESTS,
) -> ExtractionOutcome:
    """对单篇文献做一次**全文**抽取。被拒或失败都如实返回，不把异常抛给界面。

    P0-B 之前：只把前 8 页、1.2 万字符送进模型，22 页的论文等于只读三分之一，
    界面上却写着"AI 文献分析"。现在按章节（没有书签就按候选标题，再不行按页组）
    切成**有界请求**，逐块调用模型，最后合并——每一页都被送到，并如实记录覆盖率。

    ``section_wise=False`` 时退回单次请求（老行为），只用于对比与排障。
    """
    from datetime import datetime

    def _stamp(outcome: ExtractionOutcome) -> ExtractionOutcome:
        outcome.finished_at = datetime.now().isoformat(timespec="seconds")
        return outcome

    pages = paper_pages(paper)
    credentials = client.store.current()
    index = fulltext.build_index(paper) if pages else None
    base = {
        "provider": getattr(credentials, "provider", "") if credentials else "",
        "timeout_s": float(getattr(credentials.config(), "timeout", 0.0)) if credentials else 0.0,
        "page_count": len(pages),
        "excerpt_pages": len(pages),
        "excerpt_chars": sum(len(page or "") for page in pages),
        "section_source": index.section_source if index else "",
        "sections_total": len(index.sections) if index else 0,
        "coverage_notes": list(index.notes) if index else [],
    }
    reliable, reason = extraction_readiness(paper)
    if not reliable:
        log.info("跳过 %s 的 AI 抽取：%s", paper.id, reason)
        return _stamp(ExtractionOutcome(
            paper_id=paper.id, status="refused", reason=reason,
            refused_by_quality=bool(pages), **base,
        ))

    chunks = _plan_chunks(index, pages, max_chars=max_chars, max_requests=max_requests,
                          section_wise=section_wise)
    refs = {"P1": paper}
    kept_all: list[dict] = []
    dropped_all: list[dict] = []
    runs: list[dict] = []
    seen_quotes: set[str] = set()
    duplicates = 0
    raw_total = 0
    analyzed_pages: set[int] = set()
    failed = 0
    total_elapsed = 0.0
    model_id = ""
    raw_text_len = 0
    usage: dict = {}
    finish_reason = ""
    has_reasoning = False
    reasoning_len = 0

    for chunk in chunks:
        run = {"title": chunk.title, "pages": [chunk.start_page, chunk.end_page],
               "chars": chunk.chars, "status": "ok", "reason": "", "kept": 0, "dropped": 0}
        user = prompts.extract_user([("P1", paper.title, chunk.text)])
        try:
            payload, response = client.complete_json(
                prompts.EXTRACT_SYSTEM, user,
                json_schema=schemas.EXTRACTION_JSON_SCHEMA,
                max_tokens=4000,
            )
        except LLMBadResponseError as exc:
            run.update(status="failed", reason=f"模型返回的内容不是合法 JSON：{exc}")
            failed += 1
            runs.append(run)
            continue
        except LLMError as exc:
            run.update(status="failed", reason=str(exc))
            failed += 1
            runs.append(run)
            continue

        model_id = model_id or response.model
        total_elapsed += float(response.elapsed_s or 0.0)
        raw_text_len += len(response.text or "")
        finish_reason = response.finish_reason or finish_reason
        has_reasoning = has_reasoning or bool(response.has_reasoning)
        reasoning_len += int(response.reasoning_len or 0)
        for key, value in dict(response.usage or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        analyzed_pages.update(chunk.pages)

        try:
            parsed = schemas.ExtractionPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 结构不符也要变成可见终态
            run.update(status="failed",
                       reason=f"模型返回的 JSON 结构不符合约定：{type(exc).__name__}: {str(exc)[:160]}")
            failed += 1
            runs.append(run)
            continue

        items = [item.model_dump() for item in parsed.items]
        raw_total += len(items)
        kept, dropped = verify_items(items, refs, pages_by_ref={"P1": pages})
        # 跨章去重：同一句原文很可能被多章复述，不能变成多条"结论"
        unique: list[dict] = []
        for item in kept:
            key = evidence_mod.normalize_for_match(item.get("quote") or "")
            if key and key in seen_quotes:
                duplicates += 1
                continue
            if key:
                seen_quotes.add(key)
            unique.append(item)
        kept_all.extend(unique)
        dropped_all.extend(dropped)
        run.update(kept=len(unique), dropped=len(dropped),
                   duplicate=len(kept) - len(unique))
        runs.append(run)

    if not chunks:
        return _stamp(ExtractionOutcome(
            paper_id=paper.id, status="failed",
            reason="这篇文献的按页正文是空的，没有可分析的内容", **base,
        ))

    if persist and kept_all:
        save_claims(paper, kept_all, model_id=model_id or "unknown")

    if failed and failed == len(chunks):
        status = "failed"
        # 全部失败时给出**真实原因**（不是一句笼统的"没取到结果"）
        reason_text = next(
            (run["reason"] for run in runs if run["status"] == "failed"),
            "全部章节块都没有取到结果",
        )
    else:
        status = "ok"
        reason_text = (
            f"{failed}/{len(chunks)} 个章节块没有取到结果，其余章节已完成" if failed else ""
        )
    notes = list(base.pop("coverage_notes", []))
    notes.extend(
        f"{run['title'][:40]}（第 {run['pages'][0]}–{run['pages'][1]} 页）失败：{run['reason'][:80]}"
        for run in runs if run["status"] == "failed"
    )
    if duplicates:
        notes.append(f"有 {duplicates} 条引文在多处重复出现，已合并为一条（避免同一句话变成多条结论）")
    if analyzed_pages and index is not None:
        cover = fulltext.coverage(index, analyzed_pages)
        notes.append(
            f"覆盖 {cover['pages_analyzed']}/{cover['page_count']} 页、"
            f"{cover['sections_covered']}/{cover['sections_total']} 个章节块，"
            f"共 {len(chunks)} 次模型请求"
        )
        for miss in cover["sections_missing"][:5]:
            notes.append(f"未覆盖：{miss['title']}（第 {miss['pages'][0]}–{miss['pages'][1]} 页）")

    return _stamp(ExtractionOutcome(
        paper_id=paper.id, status=status, reason=reason_text,
        kept=kept_all, dropped=dropped_all, raw_item_count=raw_total,
        model=model_id, elapsed_s=round(total_elapsed, 2), raw_text_len=raw_text_len,
        usage=usage, finish_reason=finish_reason, has_reasoning=has_reasoning,
        reasoning_len=reasoning_len,
        excerpt_pages=len(analyzed_pages), excerpt_chars=base.get("excerpt_chars", 0),
        pages_analyzed=len(analyzed_pages), requests=len(chunks),
        sections_total=base.get("sections_total", 0),
        sections_analyzed=len({idx for chunk in chunks for idx in chunk.section_indexes}),
        failed_sections=failed, duplicate_skipped=duplicates,
        section_source=base.get("section_source", ""),
        section_runs=runs, coverage_notes=notes,
        **{
            key: value for key, value in base.items()
            if key not in {"coverage_notes", "excerpt_pages", "excerpt_chars",
                           "section_source", "sections_total"}
        },
    ))


def _plan_chunks(index, pages: list[str], *, max_chars: int, max_requests: int,
                 section_wise: bool) -> list:
    """切分请求。短文献与 ``section_wise=False`` 都退回单次请求。"""
    if index is None:
        excerpt = evidence_mod.excerpt_with_pages(
            pages, max_chars=max_chars, max_pages=max(len(pages), 1)
        )
        return [fulltext.RequestChunk(
            title="全文（无分页正文结构）", start_page=1, end_page=len(pages),
            pages=tuple(range(1, len(pages) + 1)), text=excerpt, section_indexes=(),
        )]
    if not section_wise:
        excerpt = evidence_mod.excerpt_with_pages(
            pages, max_chars=max_chars, max_pages=max(len(pages), 1)
        )
        return [fulltext.RequestChunk(
            title="全文（单次请求模式）", start_page=1, end_page=len(pages),
            pages=tuple(range(1, len(pages) + 1)), text=excerpt,
            section_indexes=tuple(section.index for section in index.sections),
        )]
    return fulltext.plan_requests(index, max_chars=max_chars, max_requests=max_requests)


def verify_items(
    items: list[dict],
    refs: dict[str, Paper],
    *,
    pages_by_ref: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    """逐条证据校验。返回 (通过, 被丢弃)。**这是入库前的唯一闸门。**"""
    kept: list[dict] = []
    dropped: list[dict] = []

    for item in items:
        kind = str(item.get("kind") or "").strip()
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        ref = str(item.get("paper_ref") or "").strip().upper()

        if kind not in schemas.KIND_LABELS:
            dropped.append({**item, "drop_reason": f"未知的条目类型：{kind!r}"})
            continue
        if not text:
            dropped.append({**item, "drop_reason": "条目内容为空"})
            continue
        if not quote:
            dropped.append({**item, "drop_reason": "没有引文，无法核对出处"})
            continue
        text_norm = evidence_mod.normalize_for_match(text)
        quote_norm = evidence_mod.normalize_for_match(quote)
        if text_norm == quote_norm or (
            len(text_norm) >= 18
            and (text_norm in quote_norm or quote_norm in text_norm)
        ):
            dropped.append({
                **item,
                "drop_reason": "概览文字是原文摘抄，不是改写概括",
            })
            continue
        paper = refs.get(ref)
        if paper is None:
            dropped.append({**item, "drop_reason": f"文献编号 {ref!r} 对不上任何已上传文献"})
            continue
        pages = pages_by_ref.get(ref) or []
        match = evidence_mod.locate_quote(quote, pages)
        if not match.accepted:
            dropped.append({**item, "drop_reason": match.detail})
            continue
        kept.append({
            "kind": kind,
            "text": text,
            "quote": quote,
            "paper_id": paper.id,
            "paper_ref": ref,
            "page": match.page,
            "certainty": match.certainty,
            "certainty_label": _certainty_label(match.certainty),
            "detail": match.detail,
            "page_hint": item.get("page_hint"),
        })
    return kept, dropped


def save_claims(paper: Paper, kept: list[dict], *, model_id: str) -> list[PaperClaim]:
    """把通过校验的条目落库。同一篇文献的同一条引文不重复写。"""
    existing = {
        (claim.kind, claim.quote)
        for claim in repo.list_all(PaperClaim)
        if claim.paper_id == paper.id
    }
    saved: list[PaperClaim] = []
    for item in kept:
        key = (item["kind"], item["quote"])
        if key in existing:
            continue
        claim = PaperClaim(
            id=repo.new_id("claim"),
            project_id=paper.project_id,
            paper_id=paper.id,
            claim=item["text"],
            locator=f"第 {item['page']} 页" if item.get("page") else "",
            certainty=item["certainty"],
            kind=item["kind"],
            quote=item["quote"],
            page=item.get("page"),
            quote_verified=item["certainty"] is Certainty.STATED,
            created_by="llm",
            model=model_id,
        )
        repo.save(claim)
        saved.append(claim)
    return saved


def claims_of(paper: Paper) -> list[PaperClaim]:
    """按 paper_id 过滤，不依赖 project_id 字段（旧记录可能没有它）。"""
    return [
        claim for claim in repo.list_all(PaperClaim)
        if claim.paper_id == paper.id
    ]

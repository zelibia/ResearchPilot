"""证据校验：AI 给出的引文必须能在原文里找得到。

这是整套 AI 能力里**唯一不依赖模型**的一环，也是防幻觉的底线：

1. 规范化后**精确命中** → `stated`（原文事实），页码以本地定位为准；
2. 未精确命中但字面重合度高 → `implied`（疑似转述），标"待人工核查"；
3. 都对不上 → `unsupported`，**整条丢弃**，不允许进正式结论。

两条纪律：

- 模型自报的页码只作参考，落库页码一律取本地定位结果；
- 引文绝不"润色"后再比对——那等于帮模型圆谎。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..literature import paths as paper_paths
from ..storage.models import Certainty, Paper

# pages.txt 里的分页标记，与 literature/ingest.py 写入的格式一致
PAGE_SPLIT_RE = re.compile(r"=====\s*第\s*(\d+)\s*页\s*=====")

# implied 判定的字面重合度阈值（按字符二元组算）
IMPLIED_OVERLAP = 0.80

# 只容忍**首尾标点**的差异：模型很爱给引文补一个句号，那仍然是逐字引用。
# 内容里少一个字或多一个字都不在此列——那属于转述，必须人工看一眼。
_BOUNDARY_PUNCT = "。，、；：！？.,;:!?\"'“”‘’()（）[]【】"
MIN_TRIMMED_LEN = 6


@dataclass(frozen=True)
class EvidenceMatch:
    certainty: Certainty
    page: int | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.certainty is not Certainty.INFERRED


def load_pages(paper: Paper) -> list[str]:
    """读入库时留下的 `<pdf>.pages.txt`，返回按页正文。读不到返回空列表。"""
    return load_pages_with_reason(paper)[0]


def load_pages_with_reason(paper: Paper) -> tuple[list[str], str]:
    """读按页正文，并给出**能分辨原因**的失败说明（P0-1）。

    以前只有一句"PDF 未成功解析或正文文件已不在"，把三种完全不同的情况混在一起：
    文件被移走、跨系统路径没解析、正文根本没解析出来。用户据此无法判断该做什么。
    """
    path = paper_paths.pages_path(paper.local_path)
    if path is None:
        return [], paper_paths.describe_missing(paper.local_path)
    if not paper_paths.resolve_local_path(paper.local_path):
        return [], paper_paths.describe_missing(paper.local_path)
    if not path.is_file():
        return [], (
            f"还没有生成按页正文（缺少 {path.name}）：这篇文献入库时没有完成解析，"
            "重新上传同一份 PDF 即可补齐。"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"按页正文读不出来（{path.name}）：{exc}"
    if not PAGE_SPLIT_RE.search(text):
        # 没有分页标记时把整篇当一页——这里必须返回二元组：
        # 返回单个列表会让所有调用方在解包时直接崩（离线回归 G08 抓到过）。
        return [text], ""
    parts = PAGE_SPLIT_RE.split(text)
    # split 结果是 [前言, 页号, 正文, 页号, 正文, ...]
    pages: list[str] = []
    for index in range(1, len(parts), 2):
        body = parts[index + 1] if index + 1 < len(parts) else ""
        pages.append(body)
    return pages, ""


def normalize_for_match(text: str) -> str:
    """比对用的规范化：统一宽度、去掉所有空白与换行连字符。

    只做这些**无损**变换；改字、改词一律不做。
    """
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = value.replace("-\n", "").replace("‐\n", "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("‐", "-").replace("–", "-").replace("—", "-")
    return value


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def overlap_ratio(quote: str, page_text: str) -> float:
    """引文与某页的字面重合度（字符二元组）。"""
    left = _bigrams(normalize_for_match(quote))
    if not left:
        return 0.0
    right = _bigrams(normalize_for_match(page_text))
    if not right:
        return 0.0
    return len(left & right) / len(left)


def locate_quote(quote: str, pages: list[str]) -> EvidenceMatch:
    """把一条引文定位到页码。返回的 page 是**本地定位**结果，不是模型自报的。"""
    normalized = normalize_for_match(quote)
    if not normalized:
        return EvidenceMatch(Certainty.INFERRED, None, "引文为空")

    best_ratio = 0.0
    best_page = None
    trimmed = normalized.strip(_BOUNDARY_PUNCT)
    for index, page in enumerate(pages, start=1):
        page_norm = normalize_for_match(page)
        if normalized in page_norm:
            return EvidenceMatch(Certainty.STATED, index, f"在第 {index} 页精确命中")
        if (
            trimmed
            and len(trimmed) >= MIN_TRIMMED_LEN
            and trimmed != normalized
            and trimmed in page_norm
        ):
            return EvidenceMatch(
                Certainty.STATED, index,
                f"在第 {index} 页命中（仅首尾标点与原文不同）",
            )
        ratio = overlap_ratio(quote, page)
        if ratio > best_ratio:
            best_ratio, best_page = ratio, index

    if best_ratio >= IMPLIED_OVERLAP:
        return EvidenceMatch(
            Certainty.IMPLIED, best_page,
            f"未逐字命中，但第 {best_page} 页字面重合 {best_ratio:.0%}，属疑似转述，需人工核查",
        )
    return EvidenceMatch(
        Certainty.INFERRED, None,
        f"原文中找不到该引文（最高重合仅 {best_ratio:.0%}），按未核实处理",
    )


def excerpt_with_pages(pages: list[str], *, max_chars: int = 12000, max_pages: int = 8) -> str:
    """给模型看的正文节选，带页码标记，方便它引用。"""
    chunks: list[str] = []
    used = 0
    for index, page in enumerate(pages[:max_pages], start=1):
        text = page.strip()
        if not text:
            continue
        budget = max_chars - used
        if budget <= 0:
            break
        body = text[:budget]
        chunks.append(f"【第 {index} 页】\n{body}")
        used += len(body)
    return "\n\n".join(chunks)

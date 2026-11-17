"""Validation and normalization helpers shared by service and HTTP layers."""

from __future__ import annotations

import re
from typing import Any

from .contracts import (
    ANCHOR_STATUSES,
    STANDARD_COLORS,
    TEXT_ANNOTATION_TYPES,
    AnnotationDraft,
    PageViewBox,
    ReaderAnchorSegment,
)
from .errors import (
    AnnotationError,
    invalid_anchor,
    invalid_color,
    invalid_type,
    read_only,
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_color(value: str) -> str:
    color = (value or "").strip().upper()
    if not _HEX_COLOR.fullmatch(color):
        raise invalid_color("颜色必须是 #RRGGBB；标准色为 " + ", ".join(STANDARD_COLORS))
    return color


def require_text_type(value: str) -> str:
    if value not in TEXT_ANNOTATION_TYPES:
        raise invalid_type(
            "标注类型只允许 highlight、underline、strikeout"
        )
    return value


def _segment_from_draft(draft: AnnotationDraft) -> ReaderAnchorSegment:
    return ReaderAnchorSegment(
        pageIndex=draft.page_index,
        quote=draft.quote,
        charStart=draft.selector.char_start,
        charEnd=draft.selector.char_end,
        rects=draft.rects,
        pageViewBox=draft.selector.page_view_box,
        textSource=draft.selector.text_source,
        textHash=draft.selector.text_hash,
    )


def normalized_selector(draft: AnnotationDraft) -> dict[str, Any]:
    """Return a strict P0-03 selector, preserving every page segment."""

    if not draft.quote.strip():
        raise AnnotationError("EMPTY_SELECTION", "选区文本不能为空")
    if not draft.selector.text_hash.strip():
        raise invalid_anchor("锚点 textHash 不能为空")
    if draft.selector.text_source == "ocr" and not draft.rects:
        raise AnnotationError(
            "OCR_TEXT_UNAVAILABLE",
            "OCR 标注缺少可用于定位的文字矩形",
        )
    if draft.anchor_status not in ANCHOR_STATUSES:
        raise invalid_anchor("非法 anchorStatus")

    segments = list(draft.selector.segments)
    if not segments:
        segments = [_segment_from_draft(draft)]

    previous_page = -1
    for segment in segments:
        if segment.page_index < previous_page:
            raise invalid_anchor("跨页 segment 必须按 pageIndex 升序排列")
        previous_page = segment.page_index
        if not segment.rects:
            raise invalid_anchor("每个 segment 至少需要一个归一化矩形")

    first = segments[0]
    last = segments[-1]
    if first.char_start != draft.selector.char_start:
        raise invalid_anchor("selector.charStart 与首个 segment 不一致")
    if last.char_end != draft.selector.char_end:
        raise invalid_anchor("selector.charEnd 与末个 segment 不一致")
    if first.page_index != draft.page_index:
        raise invalid_anchor("主记录页码必须等于首个 segment.pageIndex")

    joined_quote = "".join(segment.quote for segment in segments)
    if joined_quote != draft.quote:
        raise invalid_anchor(
            "segments.quote 拼接后必须等于完整选区 quote"
        )
    if segments[0].text_source != draft.selector.text_source:
        raise invalid_anchor("首个 segment.textSource 与 selector 不一致")
    if segments[0].text_hash != draft.selector.text_hash:
        raise invalid_anchor("首个 segment.textHash 与 selector 不一致")

    result = draft.selector.model_dump(by_alias=True)
    result["segments"] = [segment.model_dump(by_alias=True) for segment in segments]
    return result


def primary_text_source(selector: dict[str, Any]) -> str:
    segments = selector.get("segments") or []
    if segments:
        return str(segments[0].get("textSource") or "pdf_text")
    return str(selector.get("textSource") or "pdf_text")


def primary_text_hash(selector: dict[str, Any]) -> str:
    segments = selector.get("segments") or []
    if segments:
        return str(segments[0].get("textHash") or "")
    return str(selector.get("textHash") or "")


def cross_page(selector: dict[str, Any]) -> bool:
    pages = {
        int(segment.get("pageIndex", 0))
        for segment in selector.get("segments") or []
    }
    return len(pages) > 1


def page_view_box(selector: dict[str, Any]) -> PageViewBox:
    raw = selector.get("pageViewBox") or {}
    try:
        return PageViewBox.model_validate(raw)
    except Exception:
        return PageViewBox(width=1, height=1, rotation=0)


def ensure_editable(read_only_flag: bool, source: str) -> None:
    if read_only_flag or source == "external_pdf":
        raise read_only()

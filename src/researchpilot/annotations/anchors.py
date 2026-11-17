"""Annotation -> P0-03 ReaderAnchor conversion."""

from __future__ import annotations

from typing import Any

from ..storage.v116.orm import Annotation
READER_PROTOCOL_VERSION = "rp.reader.v1"


def _rects(raw: Any) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "x": float(item.get("x", 0)),
                "y": float(item.get("y", 0)),
                "width": float(item.get("width", 0)),
                "height": float(item.get("height", 0)),
            }
        )
    return result


def _view_box(raw: Any) -> dict[str, float | int]:
    raw = raw or {}
    return {
        "width": float(raw.get("width", 1)),
        "height": float(raw.get("height", 1)),
        "rotation": int(raw.get("rotation", 0)),
    }


def _segments(annotation: Annotation) -> list[dict[str, Any]]:
    selector = annotation.selector_json or {}
    raw_segments = selector.get("segments") or []
    if raw_segments:
        return [
            {
                "pageIndex": int(segment.get("pageIndex", annotation.page_index)),
                "quote": str(segment.get("quote", annotation.quote)),
                "charStart": int(
                    segment.get("charStart", annotation.char_start or 0)
                ),
                "charEnd": int(
                    segment.get(
                        "charEnd",
                        annotation.char_end
                        if annotation.char_end is not None
                        else annotation.char_start or 0,
                    )
                ),
                "rects": _rects(segment.get("rects") or annotation.rects_json),
                "pageViewBox": _view_box(
                    segment.get("pageViewBox")
                    or selector.get("pageViewBox")
                ),
                "textSource": str(
                    segment.get("textSource") or annotation.text_source
                ),
                "textHash": str(
                    segment.get("textHash") or annotation.text_hash or ""
                ),
            }
            for segment in raw_segments
        ]
    return [
        {
            "pageIndex": annotation.page_index,
            "quote": annotation.quote,
            "charStart": int(annotation.char_start or 0),
            "charEnd": int(
                annotation.char_end
                if annotation.char_end is not None
                else annotation.char_start or 0
            ),
            "rects": _rects(annotation.rects_json),
            "pageViewBox": _view_box(selector.get("pageViewBox")),
            "textSource": annotation.text_source,
            "textHash": annotation.text_hash,
        }
    ]


def annotation_to_reader_anchor(annotation: Annotation) -> dict[str, Any]:
    segments = _segments(annotation)
    first = segments[0]
    last = segments[-1]
    first_rect = (first["rects"] or [{"x": 0, "y": 0}])[0]
    last_rect = (last["rects"] or [{"x": 0, "y": 0}])[-1]
    return {
        "protocolVersion": READER_PROTOCOL_VERSION,
        "anchorId": annotation.id,
        "documentId": annotation.document_id,
        "fileAssetId": annotation.file_id,
        "start": {
            "pageIndex": first["pageIndex"],
            "charOffset": first["charStart"],
            "x": float(first_rect.get("x", 0)),
            "y": float(first_rect.get("y", 0)),
        },
        "end": {
            "pageIndex": last["pageIndex"],
            "charOffset": last["charEnd"],
            "x": float(last_rect.get("x", 0))
            + float(last_rect.get("width", 0)),
            "y": float(last_rect.get("y", 0))
            + float(last_rect.get("height", 0)),
        },
        "quote": annotation.quote,
        "segments": segments,
        "matchStatus": annotation.anchor_status,
        "coordinateVersion": "page-normalized-v1",
        "createdAt": annotation.created_at,
    }


def anchor_resolve_result(
    annotation: Annotation,
    *,
    mode: str,
) -> dict[str, Any]:
    anchor = annotation_to_reader_anchor(annotation)
    if mode == "png_fallback":
        return {
            "requestedAnchor": anchor,
            "resolvedPageIndex": annotation.page_index,
            "strategy": "page_only",
            "matchStatus": "no_text_layer",
            "rects": [],
            "reason": "PNG 降级模式仅按页码定位",
            "highlightRendered": False,
        }
    if annotation.anchor_status == "out_of_range":
        return {
            "requestedAnchor": anchor,
            "resolvedPageIndex": annotation.page_index,
            "strategy": "none",
            "matchStatus": "out_of_range",
            "rects": [],
            "reason": "目标页已不存在",
            "highlightRendered": False,
        }
    if annotation.anchor_status in {"none", "no_text_layer"}:
        return {
            "requestedAnchor": anchor,
            "resolvedPageIndex": annotation.page_index,
            "strategy": "page_only",
            "matchStatus": annotation.anchor_status,
            "rects": [],
            "reason": (
                "当前来源没有文字层，已仅定位到页"
                if annotation.anchor_status == "no_text_layer"
                else "锚点未能定位到原文"
            ),
            "highlightRendered": False,
        }
    rects = anchor["segments"][0]["rects"]
    return {
        "requestedAnchor": anchor,
        "resolvedPageIndex": annotation.page_index,
        "strategy": "char_offset_and_quote",
        "matchStatus": annotation.anchor_status,
        "rects": rects,
        "reason": "",
        "highlightRendered": True,
    }

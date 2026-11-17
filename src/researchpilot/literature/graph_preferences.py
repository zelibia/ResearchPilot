"""Per-paper citation graph preferences stored on the existing Paper summary.

Only UI state lives here: focused relation ids and the last selected relation.
Citation direction, review status, notes and categories remain on
``PaperRelation`` so the graph and the citation list share one source of truth.
"""

from __future__ import annotations

from typing import Any

from ..storage import repository as repo
from ..storage.models import Paper, PaperRelation

SUMMARY_KEY = "citation_graph_preferences"
MAX_FOCUSED = 5


def _load(paper: Paper) -> dict[str, Any]:
    value = (paper.summary or {}).get(SUMMARY_KEY)
    if not isinstance(value, dict):
        return {"focused_relation_ids": [], "selected_relation_id": ""}
    focused = value.get("focused_relation_ids")
    if not isinstance(focused, list):
        focused = []
    selected = value.get("selected_relation_id")
    return {
        "focused_relation_ids": [
            str(item) for item in focused if str(item or "").strip()
        ][:MAX_FOCUSED],
        "selected_relation_id": str(selected or ""),
    }


def _save(paper: Paper, value: dict[str, Any]) -> Paper:
    paper.summary = {**(paper.summary or {}), SUMMARY_KEY: value}
    return repo.save(paper)


def focused_ids(paper: Paper) -> list[str]:
    return _load(paper)["focused_relation_ids"]


def is_focused(paper: Paper, relation_id: str) -> bool:
    return relation_id in focused_ids(paper)


def toggle_focus(paper: Paper, relation: PaperRelation) -> tuple[bool, str]:
    """Toggle one relation and enforce the five-node UI limit.

    Returns ``(is_now_focused, message)``. Rejected relations cannot become
    highlights because rejected records are outside the default graph.
    """
    value = _load(paper)
    focused = value["focused_relation_ids"]
    if relation.id in focused:
        focused.remove(relation.id)
        value["focused_relation_ids"] = focused
        _save(paper, value)
        return False, "已取消重点标记"
    if relation.review_status.value == "rejected":
        return False, "已驳回的关系不能标记为重点"
    if len(focused) >= MAX_FOCUSED:
        return False, f"单篇最多标记 {MAX_FOCUSED} 个重点节点，请先取消其他重点"
    focused.append(relation.id)
    value["focused_relation_ids"] = focused
    _save(paper, value)
    return True, "已标记为重点"


def selected_relation_id(paper: Paper) -> str:
    return _load(paper)["selected_relation_id"]


def select_relation(paper: Paper, relation_id: str) -> None:
    value = _load(paper)
    value["selected_relation_id"] = str(relation_id or "")
    _save(paper, value)

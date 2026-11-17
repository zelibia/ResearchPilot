"""Per-paper citation graph view versions.

Versions store only view configuration in ``Paper.summary``. They never duplicate
or rewrite PaperRelation rows, paper metadata, evidence, or citation direction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..storage import repository as repo
from ..storage.models import Paper, PaperRelation, ReviewStatus

AUTO_VERSION = "__auto__"
MAX_CUSTOM_VERSIONS = 3
SUMMARY_KEY = "citation_graph_versions"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load(paper: Paper) -> dict[str, Any]:
    value = (paper.summary or {}).get(SUMMARY_KEY)
    if not isinstance(value, dict):
        value = {"schema_version": 1, "custom": []}
    custom = value.get("custom")
    if not isinstance(custom, list):
        custom = []
    return {"schema_version": 1, "custom": custom}


def _save(paper: Paper, data: dict[str, Any]) -> Paper:
    paper.summary = {**(paper.summary or {}), SUMMARY_KEY: data}
    return repo.save(paper)


def list_custom(paper: Paper) -> list[dict[str, Any]]:
    return [
        item for item in load(paper)["custom"]
        if isinstance(item, dict) and str(item.get("id") or "")
    ]


def version(paper: Paper, version_id: str) -> dict[str, Any] | None:
    if version_id == AUTO_VERSION:
        return None
    return next(
        (item for item in list_custom(paper) if item.get("id") == version_id),
        None,
    )


def create(
    paper: Paper,
    name: str,
    relations: list[PaperRelation],
    *,
    show_rejected: bool,
    nodes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    title = " ".join(str(name or "").split()).strip()[:60]
    if not title:
        raise ValueError("请输入版本名称")
    data = load(paper)
    if len(data["custom"]) >= MAX_CUSTOM_VERSIONS:
        raise ValueError(f"单篇文献最多保留 {MAX_CUSTOM_VERSIONS} 个自定义版本")
    item = {
        "id": repo.new_id("cgv"),
        "name": title,
        "created_at": _now(),
        "show_rejected": bool(show_rejected),
        "nodes": nodes or {
            relation.id: {
                "visible": True,
                "graph_group": relation.graph_group,
                "note": relation.review_note,
                "sort": index,
            }
            for index, relation in enumerate(relations)
        },
    }
    data["custom"].append(item)
    _save(paper, data)
    return item


def delete(paper: Paper, version_id: str) -> bool:
    data = load(paper)
    kept = [item for item in data["custom"] if item.get("id") != version_id]
    changed = len(kept) != len(data["custom"])
    if changed:
        data["custom"] = kept
        _save(paper, data)
    return changed


def set_show_rejected(paper: Paper, version_id: str, value: bool) -> None:
    data = load(paper)
    for item in data["custom"]:
        if item.get("id") == version_id:
            item["show_rejected"] = bool(value)
            _save(paper, data)
            return


def node_config(
    paper: Paper, version_id: str, relation_id: str
) -> dict[str, Any]:
    item = version(paper, version_id)
    if item is None:
        return {}
    node = (item.get("nodes") or {}).get(relation_id)
    return dict(node) if isinstance(node, dict) else {}


def update_node(paper: Paper, version_id: str, relation_id: str, **updates: Any) -> None:
    data = load(paper)
    item = next(
        (row for row in data["custom"] if row.get("id") == version_id),
        None,
    )
    if item is None:
        raise ValueError("自动版不能保存视图覆盖")
    nodes = item.setdefault("nodes", {})
    node = nodes.get(relation_id)
    if not isinstance(node, dict):
        node = {}
        nodes[relation_id] = node
    for key in ("visible", "graph_group", "note", "sort"):
        if key in updates:
            node[key] = updates[key]
    _save(paper, data)


def move_order(
    paper: Paper,
    version_id: str,
    relation_id: str,
    delta: int,
    relations: list[PaperRelation],
) -> None:
    data = load(paper)
    item = next(
        (row for row in data["custom"] if row.get("id") == version_id),
        None,
    )
    if item is None:
        raise ValueError("自动版不能调整排序")
    ordered = sorted(
        relations,
        key=lambda relation: (
            int((item.get("nodes", {}).get(relation.id) or {}).get("sort", 10**9)),
            relation.id,
        ),
    )
    ids = [relation.id for relation in ordered]
    if relation_id not in ids:
        return
    index = ids.index(relation_id)
    target = max(0, min(len(ids) - 1, index + int(delta)))
    if target == index:
        return
    ids.insert(target, ids.pop(index))
    nodes = item.setdefault("nodes", {})
    for order, rid in enumerate(ids):
        node = nodes.setdefault(rid, {})
        node["sort"] = order
    _save(paper, data)


def clear_custom_notes(paper: Paper) -> int:
    data = load(paper)
    count = 0
    for item in data["custom"]:
        for node in (item.get("nodes") or {}).values():
            if isinstance(node, dict) and str(node.get("note") or ""):
                node["note"] = ""
                count += 1
    if count:
        _save(paper, data)
    return count

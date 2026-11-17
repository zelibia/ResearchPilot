"""Markdown <-> outline tree conversion and tree integrity helpers."""

from __future__ import annotations

import copy
import difflib
import re
import uuid
from typing import Any, Iterable

from .contracts import OutlineTreeNode
from .errors import OutlineError

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def flatten_tree(tree: list[OutlineTreeNode]) -> list[OutlineTreeNode]:
    output: list[OutlineTreeNode] = []

    def visit(nodes: list[OutlineTreeNode]) -> None:
        for node in nodes:
            output.append(node)
            visit(node.children)

    visit(tree)
    return output


def tree_to_markdown(tree: list[OutlineTreeNode]) -> str:
    chunks: list[str] = []
    for node in flatten_tree(tree):
        chunks.append(f"{'#' * node.heading_level} {node.title}".rstrip())
        if node.body_markdown.strip():
            chunks.append("")
            chunks.append(node.body_markdown.strip())
        chunks.append("")
    return "\n".join(chunks).rstrip() + ("\n" if chunks else "")


def _normal_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def parse_markdown(
    markdown: str,
    *,
    existing_tree: list[OutlineTreeNode] | None = None,
    preserve_node_ids: bool = True,
) -> tuple[list[OutlineTreeNode], list[str], list[str]]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[dict[str, Any]] = []
    for line_index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            headings.append(
                {
                    "line": line_index,
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                }
            )
    warnings: list[str] = []
    if not headings:
        raise OutlineError("MARKDOWN_INVALID", "Markdown 至少需要一个标题")

    existing = flatten_tree(existing_tree or [])
    by_title_level = {
        (_normal_title(node.title), node.heading_level): node
        for node in existing
    }
    by_title: dict[str, list[OutlineTreeNode]] = {}
    for node in existing:
        by_title.setdefault(_normal_title(node.title), []).append(node)
    matched: list[str] = []
    root: list[OutlineTreeNode] = []
    stack: list[OutlineTreeNode] = []
    for index, heading in enumerate(headings):
        end_line = (
            headings[index + 1]["line"]
            if index + 1 < len(headings)
            else len(lines)
        )
        body = "\n".join(lines[heading["line"] + 1 : end_line]).strip()
        match_node = None
        if preserve_node_ids:
            normalized = _normal_title(heading["title"])
            match_node = by_title_level.get((normalized, heading["level"]))
            if match_node is None:
                candidates = by_title.get(normalized, [])
                if len(candidates) == 1:
                    match_node = candidates[0]
        node = OutlineTreeNode(
            nodeId=(
                match_node.node_id
                if match_node
                else f"onode_{uuid.uuid4().hex}"
            ),
            title=heading["title"],
            headingLevel=heading["level"],
            orderIndex=0,
            bodyMarkdown=body,
            anchor=match_node.anchor if match_node else None,
            annotationIds=list(match_node.annotation_ids) if match_node else [],
            noteMarkdown=match_node.note_markdown if match_node else "",
            locked=match_node.locked if match_node else False,
            origin=match_node.origin if match_node else "user",
        )
        if match_node:
            matched.append(node.node_id)
        while stack and stack[-1].heading_level >= node.heading_level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)
    normalize_tree(root)
    if len({heading["title"] for heading in headings}) < len(headings):
        warnings.append("存在重复标题；稳定 ID 匹配可能需要在保存前人工确认")
    return root, warnings, matched


def normalize_tree(tree: list[OutlineTreeNode]) -> None:
    seen: set[str] = set()

    def visit(nodes: list[OutlineTreeNode], parent_id: str | None) -> None:
        nodes.sort(key=lambda node: node.order_index)
        for order, node in enumerate(nodes):
            if node.node_id in seen:
                raise OutlineError(
                    "OUTLINE_INVALID_TREE",
                    f"节点 ID 重复：{node.node_id}",
                )
            seen.add(node.node_id)
            node.order_index = order
            if parent_id and node.node_id == parent_id:
                raise OutlineError("OUTLINE_NODE_CYCLE", "节点不能成为自己的子节点")
            visit(node.children, node.node_id)

    visit(tree, None)


def validate_tree(tree: list[OutlineTreeNode]) -> None:
    normalize_tree(tree)
    visited: set[str] = set()

    def visit(node: OutlineTreeNode, ancestors: set[str]) -> None:
        if node.node_id in ancestors:
            raise OutlineError("OUTLINE_NODE_CYCLE", "大纲树存在循环父子关系")
        if not node.title.strip():
            raise OutlineError("OUTLINE_INVALID_TREE", "节点标题不能为空")
        visited.add(node.node_id)
        for child in node.children:
            visit(child, ancestors | {node.node_id})

    for item in tree:
        visit(item, set())
    if len(visited) != len(flatten_tree(tree)):
        raise OutlineError("OUTLINE_INVALID_TREE", "大纲树节点重复或不可达")


def find_node(
    tree: list[OutlineTreeNode],
    node_id: str,
) -> tuple[OutlineTreeNode | None, OutlineTreeNode | None, list[OutlineTreeNode]]:
    def visit(
        nodes: list[OutlineTreeNode],
        parent: OutlineTreeNode | None,
    ) -> tuple[OutlineTreeNode | None, OutlineTreeNode | None, list[OutlineTreeNode]]:
        for node in nodes:
            if node.node_id == node_id:
                return node, parent, nodes
            found, found_parent, siblings = visit(node.children, node)
            if found:
                return found, found_parent, siblings
        return None, None, []

    return visit(tree, None)


def remove_nodes(tree: list[OutlineTreeNode], node_ids: set[str]) -> list[str]:
    removed: list[str] = []

    def visit(nodes: list[OutlineTreeNode]) -> list[OutlineTreeNode]:
        kept: list[OutlineTreeNode] = []
        for node in nodes:
            if node.node_id in node_ids:
                removed.extend(item.node_id for item in flatten_tree([node]))
                continue
            node.children = visit(node.children)
            kept.append(node)
        return kept

    tree[:] = visit(tree)
    normalize_tree(tree)
    return removed


def clone_tree(tree: list[OutlineTreeNode]) -> list[OutlineTreeNode]:
    return [OutlineTreeNode.model_validate(item) for item in copy.deepcopy(tree)]


def diff_markdown(left: str, right: str) -> str:
    return "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile="left",
            tofile="right",
        )
    )


def subtree_ids(node: OutlineTreeNode) -> Iterable[str]:
    for item in flatten_tree([node]):
        yield item.node_id

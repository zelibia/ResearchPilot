"""Deterministic citation-graph exports using existing report/plan models."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import PATHS
from ..planning import draft as plan_store
from ..storage import repository as repo
from ..storage.models import (
    Artifact,
    Paper,
    PaperClaim,
    PaperRelation,
    PlanDraft,
    PlanMode,
    Project,
    RelationType,
    ReviewStatus,
)
from . import graph_versions


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_text(value: Any, fallback: str = "论文未提供") -> str:
    text = " ".join(str(value or "").split()).strip()
    return text or fallback


def _method_text(paper: Paper) -> str:
    guide = (paper.summary or {}).get("reading_guide") or {}
    mode_content = guide.get("mode_content") or {}
    if mode_content.get("kind") == "audit":
        steps = mode_content.get("steps") or []
        return _safe_text("；".join(
            _safe_text(step.get("method"), "")
            for step in steps
            if isinstance(step, dict) and step.get("method")
        ))
    dimensions = mode_content.get("dimensions") or {}
    method = dimensions.get("method") if isinstance(dimensions, dict) else {}
    if isinstance(method, dict):
        parts = [str(method.get("summary") or "")]
        parts.extend(str(item) for item in method.get("bullets") or [])
        text = "；".join(item.strip() for item in parts if item.strip())
        if text:
            return text
    claims = [
        claim for claim in repo.list_all(PaperClaim, paper.project_id)
        if claim.paper_id == paper.id and claim.kind == "method"
    ]
    return _safe_text("；".join(claim.claim for claim in claims[:6]))


def _parameters(paper: Paper) -> str:
    guide = (paper.summary or {}).get("reading_guide") or {}
    mode_content = guide.get("mode_content") or {}
    values: list[str] = []
    if mode_content.get("kind") == "audit":
        for param in mode_content.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            values.append(
                f"{param.get('name', '')}={param.get('value', '')}"
                f"{param.get('unit', '')}".strip("=")
            )
    if not values:
        for claim in repo.list_all(PaperClaim, paper.project_id):
            if claim.paper_id == paper.id and claim.kind in {"method", "assumption"}:
                values.append(claim.claim)
    return _safe_text("；".join(dict.fromkeys(values[:8])))


def _scope_limits(paper: Paper) -> str:
    guide = (paper.summary or {}).get("reading_guide") or {}
    mode_content = guide.get("mode_content") or {}
    if mode_content.get("kind") == "audit":
        values = [
            str(item).strip()
            for item in mode_content.get("boundaries") or []
            if str(item).strip()
        ]
        if values:
            return "；".join(values[:5])
    dimensions = mode_content.get("dimensions") or {}
    limitation = dimensions.get("limitations") if isinstance(dimensions, dict) else {}
    if isinstance(limitation, dict) and limitation.get("summary"):
        return str(limitation["summary"])
    claims = [
        claim for claim in repo.list_all(PaperClaim, paper.project_id)
        if claim.paper_id == paper.id and claim.kind == "limitation"
    ]
    return _safe_text("；".join(claim.claim for claim in claims[:5]))


def _confirmed_citations(
    paper: Paper, relations: list[PaperRelation], version_id: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    version_item = graph_versions.version(paper, version_id)
    configs = (version_item or {}).get("nodes") or {}
    for relation in relations:
        if relation.relation_type is not RelationType.CITES:
            continue
        if relation.review_status is ReviewStatus.REJECTED or not relation.downstream_ready:
            continue
        config = configs.get(relation.id) if isinstance(configs, dict) else {}
        if isinstance(config, dict) and config.get("visible") is False:
            continue
        other_id = (
            relation.to_paper_id
            if relation.from_paper_id == paper.id
            else relation.from_paper_id
        )
        other = repo.get(Paper, other_id)
        if other is None:
            continue
        group = (
            str(config.get("graph_group") or "")
            if isinstance(config, dict)
            else ""
        )
        if not group:
            group = relation.graph_group or (
                "method_source" if relation.from_paper_id == paper.id
                else "later_extension"
            )
        rows.append({
            "paper": other,
            "group": group,
            "relation": relation,
            "sort": int(config.get("sort", len(rows))) if isinstance(config, dict) else len(rows),
        })
    order = {"method_source": 0, "related": 1, "later_extension": 2}
    rows.sort(key=lambda item: (
        order.get(item["group"], 1),
        int(item["paper"].year) if str(item["paper"].year).isdigit() else 9999,
        item["sort"],
        item["paper"].title,
    ))
    return rows


def build_method_outline(
    project: Project,
    paper: Paper,
    relations: list[PaperRelation],
    version_id: str,
) -> dict[str, Any]:
    rows = _confirmed_citations(paper, relations, version_id)
    labels = {
        "method_source": "方法来源 / 前期工作",
        "later_extension": "后续延伸",
        "related": "相关研究",
    }
    items = [{
        "section": "当前论文",
        "title": paper.title,
        "year": paper.year,
        "contribution": _method_text(paper),
        "relation": "当前研究的主线方法",
    }]
    for row in rows:
        other = row["paper"]
        items.append({
            "section": labels.get(row["group"], "相关研究"),
            "title": other.title,
            "year": other.year,
            "contribution": _method_text(other),
            "relation": labels.get(row["group"], "相关研究"),
        })
    lines = [
        f"# 方法演进大纲：{paper.title or '当前论文'}",
        "",
        f"- 生成时间：{_now()}",
        "- 只收录已进入下游的引用关系；缺失的方法信息明确写“论文未提供”。",
        "",
    ]
    for section in ("方法来源 / 前期工作", "当前论文", "后续延伸", "相关研究"):
        group = [item for item in items if item["section"] == section]
        if not group:
            continue
        lines.append(f"## {section}")
        for item in group:
            lines.append(
                f"- **{item['title'] or '未命名文献'}**"
                f"（{item['year'] or '年份未知'}）"
            )
            lines.append(f"  - 核心方法贡献：{item['contribution']}")
            lines.append(f"  - 与本文关系：{item['relation']}")
        lines.append("")
    markdown = "\n".join(lines).strip() + "\n"
    return {
        "title": f"方法演进大纲：{paper.title or '当前论文'}",
        "items": items,
        "markdown": markdown,
    }


def save_outline_artifact(project: Project, outline: dict[str, Any]) -> Artifact:
    folder = PATHS.projects / project.id / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"method-evolution-outline-{stamp}.md"
    path.write_text(str(outline["markdown"]), encoding="utf-8")
    artifact = repo.save(Artifact(
        id=repo.new_id("art"),
        project_id=project.id,
        type="report",
        path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    ))
    return artifact


def create_comparison_draft(
    project: Project,
    paper: Paper,
    selected: list[Paper],
) -> PlanDraft:
    if len(selected) < 2:
        raise ValueError("至少选择两篇方法类文献")
    steps = []
    parameters: list[dict[str, Any]] = []
    for order, item in enumerate(selected, start=1):
        method_text = _method_text(item)
        parameter_text = _parameters(item)
        limits = _scope_limits(item)
        steps.append({
            "order": order,
            "title": item.title or "未命名方法",
            "purpose": "作为复现方案对比对象",
            "method": method_text,
            "suggested_params": [],
            "expected_metric": "论文未提供",
            "risk": limits,
            "parameters": parameter_text,
            "scope_limits": limits,
            "paper_id": item.id,
            "year": item.year,
            "origin": "source",
        })
        parameters.append({
            "paper_id": item.id,
            "paper_title": item.title,
            "parameters": parameter_text,
            "scope_limits": limits,
        })
    draft = repo.save(PlanDraft(
        id=repo.new_id("draft"),
        project_id=project.id,
        mode=PlanMode.IMPROVE,
        series_id=plan_store.new_series_id(),
        series_title="引用图谱复现方案对比",
        question=f"比较与《{paper.title or '当前论文'}》相关的方法路线",
        objective="形成可核对的方法对比，并据此选择复现或改进路线",
        steps=steps,
        parameters=parameters,
        risks=[
            f"{item.title or '未命名方法'}：{_scope_limits(item)}"
            for item in selected
        ],
        user_notes="由引用关系图谱导出；所有方法、参数和局限均来自库中已有文献记录，缺失项不补造。",
        origin="user",
        generated_at=_now(),
    ))
    return draft


def comparison_rows(selected: list[Paper]) -> list[dict[str, str]]:
    return [
        {
            "方法名称": f"{paper.title or '未命名方法'}（{paper.year or '年份未知'}）",
            "核心步骤": _method_text(paper),
            "关键参数": _parameters(paper),
            "适用场景与局限": _scope_limits(paper),
        }
        for paper in selected
    ]

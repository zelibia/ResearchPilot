"""Fixed read-only tools for ResearchPilot's project assistant.

The model never receives SQL, shell, Python, file paths, or a caller-supplied
project id. Python chooses from a fixed registry, validates every argument, and
scopes every result to the current ``AgentContext.project_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..storage import repository as repo
from ..storage.models import (
    Artifact,
    ExperimentRun,
    Paper,
    PaperClaim,
    PlanDraft,
    PlanVersion,
    Project,
)
from .context import AgentContext

log = logging.getLogger(__name__)

MAX_TOOL_CHARS = 14_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    kind: str = "read"
    max_results: int = 50


TOOL_SPECS: dict[str, ToolSpec] = {
    "project_overview": ToolSpec(
        "project_overview", "项目现场与真实记录计数", max_results=1
    ),
    "list_papers": ToolSpec(
        "list_papers", "按项目列出文献与阅读导览状态", max_results=20
    ),
    "get_paper": ToolSpec(
        "get_paper", "读取当前项目内一篇文献的元数据与导览摘要", max_results=1
    ),
    "list_claims": ToolSpec(
        "list_claims", "列出已分析条目与逐字证据", max_results=30
    ),
    "plan_context": ToolSpec(
        "plan_context", "读取已确认方案与当前草稿摘要", max_results=20
    ),
    "list_runs": ToolSpec(
        "list_runs", "列出实验运行状态", max_results=20
    ),
    "get_run": ToolSpec(
        "get_run", "读取一次运行的真实错误、摘要与产物数量", max_results=1
    ),
    "list_artifacts": ToolSpec(
        "list_artifacts", "列出一次运行的真实产物", max_results=30
    ),
}


@dataclass
class ToolResult:
    name: str
    ok: bool
    payload: Any
    project_id: str
    chars: int
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "ok": self.ok,
            "payload": self.payload,
            "error": self.error,
        }


def _bounded_limit(value: Any, spec: ToolSpec, default: int) -> int:
    if value is None:
        return min(default, spec.max_results)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if not 1 <= value <= spec.max_results:
        raise ValueError(f"limit must be between 1 and {spec.max_results}")
    return value


def _paper_in_project(context: AgentContext, paper_id: str) -> Paper:
    if not paper_id:
        raise ValueError("paper_id is required")
    paper = repo.get(Paper, paper_id)
    if paper is None or paper.project_id != context.project_id:
        raise ValueError("paper not found in the current project")
    return paper


def _run_in_project(context: AgentContext, run_id: str) -> ExperimentRun:
    if not run_id:
        raise ValueError("run_id is required")
    run = repo.get(ExperimentRun, run_id)
    if run is None or run.project_id != context.project_id:
        raise ValueError("run not found in the current project")
    return run


def _claim_summary(claim: PaperClaim, title: str = "") -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "paper_id": claim.paper_id,
        "paper_title": title,
        "kind": claim.kind,
        "claim": claim.claim,
        "quote": (claim.quote or "")[:300],
        "page": claim.page,
        "locator": claim.locator,
        "verified": bool(claim.quote_verified),
        "certainty": claim.certainty.value,
    }


def _guide_summary(paper: Paper) -> dict[str, Any]:
    guide = (paper.summary or {}).get("reading_guide") or {}
    sections = guide.get("sections") or []
    mode_content = guide.get("mode_content") or {}
    stats = guide.get("stats") or {}
    return {
        "generated": bool(sections or mode_content),
        "mode": guide.get("mode") or "",
        "mode_kind": mode_content.get("kind") or "",
        "section_count": len(sections),
        "page_count": stats.get("page_count", paper.page_count),
        "pages_analyzed": stats.get("pages_analyzed", 0),
        "failed_chunks": stats.get("failed_chunks", 0),
        "dropped_points": stats.get("dropped_points", 0),
        "limits": [
            str(section.get("limits") or "")
            for section in sections[:4]
            if section.get("limits")
        ],
        "reproducibility": [
            item
            for section in sections[:4]
            for item in (section.get("reproducibility") or [])[:2]
        ][:8],
    }


def _project_overview(context: AgentContext) -> dict[str, Any]:
    project = repo.get(Project, context.project_id)
    if project is None:
        raise ValueError("project not found")
    papers = repo.list_all(Paper, context.project_id)
    claims = repo.list_all(PaperClaim, context.project_id)
    return {
        "title": project.title,
        "domain": project.domain,
        "description": project.description,
        "counts": {
            "papers": len(papers),
            "verified_claims": sum(1 for item in claims if item.quote_verified),
            "unverified_claims": sum(1 for item in claims if not item.quote_verified),
            "candidates": sum(1 for item in papers if item.origin == "openalex"),
        },
    }


def _list_papers(context: AgentContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = _bounded_limit(args.get("limit"), TOOL_SPECS["list_papers"], 8)
    papers = sorted(
        repo.list_all(Paper, context.project_id),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    claims = repo.list_all(PaperClaim, context.project_id)
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim.paper_id] = counts.get(claim.paper_id, 0) + 1
    return [
        {
            "paper_id": paper.id,
            "title": paper.title,
            "year": paper.year,
            "venue": paper.venue,
            "doi": paper.doi,
            "verified": paper.verified,
            "page_count": paper.page_count,
            "claim_count": counts.get(paper.id, 0),
            "has_reading_guide": bool(
                ((paper.summary or {}).get("reading_guide") or {}).get("sections")
            ),
        }
        for paper in papers[:limit]
    ]


def _get_paper(context: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
    paper = _paper_in_project(context, str(args.get("paper_id") or ""))
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "verified": paper.verified,
        "verification_status": paper.verification_status.value,
        "page_count": paper.page_count,
        "reading_guide": _guide_summary(paper),
        "opened_page": context.paper_page,
    }


def _list_claims(context: AgentContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = _bounded_limit(args.get("limit"), TOOL_SPECS["list_claims"], 20)
    paper_id = str(args.get("paper_id") or context.paper_id)
    papers = {paper.id: paper for paper in repo.list_all(Paper, context.project_id)}
    if paper_id and paper_id not in papers:
        raise ValueError("paper not found in the current project")
    claims = [
        item
        for item in repo.list_all(PaperClaim, context.project_id)
        if not paper_id or item.paper_id == paper_id
    ]
    claims.sort(
        key=lambda item: (
            not item.quote_verified,
            item.page is None,
            item.page or 9999,
            item.id,
        )
    )
    return [
        _claim_summary(item, papers.get(item.paper_id).title if item.paper_id in papers else "")
        for item in claims[:limit]
    ]


def _plan_context(context: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = _bounded_limit(args.get("limit"), TOOL_SPECS["plan_context"], 6)
    versions = sorted(
        repo.list_all(PlanVersion, context.project_id),
        key=lambda item: (item.confirmed_at or item.created_at, item.id),
        reverse=True,
    )
    drafts = sorted(
        repo.list_all(PlanDraft, context.project_id),
        key=lambda item: (item.updated_at or item.created_at, item.id),
        reverse=True,
    )
    return {
        "current_selection": context.plan_selection,
        "versions": [
            {
                "version_id": item.id,
                "version": item.version,
                "series_title": item.series_title,
                "question": item.question[:240],
                "objective": item.objective[:240],
                "success_criteria": item.success_criteria[:240],
                "step_count": len(item.steps),
                "confirmed_at": item.confirmed_at,
            }
            for item in versions[:limit]
        ],
        "drafts": [
            {
                "draft_id": item.id,
                "status": item.status.value,
                "series_title": item.series_title,
                "question": item.question[:200],
                "objective": item.objective[:200],
                "step_count": len(item.steps),
                "evidence_count": item.evidence_count,
                "ai_step_count": item.ai_step_count,
                "updated_at": item.updated_at or item.created_at,
            }
            for item in drafts[:limit]
        ],
    }


def _list_runs(context: AgentContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = _bounded_limit(args.get("limit"), TOOL_SPECS["list_runs"], 10)
    runs = sorted(
        repo.list_all(ExperimentRun, context.project_id),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    return [
        {
            "run_id": run.id,
            "status": run.status.value,
            "engine": run.engine,
            "created_at": run.created_at,
            "ended_at": run.ended_at,
            "error_count": len(run.errors),
            "warning_count": len(run.warnings),
            "duration_s": run.duration_s,
            "artifact_count": len(
                [
                    item
                    for item in repo.list_all(Artifact, context.project_id)
                    if item.run_id == run.id
                ]
            ),
        }
        for run in runs[:limit]
    ]


def _get_run(context: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
    run = _run_in_project(context, str(args.get("run_id") or context.run_id))
    artifacts = [
        item
        for item in repo.list_all(Artifact, context.project_id)
        if item.run_id == run.id
    ]
    return {
        "run_id": run.id,
        "status": run.status.value,
        "engine": run.engine,
        "engine_version": run.engine_version,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "duration_s": run.duration_s,
        "exit_code": run.exit_code,
        "errors": run.errors[:8],
        "warnings": run.warnings[:8],
        "summary": run.summary,
        "artifact_count": len(artifacts),
    }


def _list_artifacts(context: AgentContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    run = _run_in_project(context, str(args.get("run_id") or context.run_id))
    limit = _bounded_limit(args.get("limit"), TOOL_SPECS["list_artifacts"], 20)
    artifacts = [
        item
        for item in repo.list_all(Artifact, context.project_id)
        if item.run_id == run.id
    ]
    return [
        {
            "artifact_id": item.id,
            "type": item.type,
            "name": Path(item.path).name if item.path else "",
            "sha256": item.sha256[:16],
            "created_at": item.created_at,
        }
        for item in artifacts[:limit]
    ]


TOOL_FUNCTIONS: dict[str, Callable[[AgentContext, dict[str, Any]], Any]] = {
    "project_overview": lambda context, args: _project_overview(context),
    "list_papers": _list_papers,
    "get_paper": _get_paper,
    "list_claims": _list_claims,
    "plan_context": _plan_context,
    "list_runs": _list_runs,
    "get_run": _get_run,
    "list_artifacts": _list_artifacts,
}


def execute_read_tool(
    context: AgentContext, name: str, args: dict[str, Any] | None = None
) -> ToolResult:
    """Execute one registered read tool with strict validation."""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return ToolResult(name, False, None, context.project_id, 0, "unknown tool")
    allowed_args = {
        "project_overview": set(),
        "list_papers": {"limit"},
        "get_paper": {"paper_id"},
        "list_claims": {"paper_id", "limit"},
        "plan_context": {"limit"},
        "list_runs": {"limit"},
        "get_run": {"run_id"},
        "list_artifacts": {"run_id", "limit"},
    }[name]
    provided = set((args or {}).keys())
    unexpected = provided - allowed_args
    if unexpected:
        return ToolResult(
            name, False, None, context.project_id, 0,
            f"unsupported arguments: {', '.join(sorted(unexpected))}",
        )
    try:
        payload = TOOL_FUNCTIONS[name](context, args or {})
    except (ValueError, TypeError) as exc:
        return ToolResult(name, False, None, context.project_id, 0, str(exc))
    encoded = repr(payload)
    result = ToolResult(
        name=name,
        ok=True,
        payload=payload,
        project_id=context.project_id,
        chars=len(encoded),
    )
    log.info(
        "agent read tool project=%s tool=%s args=%s chars=%d",
        context.project_id, name, args or {}, result.chars,
    )
    return result


def select_read_tools(
    context: AgentContext, question: str
) -> list[tuple[str, dict[str, Any]]]:
    """Choose a small, deterministic set of tools from the user's wording."""
    text = (question or "").lower()
    calls: list[tuple[str, dict[str, Any]]] = [("project_overview", {})]

    paper_terms = (
        "文献", "论文", "文章", "比较", "差别", "差异", "方法", "作者",
        "这篇", "当前论文", "当前文献", "paper", "literature", "compare",
    )
    claim_terms = ("证据", "引文", "出处", "页码", "条目", "claim", "quote")
    wants_paper = any(term in text for term in paper_terms)
    wants_claim = any(term in text for term in claim_terms)

    if wants_paper or wants_claim:
        calls.append(("list_papers", {"limit": 8}))
    if context.paper_id and (wants_paper or wants_claim):
        calls.append(("get_paper", {"paper_id": context.paper_id}))
    if wants_claim or (context.paper_id and wants_paper):
        calls.append((
            "list_claims",
            {"paper_id": context.paper_id, "limit": 20}
            if context.paper_id
            else {"limit": 12},
        ))
    unique: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for call in calls:
        if call[0] not in seen:
            seen.add(call[0])
            unique.append(call)
    return unique[:8]


def run_read_tools(context: AgentContext, question: str) -> list[dict[str, Any]]:
    """Run the selected tools and cap the total material sent to the model."""
    results: list[dict[str, Any]] = []
    used = 0
    for name, args in select_read_tools(context, question):
        result = execute_read_tool(context, name, args)
        if used + result.chars > MAX_TOOL_CHARS:
            result = ToolResult(
                name,
                False,
                None,
                context.project_id,
                0,
                "tool result omitted because the context budget was reached",
            )
        used += result.chars
        results.append(result.as_dict())
    return results

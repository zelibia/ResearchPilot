"""Bounded, project-scoped context for page-level agents.

The UI passes session state in, but this module never accepts a project or object
scope from the model. Every optional selection is validated against the current
project before it can enter the context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..storage.models import Project


@dataclass(frozen=True)
class AgentContext:
    project_id: str
    project_title: str
    screen: str = ""
    paper_id: str = ""
    paper_page: int | None = None
    run_id: str = ""
    plan_selection: str = ""

    @classmethod
    def from_ui_state(
        cls,
        project: Project,
        screen: str | None,
        state: Mapping[str, Any],
    ) -> "AgentContext":
        """Read only explicitly selected objects that belong to this project."""
        paper_id = ""
        if state.get("selected_paper_project") == project.id:
            candidate = str(state.get("selected_paper_id") or "")
            if candidate:
                paper_id = candidate

        paper_page: int | None = None
        workspace = state.get("pdf_workspace")
        if (
            paper_id
            and isinstance(workspace, dict)
            and workspace.get("project_id") == project.id
            and workspace.get("paper_id") == paper_id
        ):
            try:
                page = int(workspace.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            if page > 0:
                paper_page = page

        run_id = str(state.get(f"exp_run_{project.id}") or "")
        plan_selection = str(state.get(f"planner_pick_{project.id}") or "")
        return cls(
            project_id=project.id,
            project_title=project.title,
            screen=screen or "",
            paper_id=paper_id,
            paper_page=paper_page,
            run_id=run_id,
            plan_selection=plan_selection,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_title": self.project_title,
            "screen": self.screen,
            "paper_id": self.paper_id,
            "paper_page": self.paper_page,
            "run_id": self.run_id,
            "plan_selection": self.plan_selection,
        }

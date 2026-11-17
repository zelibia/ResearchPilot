"""Long-press drag handle for moving selected library papers between projects."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ..literature import library
from ..storage import repository as repo
from ..storage.models import Paper, Project


_HTML = """
<div class="rp-mover" role="group" aria-label="移动选中文献">
  <button class="rp-mover__grip" type="button"
          aria-label="长按并拖动到目标项目" title="长按并拖动到目标项目">
    <span aria-hidden="true">⠿</span>
  </button>
  <div class="rp-mover__targets" role="list" hidden></div>
</div>
"""

_CSS = """
:host,.rp-mover{height:100%;}
.rp-mover{position:relative;min-height:32px;}
.rp-mover__grip{
  width:34px;height:32px;border:1px dashed #C9C9C9;border-radius:4px;
  background:#FAFAFA;color:#666;cursor:grab;padding:0;font-size:17px;
  touch-action:none;user-select:none;
}
.rp-mover__grip:focus-visible{outline:2px solid #647A70;outline-offset:2px;}
.rp-mover.is-dragging .rp-mover__grip{
  cursor:grabbing;background:#EEF3F0;border-color:#647A70;color:#526E5C;
}
.rp-mover__targets{
  position:absolute;z-index:20;top:36px;left:0;width:250px;
  display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:8px;
  background:#fff;border:1px solid #DDD;border-radius:6px;
  box-shadow:0 8px 24px rgba(0,0,0,.14);
}
.rp-mover__targets[hidden]{display:none;}
.rp-mover__target{
  padding:7px 8px;border:1px solid #E3E3E3;border-radius:4px;
  background:#FAFAFA;color:#555;font-size:12px;line-height:1.3;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;
}
.rp-mover__target.is-hot{
  border-color:#647A70;background:#EEF3F0;color:#3F5549;font-weight:600;
}
"""

_JS = """
export default function(component) {
  const {data, setStateValue, parentElement} = component;
  const root = parentElement.querySelector(".rp-mover");
  const grip = root?.querySelector(".rp-mover__grip");
  const panel = root?.querySelector(".rp-mover__targets");
  if (!root || !grip || !panel) return;

  const config = data && typeof data === "object" ? data : {};
  const projects = Array.isArray(config.projects) ? config.projects : [];
  const paperIds = Array.isArray(config.paper_ids) ? config.paper_ids : [];
  const sourceProjectId = String(config.source_project_id || "");
  if (!projects.length || !paperIds.length) return;

  let timer = null;
  let dragging = false;
  let activePointer = null;
  let hot = null;

  function targetAt(clientX, clientY) {
    return [...panel.querySelectorAll(".rp-mover__target")].find((target) => {
      const rect = target.getBoundingClientRect();
      return clientX >= rect.left && clientX <= rect.right
        && clientY >= rect.top && clientY <= rect.bottom;
    }) || null;
  }

  function setHeight(height) {
    const host = parentElement?.host || parentElement;
    if (host) host.style.height = height + "px";
    window.parent.postMessage({
      isStreamlitMessage: true,
      type: "streamlit:setFrameHeight",
      height,
    }, "*");
  }

  function clearHot() {
    if (hot) hot.classList.remove("is-hot");
    hot = null;
  }

  function closeTargets() {
    dragging = false;
    root.classList.remove("is-dragging");
    panel.hidden = true;
    clearHot();
    setHeight(34);
  }

  function openTargets() {
    dragging = true;
    root.classList.add("is-dragging");
    panel.hidden = false;
    setHeight(Math.min(230, 58 + projects.length * 20));
  }

  for (const project of projects) {
    const target = document.createElement("button");
    target.type = "button";
    target.className = "rp-mover__target";
    target.dataset.projectId = String(project.id || "");
    target.textContent = String(project.title || "未命名项目");
    target.setAttribute("role", "listitem");
    target.addEventListener("click", () => {
      moveTo(target.dataset.projectId);
    });
    panel.appendChild(target);
  }

  function moveTo(projectId) {
    if (!projectId || projectId === sourceProjectId) return;
    setStateValue("move_request", {
      request_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      source_project_id: sourceProjectId,
      target_project_id: projectId,
      paper_ids: paperIds,
    });
    closeTargets();
  }

  grip.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    activePointer = event.pointerId;
    grip.setPointerCapture(event.pointerId);
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      openTargets();
    }, 420);
    event.preventDefault();
  });

  grip.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const target = targetAt(event.clientX, event.clientY);
    if (target === hot) return;
    clearHot();
    if (target) {
      hot = target;
      hot.classList.add("is-hot");
    }
  });

  function finish(event) {
    clearTimeout(timer);
    timer = null;
    if (!dragging) return;
    const target = targetAt(event.clientX, event.clientY);
    if (target) {
      moveTo(target.dataset.projectId);
    } else {
      closeTargets();
    }
    if (activePointer !== null && grip.hasPointerCapture?.(activePointer)) {
      grip.releasePointerCapture(activePointer);
    }
    activePointer = null;
  }

  grip.addEventListener("pointerup", finish);
  grip.addEventListener("pointercancel", () => {
    clearTimeout(timer);
    activePointer = null;
    closeTargets();
  });
  grip.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      panel.hidden ? openTargets() : closeTargets();
      event.preventDefault();
    } else if (event.key === "Escape") {
      closeTargets();
    }
  });

  setHeight(34);
  return () => {
    clearTimeout(timer);
  };
}
"""


def _component():
    return st.components.v2.component(
        "rp_library_mover",
        html=_HTML,
        css=_CSS,
        js=_JS,
    )


def _decode_request(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _apply_move(project_id: str, widget_key: str) -> None:
    request = _decode_request(st.session_state.get(widget_key))
    if not request or request.get("source_project_id") != project_id:
        return
    target_id = str(request.get("target_project_id") or "")
    target = repo.get(Project, target_id)
    if target is None or target.id == project_id:
        return
    paper_ids: list[str] = []
    for item in request.get("paper_ids") or []:
        paper = repo.get(Paper, str(item))
        if paper is not None and paper.project_id == project_id:
            paper_ids.append(paper.id)
    if not paper_ids:
        return
    result = library.move_to_project(project_id, paper_ids, target_id)
    st.session_state[widget_key] = {}
    st.session_state["selected_paper_id"] = ""
    st.session_state[f"_lit_rail_state_{project_id}"] = {
        "selected": set(),
        "epoch": int(
            (st.session_state.get(f"_lit_rail_state_{project_id}") or {}).get(
                "epoch", 0
            )
        ) + 1,
    }
    st.session_state["_library_notice"] = (
        "success",
        f"已移动 {result['moved']} 篇到「{target.title}」",
    )
    st.rerun()


def render(project: Project, selected_ids: list[str]) -> None:
    """Render a compact drag grip when at least one other project exists."""
    targets = [
        target for target in repo.list_all(Project)
        if target.id != project.id
    ]
    if not selected_ids or not targets:
        return
    widget_key = f"library_mover_{project.id}"
    _component()(
        key=widget_key,
        data={
            "source_project_id": project.id,
            "paper_ids": selected_ids,
            "projects": [
                {"id": target.id, "title": target.title}
                for target in targets
            ],
        },
        default={},
        height=34,
        on_move_request=lambda: _apply_move(project.id, widget_key),
    )

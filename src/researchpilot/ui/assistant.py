"""Project-scoped global assistant with floating and docked window modes.

The AI call path is intentionally unchanged: the panel still asks
``agent.assistant.reply`` with bounded project context and read-only tool
results. This module adds conversation grouping and browser-side window state
without changing the assistant's role or write boundaries.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import streamlit as st

from ..agent import assistant as assistant_agent
from ..agent import tools as tools_agent
from ..agent.agent_skills import LITERATURE_ASSISTANT_SKILLS
from ..agent.context import AgentContext
from ..literature import paths as paper_paths
from ..storage import repository as repo
from ..storage.models import (
    DiscussionMessage,
    ExperimentRun,
    MessageRole,
    Paper,
    Project,
)
from . import (
    assistant_conversations as conversations,
    assistant_window,
    browser_prefs,
    llm_settings,
    pdf_workspace,
    theme,
)

log = logging.getLogger(__name__)

STATE_KEY = "rp_assistant_state"
PANEL_HEIGHT = 280
AGENT_ROLE = conversations.AGENT_ROLE
MODE_FLOATING = "floating"
MODE_DOCKED = "docked"
MODE_LABELS = {MODE_FLOATING: "悬浮窗", MODE_DOCKED: "右侧停靠"}
DOCK_MIN = 320
DOCK_MAX = 600
FLOAT_MIN_WIDTH = 320
FLOAT_MIN_HEIGHT = 360


def _number(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _initial_state() -> dict[str, Any]:
    mode = str(browser_prefs.get("assistant_mode", MODE_FLOATING))
    if mode not in {MODE_FLOATING, MODE_DOCKED}:
        mode = MODE_FLOATING
    return {
        "open": bool(browser_prefs.get("assistant_open", False)),
        "pending": None,
        "handoff_screen": "",
        "mode": mode,
        "maximized": bool(browser_prefs.get("assistant_maximized", False)),
        "dock_width": _clamp(
            _number(browser_prefs.get("assistant_dock_width"), 420),
            DOCK_MIN,
            DOCK_MAX,
        ),
        "float_x": _number(browser_prefs.get("assistant_float_x"), -1),
        "float_y": _number(browser_prefs.get("assistant_float_y"), -1),
        "float_width": _clamp(
            _number(browser_prefs.get("assistant_float_width"), 420),
            FLOAT_MIN_WIDTH,
            900,
        ),
        "float_height": _clamp(
            _number(browser_prefs.get("assistant_float_height"), 560),
            FLOAT_MIN_HEIGHT,
            900,
        ),
        "conversation_by_project": {},
        "new_conversations": [],
        "new_context_by_conversation": {},
        "history_open": {},
        "delete_confirm": "",
        "window_seen": [],
    }


def _state() -> dict[str, Any]:
    state = st.session_state.get(STATE_KEY)
    if not isinstance(state, dict):
        state = _initial_state()
        st.session_state[STATE_KEY] = state
    defaults = _initial_state()
    for key, value in defaults.items():
        state.setdefault(key, value)
    _hydrate_preferences(state)
    return state


def _persist_window_state(state: dict[str, Any]) -> None:
    state["prefs_hydrated"] = True
    browser_prefs.set(
        assistant_mode=state["mode"],
        assistant_open=bool(state["open"]),
        assistant_maximized=bool(state["maximized"]),
        assistant_dock_width=int(state["dock_width"]),
        assistant_float_x=int(state["float_x"]),
        assistant_float_y=int(state["float_y"]),
        assistant_float_width=int(state["float_width"]),
        assistant_float_height=int(state["float_height"]),
    )


def _hydrate_preferences(state: dict[str, Any]) -> None:
    if state.get("prefs_hydrated"):
        return
    prefs = st.session_state.get("_browser_prefs")
    if not isinstance(prefs, dict):
        return
    keys = {
        "assistant_mode", "assistant_open", "assistant_maximized",
        "assistant_dock_width", "assistant_float_x", "assistant_float_y",
        "assistant_float_width", "assistant_float_height",
    }
    if not (keys & set(prefs)):
        return
    mode = str(prefs.get("assistant_mode", state["mode"]))
    if mode in {MODE_FLOATING, MODE_DOCKED}:
        state["mode"] = mode
    state["open"] = bool(prefs.get("assistant_open", state["open"]))
    state["maximized"] = bool(
        prefs.get("assistant_maximized", state.get("maximized", False))
    )
    state["dock_width"] = _clamp(
        _number(prefs.get("assistant_dock_width"), state["dock_width"]),
        DOCK_MIN,
        DOCK_MAX,
    )
    state["float_x"] = _number(
        prefs.get("assistant_float_x"), state["float_x"]
    )
    state["float_y"] = _number(
        prefs.get("assistant_float_y"), state["float_y"]
    )
    state["float_width"] = _clamp(
        _number(prefs.get("assistant_float_width"), state["float_width"]),
        FLOAT_MIN_WIDTH,
        900,
    )
    state["float_height"] = _clamp(
        _number(prefs.get("assistant_float_height"), state["float_height"]),
        FLOAT_MIN_HEIGHT,
        900,
    )
    state["prefs_hydrated"] = True


def _conversation_pref_key(project_id: str) -> str:
    return f"assistant_conversation_{project_id}"


def _history_pref_key(project_id: str) -> str:
    return f"assistant_history_{project_id}"


def _ensure_conversation(state: dict[str, Any], project: Project) -> str:
    mapping = state.setdefault("conversation_by_project", {})
    current = str(
        mapping.get(project.id)
        or browser_prefs.get(_conversation_pref_key(project.id), "")
    )
    records = conversations.list_conversations(project)
    ids = {item["id"] for item in records}
    new_ids = set(state.get("new_conversations") or [])
    if current and (current in ids or current in new_ids):
        mapping[project.id] = current
        return current
    current = records[0]["id"] if records else conversations.new_id()
    mapping[project.id] = current
    browser_prefs.set(**{_conversation_pref_key(project.id): current})
    if current not in ids:
        state.setdefault("new_conversations", []).append(current)
    return current


def _thread(state: dict[str, Any], project: Project | None) -> list[dict[str, Any]]:
    if project is None:
        return []
    return conversations.thread(project, _ensure_conversation(state, project))


def _context_payload(
    agent_context: AgentContext,
    *,
    conversation_id: str,
    tool_names: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "agent_role": AGENT_ROLE,
        "conversation_id": conversation_id,
        "screen": agent_context.screen,
        "paper_id": agent_context.paper_id,
        "paper_page": agent_context.paper_page,
        "run_id": agent_context.run_id,
    }
    if tool_names:
        payload["tool_names"] = tool_names
    return payload


def _save_user_message(
    project: Project,
    text: str,
    agent_context: AgentContext,
    conversation_id: str,
) -> DiscussionMessage:
    state = _state()
    if conversation_id in (state.get("new_conversations") or []):
        state["new_conversations"] = [
            item for item in state["new_conversations"]
            if item != conversation_id
        ]
    return repo.save(DiscussionMessage(
        id=repo.new_id("msg"),
        project_id=project.id,
        role=MessageRole.USER,
        content=text,
        context=_context_payload(
            agent_context, conversation_id=conversation_id
        ),
    ))


def _save_assistant_message(
    project: Project,
    result: assistant_agent.AssistantReply,
    agent_context: AgentContext,
    tool_names: list[str],
    conversation_id: str,
) -> DiscussionMessage:
    text = (
        result.text or "（模型返回了空内容）"
        if result.ok
        else f"这次没有拿到回答：{result.reason}"
    )
    return repo.save(DiscussionMessage(
        id=repo.new_id("msg"),
        project_id=project.id,
        role=MessageRole.ASSISTANT,
        content=text,
        context=_context_payload(
            agent_context,
            conversation_id=conversation_id,
            tool_names=tool_names,
        ),
        labels=["AI 讨论，未核实"],
        provider=result.provider,
        model=result.model,
        error="" if result.ok else (result.reason or "调用失败"),
    ))


def open_with_draft(
    project: Project,
    screen: str | None,
    draft_text: str,
) -> None:
    """Open the assistant from a page handoff without auto-sending."""
    state = _state()
    state["open"] = True
    state["pending"] = None
    state["handoff_screen"] = screen or ""
    _ensure_conversation(state, project)
    _persist_window_state(state)
    st.session_state[STATE_KEY] = state
    st.session_state["rp_assistant_input"] = draft_text


def _open_current_paper(project: Project, paper: Paper) -> None:
    st.session_state["selected_paper_id"] = paper.id
    st.session_state["selected_paper_project"] = project.id
    st.query_params["project"] = project.id
    st.query_params["view"] = "literature"
    if paper_paths.resolve_local_path(paper.local_path) is None:
        return
    page = 1
    workspace = st.session_state.get("pdf_workspace")
    if (
        isinstance(workspace, dict)
        and workspace.get("project_id") == project.id
        and workspace.get("paper_id") == paper.id
    ):
        page = int(workspace.get("page") or 1)
    pdf_workspace.open_document(project.id, paper.id, page, "")


def _toggle() -> None:
    state = _state()
    state["open"] = not state.get("open", False)
    _persist_window_state(state)
    st.session_state[STATE_KEY] = state


def _close() -> None:
    state = _state()
    state["open"] = False
    _persist_window_state(state)
    st.session_state[STATE_KEY] = state


def _toggle_history() -> None:
    state = _state()
    project = repo.get(Project, st.session_state.get("project_picker"))
    if project is None:
        return
    value = not bool(state.setdefault("history_open", {}).get(project.id, False))
    state["history_open"][project.id] = value
    browser_prefs.set(**{_history_pref_key(project.id): value})


def _new_conversation(project: Project, screen: str | None) -> None:
    state = _state()
    conversation_id = conversations.new_id()
    state.setdefault("conversation_by_project", {})[project.id] = conversation_id
    state.setdefault("new_conversations", []).append(conversation_id)
    state.setdefault("new_context_by_conversation", {})[
        conversation_id
    ] = _context_snapshot(project, screen)
    state["pending"] = None
    state["delete_confirm"] = ""
    st.session_state["rp_assistant_input"] = ""
    browser_prefs.set(**{_conversation_pref_key(project.id): conversation_id})


def _select_conversation(project: Project, conversation_id: str) -> None:
    state = _state()
    state.setdefault("conversation_by_project", {})[
        project.id
    ] = conversation_id
    state["pending"] = None
    state["delete_confirm"] = ""
    st.session_state.pop("rp_assistant_input", None)
    browser_prefs.set(**{_conversation_pref_key(project.id): conversation_id})


def _request_delete(conversation_id: str) -> None:
    _state()["delete_confirm"] = conversation_id


def _cancel_delete() -> None:
    _state()["delete_confirm"] = ""


def _confirm_delete(project: Project, conversation_id: str) -> None:
    state = _state()
    conversations.delete_conversation(project, conversation_id)
    state["delete_confirm"] = ""
    state["new_conversations"] = [
        item for item in state.get("new_conversations") or []
        if item != conversation_id
    ]
    if (
        state.get("conversation_by_project", {}).get(project.id)
        == conversation_id
    ):
        replacement = conversations.new_id()
        state["conversation_by_project"][project.id] = replacement
        state["new_conversations"].append(replacement)
        browser_prefs.set(**{
            _conversation_pref_key(project.id): replacement
        })


def _clear_all_conversations(project: Project) -> None:
    state = _state()
    conversations.delete_all(project)
    replacement = conversations.new_id()
    state["conversation_by_project"][project.id] = replacement
    state["new_conversations"] = [replacement]
    state["delete_confirm"] = ""
    browser_prefs.set(**{_conversation_pref_key(project.id): replacement})


def _toggle_maximize() -> None:
    state = _state()
    state["maximized"] = not bool(state.get("maximized"))
    _persist_window_state(state)


def _current_paper(project: Project | None) -> Paper | None:
    if project is None:
        return None
    paper_id = st.session_state.get("selected_paper_id")
    if not paper_id or st.session_state.get("selected_paper_project") != project.id:
        return None
    paper = repo_get(paper_id)
    return paper if paper is not None and paper.project_id == project.id else None


def repo_get(paper_id: str):
    return repo.get(Paper, paper_id)


def _context_snapshot(
    project: Project | None,
    screen: str | None = None,
) -> dict[str, Any]:
    if project is None:
        return {
            "project": "", "screen": "", "paper": "", "page": "",
            "run": "", "plan": "", "screen_code": "",
            "paper_id": "", "paper_page": None, "run_id": "", "plan_selection": "",
        }
    agent_context = AgentContext.from_ui_state(project, screen, st.session_state)
    paper = _current_paper(project)
    run = repo.get(ExperimentRun, agent_context.run_id) if agent_context.run_id else None
    return {
        "project": project.title,
        "screen": screen or literature_screen_label(),
        "paper": (
            f"{paper.title or '未命名文献'}"
            + (f"（{paper.year}）" if paper.year else "")
            if paper is not None else ""
        ),
        "page": (
            f"第 {agent_context.paper_page} 页"
            if agent_context.paper_page else ""
        ),
        "run": (
            f"{run.engine or '实验记录'}"
            f"（{run.status.value if hasattr(run.status, 'value') else run.status}）"
            if run is not None else ""
        ),
        "plan": "已选中当前方案" if agent_context.plan_selection else "",
        "screen_code": agent_context.screen,
        "paper_id": agent_context.paper_id,
        "paper_page": agent_context.paper_page,
        "run_id": agent_context.run_id,
        "plan_selection": agent_context.plan_selection,
    }


def literature_screen_label() -> str:
    return "文献阅读"


def _context_label(project: Project | None, screen: str | None) -> str:
    snapshot = _context_snapshot(project, screen)
    parts = [
        snapshot["project"], snapshot["screen"], snapshot["paper"],
        snapshot["page"], snapshot["run"], snapshot["plan"],
    ]
    return "　·　".join(item for item in parts if item)


def _attach_current_context(project: Project, screen: str | None) -> None:
    snapshot = _context_snapshot(project, screen)
    lines = ["[当前页面上下文]"]
    for label, key in (
        ("项目", "project"),
        ("工作台", "screen"),
        ("当前文献", "paper"),
        ("阅读位置", "page"),
        ("当前方案", "plan"),
        ("当前实验", "run"),
    ):
        if snapshot.get(key):
            lines.append(f"{label}：{snapshot[key]}")
    block = "\n".join(lines)
    current = str(st.session_state.get("rp_assistant_input") or "").strip()
    if block in current:
        return
    prefix = "请基于以下当前页面上下文回答：" if not current else current
    st.session_state["rp_assistant_input"] = f"{prefix}\n\n{block}".strip()


def _answer(project: Project | None, screen: str | None = None) -> None:
    state = _state()
    if not state.get("pending"):
        return
    state["pending"] = None
    conversation_id = (
        _ensure_conversation(state, project) if project is not None else ""
    )
    history = conversations.thread(project, conversation_id) if project else []
    if project is not None:
        agent_context = AgentContext.from_ui_state(
            project, screen, st.session_state
        )
        question = history[-1]["text"] if history else ""
        tool_results = tools_agent.run_read_tools(agent_context, question)
        context = assistant_agent.build_context(
            project,
            selected_paper=_current_paper(project),
            screen=screen,
            agent_context=agent_context,
            tool_results=tool_results,
        )
    else:
        agent_context = None
        tool_results = []
        context = "当前还没有选中项目。"
    with st.spinner("正在生成回答…"):
        result = assistant_agent.reply(
            history, context, client=llm_settings.client()
        )
    if project is not None and agent_context is not None:
        tool_names = [str(item.get("tool") or "") for item in tool_results]
        _save_assistant_message(
            project, result, agent_context, tool_names, conversation_id
        )
    st.rerun()


def _bubble(item: dict) -> None:
    role = item.get("role", "assistant")
    with st.chat_message("user" if role == "user" else "assistant"):
        st.markdown(item.get("text", ""))
        meta = item.get("meta") or ""
        if meta:
            st.caption(meta)


@st.dialog("连接 AI 模型")
def _connect_dialog() -> None:
    llm_settings.render(namespace="assistant_dialog")


def _history_drawer(project: Project) -> None:
    state = _state()
    records = conversations.list_conversations(project)
    current = _ensure_conversation(state, project)
    st.markdown("**对话历史**")
    if not records:
        theme.markdown(
            '<div class="rp-assistant-empty">还没有历史对话。发送第一条问题后会出现在这里。</div>'
        )
    with st.container(height=330, border=False, key="rp_assistant_history"):
        for record in records:
            selected = record["id"] == current
            row = st.columns([5, 1], vertical_alignment="center")
            with row[0]:
                st.button(
                    record["summary"],
                    key=f"rp_assistant_conv_{record['id']}",
                    type="primary" if selected else "tertiary",
                    width="stretch",
                    on_click=_select_conversation,
                    args=(project, record["id"]),
                    help=(
                        f"{conversations.created_label(record['created_at'])}"
                        f" · {record['page_type']}"
                    ),
                )
                st.caption(
                    f"{conversations.created_label(record['created_at'])}"
                    f" · {record['page_type']}"
                )
            with row[1]:
                st.button(
                    "",
                    key=f"rp_assistant_delete_{record['id']}",
                    icon=":material/delete:",
                    help="删除这条对话",
                    on_click=_request_delete,
                    args=(record["id"],),
                )
            if state.get("delete_confirm") == record["id"]:
                confirm = st.columns([1, 1])
                confirm[0].button(
                    "确认删除",
                    key=f"rp_assistant_delete_confirm_{record['id']}",
                    type="primary",
                    width="stretch",
                    on_click=_confirm_delete,
                    args=(project, record["id"]),
                )
                confirm[1].button(
                    "取消",
                    key=f"rp_assistant_delete_cancel_{record['id']}",
                    width="stretch",
                    on_click=_cancel_delete,
                )
    st.button(
        "清空全部历史",
        key="rp_assistant_clear",
        type="tertiary",
        width="stretch",
        on_click=_clear_all_conversations,
        args=(project,),
    )


def _window_event() -> None:
    state = _state()
    component_state = st.session_state.get("rp_assistant_window")
    raw = (
        component_state.get("assistant_window_event")
        if isinstance(component_state, dict)
        else getattr(component_state, "assistant_window_event", None)
    )
    if isinstance(raw, str):
        try:
            action = json.loads(raw)
        except (TypeError, ValueError):
            return
    elif isinstance(raw, dict):
        action = raw
    else:
        return
    request_id = str(action.get("request_id") or "")
    seen = state.setdefault("window_seen", [])
    if not request_id or request_id in seen:
        return
    seen.append(request_id)
    state["window_seen"] = seen[-30:]
    kind = str(action.get("type") or "")
    if kind == "resize_dock":
        state["dock_width"] = _clamp(
            _number(action.get("dock_width"), state["dock_width"]),
            DOCK_MIN,
            DOCK_MAX,
        )
    elif kind == "move_float":
        state["float_x"] = max(0, _number(action.get("float_x"), state["float_x"]))
        state["float_y"] = max(0, _number(action.get("float_y"), state["float_y"]))
    elif kind == "resize_float":
        state["float_x"] = max(0, _number(action.get("float_x"), state["float_x"]))
        state["float_y"] = max(0, _number(action.get("float_y"), state["float_y"]))
        state["float_width"] = max(
            FLOAT_MIN_WIDTH,
            _number(action.get("float_width"), state["float_width"]),
        )
        state["float_height"] = max(
            FLOAT_MIN_HEIGHT,
            _number(action.get("float_height"), state["float_height"]),
        )
    else:
        return
    _persist_window_state(state)
    st.rerun()


def _window_style(state: dict[str, Any]) -> None:
    position = ""
    if int(state["float_x"]) >= 0:
        position += f"--rp-assistant-x:{int(state['float_x'])}px;"
    if int(state["float_y"]) >= 0:
        position += f"--rp-assistant-y:{int(state['float_y'])}px;"
    theme.markdown(
        "<style>:root{"
        f"--rp-assistant-dock-width:{int(state['dock_width'])}px;"
        f"--rp-assistant-width:{int(state['float_width'])}px;"
        f"--rp-assistant-height:{int(state['float_height'])}px;"
        f"{position}"
        "}</style>"
    )


def _panel(project: Project | None, screen: str | None = None) -> None:
    state = _state()
    mode_widget = st.session_state.get("rp_assistant_mode")
    remembered_mode = next(
        (
            mode for mode, label in MODE_LABELS.items()
            if label == mode_widget
        ),
        "",
    )
    if remembered_mode and remembered_mode != state["mode"]:
        state["mode"] = remembered_mode
        state["maximized"] = False
        _persist_window_state(state)
    connected = llm_settings.connected()
    conversation_id = _ensure_conversation(state, project) if project else ""
    history = _thread(state, project)
    records = conversations.list_conversations(project) if project else []
    history_map = state.setdefault("history_open", {})
    if project is not None and project.id not in history_map:
        history_map[project.id] = bool(
            browser_prefs.get(_history_pref_key(project.id), False)
        )
    history_open = bool(history_map.get(project.id, False) if project else False)
    _window_style(state)

    with st.container(key="rp_assistant_panel"):
        theme.markdown(
            '<span class="rp-assistant-dock-marker"></span>'
            if state["mode"] == MODE_DOCKED
            else '<span class="rp-assistant-float-marker"></span>'
            + (
                '<span class="rp-assistant-maximized-marker"></span>'
                if state.get("maximized") else ""
            )
        )
        with st.container(key="rp_assistant_toolbar"):
            toolbar = st.columns([3.2, 1.25, 1, 1], vertical_alignment="center")
            toolbar[0].markdown("**AI 助手**")
            toolbar[1].button(
                "新建对话",
                key="rp_assistant_new_chat",
                icon=":material/add:",
                width="stretch",
                on_click=_new_conversation,
                args=(project, screen),
                disabled=project is None,
            )
            toolbar[2].button(
                "历史",
                key="rp_assistant_history_toggle",
                icon=":material/history:",
                type="primary" if history_open else "secondary",
                width="stretch",
                on_click=_toggle_history,
                disabled=project is None,
            )
            toolbar[3].button(
                "收起" if state["mode"] == MODE_DOCKED else "最小化",
                key="rp_assistant_close",
                icon=":material/close:",
                width="stretch",
                on_click=_close,
            )

        mode_cols = st.columns([2.4, 1.2, 3], vertical_alignment="center")
        selected_label = mode_cols[0].segmented_control(
            "显示模式",
            list(MODE_LABELS.values()),
            default=MODE_LABELS[state["mode"]],
            key="rp_assistant_mode",
            label_visibility="collapsed",
        )
        selected_mode = next(
            (
                mode for mode, label in MODE_LABELS.items()
                if label == selected_label
            ),
            state["mode"],
        )
        if selected_mode != state["mode"]:
            state["mode"] = selected_mode
            state["maximized"] = False
            _persist_window_state(state)
        if state["mode"] == MODE_FLOATING:
            mode_cols[1].button(
                "还原" if state.get("maximized") else "最大化",
                key="rp_assistant_maximize",
                icon=":material/fullscreen:",
                width="stretch",
                on_click=_toggle_maximize,
            )
        mode_cols[2].caption(
            "右侧停靠会为主页面留出空间；拖动窗口边缘可调整尺寸。"
        )

        theme.markdown(
            theme.chip_row([(_context_label(project, screen), "evidence")])
        )
        if state.get("handoff_screen"):
            st.caption("已从论文阅读页带入当前论文；请检查问题后再发送。")
        if not history:
            st.caption("新对话已带入当前页面上下文。")
        paper = _current_paper(project)
        if paper is not None:
            st.button(
                "打开当前论文",
                key="rp_assistant_open_paper",
                icon=":material/menu_book:",
                width="stretch",
                on_click=_open_current_paper,
                args=(project, paper),
            )

        if not connected:
            st.caption("未连接模型。助手只基于本项目的真实记录回答，也不会改动任何数据。")
            if st.button(
                "连接 AI 模型",
                key="rp_assistant_connect",
                type="primary",
                icon=":material/key:",
                width="stretch",
            ):
                _connect_dialog()

        if state.get("pending"):
            _answer(project, screen)

        if history_open and project is not None:
            drawer, body = st.columns([1.35, 3.65], gap="small")
            with drawer:
                _history_drawer(project)
            with body:
                _conversation_body(
                    project, screen, history, connected
                )
        else:
            _conversation_body(project, screen, history, connected)
            if project is not None:
                with st.container(key="rp_assistant_clear_compat"):
                    st.button(
                        "清空全部历史",
                        key="rp_assistant_clear",
                        type="tertiary",
                        on_click=_clear_all_conversations,
                        args=(project,),
                    )

        assistant_window.render(
            {
                "mode": state["mode"],
                "maximized": bool(state.get("maximized")),
                "dock_width": int(state["dock_width"]),
                "float_x": int(state["float_x"]),
                "float_y": int(state["float_y"]),
                "float_width": int(state["float_width"]),
                "float_height": int(state["float_height"]),
            },
            _window_event,
        )


def _conversation_body(
    project: Project | None,
    screen: str | None,
    history: list[dict[str, Any]],
    connected: bool,
) -> None:
    state = _state()
    with st.container(height=PANEL_HEIGHT, border=False, key="rp_assistant_log"):
        if not history:
            st.caption(
                "可以问：项目现状、文献差异、证据缺口、原文页码或阅读路径。"
            )
            with st.expander(
                f"可用能力（{len(LITERATURE_ASSISTANT_SKILLS)} 项）",
                expanded=False,
            ):
                st.markdown(
                    "\n".join(
                        f"- **{skill.title}**：{skill.use_when}"
                        for skill in LITERATURE_ASSISTANT_SKILLS
                    )
                )
        for item in history[-16:]:
            _bubble(item)

    with st.form(key="rp_assistant_form", clear_on_submit=True, border=False):
        question = st.text_area(
            "提问",
            key="rp_assistant_input",
            placeholder="例如：这两篇文献的方法有什么差别？",
            label_visibility="collapsed",
            height=64,
        )
        sent = st.form_submit_button(
            "发送",
            type="primary",
            disabled=not connected or project is None,
            width="stretch",
        )
    if project is not None:
        st.button(
            "附上当前页面上下文",
            key="rp_assistant_attach_context",
            icon=":material/add_link:",
            type="tertiary",
            width="stretch",
            on_click=_attach_current_context,
            args=(project, screen),
        )
    if sent and question.strip() and project is not None:
        state["pending"] = True
        state["handoff_screen"] = ""
        conversation_id = _ensure_conversation(state, project)
        anchor = state.get("new_context_by_conversation", {}).get(
            conversation_id
        )
        agent_context = AgentContext.from_ui_state(
            project, screen, st.session_state
        )
        if not history and isinstance(anchor, dict) and anchor.get("project"):
            agent_context = AgentContext(
                project_id=project.id,
                project_title=project.title,
                screen=str(anchor.get("screen_code") or screen or ""),
                paper_id=str(anchor.get("paper_id") or ""),
                paper_page=(
                    int(anchor.get("paper_page"))
                    if anchor.get("paper_page") else None
                ),
                run_id=str(anchor.get("run_id") or ""),
                plan_selection=str(anchor.get("plan_selection") or ""),
            )
        _save_user_message(
            project, question.strip(), agent_context, conversation_id
        )
        st.rerun()


def render(project: Project | None, screen: str | None = None) -> None:
    """Mount the persistent ball or the selected assistant window mode."""
    state = _state()
    if project is not None:
        _ensure_conversation(state, project)
    with st.container(key="rp_assistant_ball"):
        st.button(
            "AI",
            key="rp_assistant_toggle",
            on_click=_toggle,
            help="打开 / 收起 AI 助手（回答基于本项目的真实记录）",
        )
    if state.get("open"):
        _panel(project, screen)

"""统一布局框架：可折叠、可拖动宽度的索引栏 + 自适应主内容区。

文献阅读工作区共用这一套：

- 浏览：左侧项目文献库 + 右侧文献详情；
- 阅读：左侧 PDF 原文 + 右侧 AI 解读。

为什么不是 CSS 或 DOM 技巧：
横向 `st.container` + 固定像素宽子容器 + `width="stretch"` 子容器，是
Streamlit 官方 API 能表达的"左栏定宽、右栏吃掉剩余宽度"。官方没有提供
可拖动的分隔线，所以拖动交互放在一个 Custom Components v2 组件里：
组件只读写自己的 localStorage 与自己的 DOM，不去选择或改写 Streamlit 的
内部元素，因此不会随 Streamlit 版本改类名而碎掉。

拖动的粒度：
按下记录起点，移动时只更新组件自己的读数提示，**松开才提交新宽度**。
提交会触发一次 Streamlit 重跑，页面按新宽度重排。之所以不做"拖动过程中
持续重跑"，是因为每次重跑都会重建组件元素，正在进行的拖拽会被打断，
反而更糟——宁可松开后一次到位。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from . import browser_prefs, theme

# 分隔条的宽度（像素）。太窄不好点，太宽浪费横向空间。
HANDLE_WIDTH = 14

_HANDLE_HTML = """
<div class="rp-handle" role="separator" aria-orientation="vertical"
     tabindex="0" aria-label="拖动调整索引栏宽度">
  <div class="rp-grip"></div>
  <div class="rp-readout" hidden></div>
</div>
"""

_HANDLE_CSS = """
:host, .rp-handle {
  height: 100%;
}
.rp-handle {
  position: relative;
  width: 100%;
  min-height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: col-resize;
  border-radius: 6px;
  background: transparent;
  transition: background 120ms ease;
}
.rp-handle:hover, .rp-handle:focus-visible, .rp-handle.rp-dragging {
  background: rgba(127, 127, 127, 0.18);
  outline: none;
}
.rp-handle .rp-grip {
  width: 3px;
  height: 42px;
  border-radius: 2px;
  background: rgba(127, 127, 127, 0.45);
}
.rp-handle.rp-dragging .rp-grip {
  background: rgba(127, 127, 127, 0.8);
}
.rp-handle .rp-readout {
  position: absolute;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 1.5;
  white-space: nowrap;
  color: var(--st-text-color, #31333f);
  background: var(--st-secondary-background-color, rgba(127, 127, 127, 0.16));
}
"""

_HANDLE_JS = """
export default function (component) {
  const { data, setStateValue, parentElement } = component;

  let config = data;
  if (typeof config === "string") {
    try {
      config = JSON.parse(config);
    } catch (err) {
      config = null;
    }
  }
  config = config && typeof config === "object" ? config : {};

  const min = Number(config.min) || 240;
  const max = Number(config.max) || 720;
  const startWidth = Number(config.width) || 380;
  const direction = config.direction === "right" ? "right" : "left";

  const handle = parentElement.querySelector(".rp-handle");
  const readout = parentElement.querySelector(".rp-readout");
  if (!handle) {
    return;
  }

  let dragging = false;
  let startX = 0;
  let current = startWidth;

  function clamp(value) {
    return Math.round(Math.min(max, Math.max(min, value)));
  }

  function show(value) {
    if (!readout) return;
    readout.hidden = false;
    readout.textContent = value + " px";
  }

  function hide() {
    if (!readout) return;
    readout.hidden = true;
  }

  function commit(value) {
    const next = clamp(value);
    if (next !== startWidth) {
      setStateValue("width", next);
    }
  }

  function widthFromPointer(clientX) {
    const delta = direction === "right"
      ? startX - clientX
      : clientX - startX;
    return clamp(startWidth + delta);
  }

  handle.addEventListener("pointerdown", (event) => {
    dragging = true;
    startX = event.clientX;
    current = startWidth;
    handle.classList.add("rp-dragging");
    handle.setPointerCapture(event.pointerId);
    show(current);
    event.preventDefault();
  });

  handle.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    current = widthFromPointer(event.clientX);
    show(current);
  });

  function finish(event) {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("rp-dragging");
    hide();
    // 以松开那一刻的指针位置为准。浏览器会合并 pointermove 事件，
    // 只信最后一次 move 会丢掉最后一段位移（快速拖动时尤其明显）。
    if (event && typeof event.clientX === "number") {
      current = widthFromPointer(event.clientX);
    }
    commit(current);
    event.preventDefault();
  }

  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);

  // 键盘可达：左右方向键各挪 20 像素，回车 / 双击回到默认宽度。
  handle.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") {
      commit(direction === "right" ? startWidth + 20 : startWidth - 20);
    } else if (event.key === "ArrowRight") {
      commit(direction === "right" ? startWidth - 20 : startWidth + 20);
    } else if (event.key === "Enter" || event.key === " ") {
      commit(Number(config.default) || startWidth);
    } else {
      return;
    }
    event.preventDefault();
  });

  handle.addEventListener("dblclick", (event) => {
    commit(Number(config.default) || startWidth);
    event.preventDefault();
  });
}
"""

def _handle_component():
    """注册并返回分隔条组件。每次 run 注册一次，理由见 browser_prefs._component。"""
    return st.components.v2.component(
        "rp_split_handle", html=_HANDLE_HTML, css=_HANDLE_CSS, js=_HANDLE_JS
    )


def clamp_width(value: Any, default: int | None = None) -> int:
    """把任意输入收敛成合法宽度，避免拖动把组件弄到不可用。"""
    fallback = default or theme.INDEX_WIDTH_DEFAULT
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = fallback
    return max(theme.INDEX_WIDTH_MIN, min(theme.INDEX_WIDTH_MAX, number))


def _width_state_key(workbench: str) -> str:
    return f"_rp_index_width_{workbench}"


def _collapsed_key(workbench: str) -> str:
    return f"{workbench}_index_collapsed"


def _width_pref_key(workbench: str) -> str:
    return f"{workbench}_index_width"


def _commit_width(workbench: str, raw_state: Any) -> None:
    """组件报告新宽度后的落地：本会话立刻生效，同时进浏览器偏好。

    组件状态在 session_state 里是一段 JSON（`{"width": 420}`），不是裸数字。
    """
    width = None
    if isinstance(raw_state, str):
        import json

        try:
            width = json.loads(raw_state).get("width")
        except (ValueError, AttributeError):
            width = None
    elif isinstance(raw_state, dict):
        width = raw_state.get("width")

    if width is None:
        return
    width = clamp_width(width)
    st.session_state[_width_state_key(workbench)] = width
    browser_prefs.set(**{_width_pref_key(workbench): width})


def _on_width_change(workbench: str) -> None:
    _commit_width(workbench, st.session_state.get(f"rp_split_{workbench}"))


def current_width(workbench: str, default: int | None = None) -> int:
    """索引栏当前宽度：本会话的即时值 -> 浏览器偏好 -> 默认值。"""
    fallback = default or theme.INDEX_WIDTH_DEFAULT
    pending = st.session_state.get(_width_state_key(workbench))
    if pending is not None:
        return clamp_width(pending, fallback)
    return clamp_width(browser_prefs.get(_width_pref_key(workbench), fallback), fallback)


def is_collapsed(workbench: str) -> bool:
    """索引是否收起。窄窗口默认收起，但用户手动展开过就不再自动收起。"""
    if bool(st.session_state.get(f"_rp_index_pinned_{workbench}", False)):
        return bool(browser_prefs.get(_collapsed_key(workbench), False))
    if browser_prefs.viewport_is_narrow():
        return True
    return bool(browser_prefs.get(_collapsed_key(workbench), False))


def set_collapsed(workbench: str, collapsed: bool) -> None:
    st.session_state[f"_rp_index_pinned_{workbench}"] = True
    browser_prefs.set(**{_collapsed_key(workbench): bool(collapsed)})


def collapsed_rail_button(
    workbench: str,
    project_id: str,
    label: str = "索引",
    *,
    index_side: str = "left",
) -> None:
    """窄条里的展开按钮：索引收起后留一条可点击的边缘。"""
    st.button(
        "",
        key=f"expand_{workbench}_rail_{project_id}",
        icon=(
            ":material/keyboard_double_arrow_left:"
            if index_side == "right"
            else ":material/keyboard_double_arrow_right:"
        ),
        help=f"展开{label}",
        on_click=lambda: set_collapsed(workbench, False),
    )


def _mount_handle(
    workbench: str,
    width: int,
    height: int,
    default: int | None = None,
    *,
    index_side: str = "left",
) -> None:
    _handle_component()(
        key=f"rp_split_{workbench}",
        data={
            "width": width,
            "min": theme.INDEX_WIDTH_MIN,
            "max": theme.INDEX_WIDTH_MAX,
            "default": default or theme.INDEX_WIDTH_DEFAULT,
            "direction": index_side,
        },
        default={"width": width},
        width=HANDLE_WIDTH,
        height=height,
        on_width_change=lambda: _on_width_change(workbench),
    )


def render(
    *,
    workbench: str,
    index_title: str,
    index_render: Callable[[], None],
    detail_render: Callable[[], None],
    index_height: int = 560,
    index_caption: str = "",
    index_width_default: int | None = None,
    index_side: str = "left",
    index_controls_label: str = "索引",
    collapsed_rail_render: Callable[[], None] | None = None,
    show_header: bool = True,
) -> None:
    """渲染工作区：索引与主内容按 ``index_side`` 排列。

    `workbench` 是短标识（literature / pdf），用作偏好键与控件键的前缀。

    ``show_header=False``：不渲染"索引标题 + 收起"这一行。阅读页自己有一行固定
    顶部工具（标题 + PDF 控制），再叠一行标题就成了三层，也会把固定高度算错。
    """
    collapsed = is_collapsed(workbench)
    if index_side not in {"left", "right"}:
        index_side = "left"

    if show_header:
        with st.container(horizontal=True, vertical_alignment="center"):
            if collapsed:
                st.button(
                    f"展开{index_controls_label}",
                    icon=":material/unfold_more:",
                    key=f"expand_index_{workbench}",
                    help="展开后可以拖动分隔线调整宽度",
                    on_click=lambda: set_collapsed(workbench, False),
                )
                st.caption(f"{index_title}（已收起）")
            else:
                st.caption(index_title)

    if collapsed:
        if collapsed_rail_render is not None:
            with st.container(horizontal=True, wrap=False, gap="small"):
                if index_side == "right":
                    with st.container(width="stretch", key="rp_detail"):
                        detail_render()
                    with st.container(width=34, key="rp_collapsed_rail"):
                        collapsed_rail_render()
                else:
                    with st.container(width=34, key="rp_collapsed_rail"):
                        collapsed_rail_render()
                    with st.container(width="stretch", key="rp_detail"):
                        detail_render()
            return
        detail_render()
        return

    width = current_width(workbench, index_width_default)

    def _index() -> None:
        with st.container(width=width, key="rp_index"):
            if show_header:
                head = st.columns([5, 2])
                head[0].markdown(f"**{index_title}**")
                if head[1].button(
                    "收起",
                    icon=":material/unfold_less:",
                    key=f"collapse_index_{workbench}",
                    help=f"收起{index_controls_label}，主内容区占满整宽",
                    on_click=lambda: set_collapsed(workbench, True),
                ):
                    pass
            if index_caption:
                st.caption(index_caption)
            index_render()

    def _handle() -> None:
        with st.container(width=HANDLE_WIDTH):
            _mount_handle(
                workbench, width, index_height, index_width_default,
                index_side=index_side,
            )

    def _detail() -> None:
        with st.container(width="stretch", key="rp_detail"):
            detail_render()

    with st.container(horizontal=True, wrap=False, gap="small"):
        if index_side == "right":
            _detail()
            _handle()
            _index()
        else:
            _index()
            _handle()
            _detail()

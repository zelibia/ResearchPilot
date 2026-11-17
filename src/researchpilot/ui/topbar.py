"""全站顶部导航兼容层：当前项目 / 文献阅读位置。

主应用目前由侧栏承担固定位置提示，本模块保留给后续独立阅读壳体复用。

与阅读页的关系：阅读页自己还有两行 PDF 工具栏。顶栏在**上面**，
阅读工具栏用 `top: var(--rp-topbar-h)` 贴在它下面，两者高度一起进
`--rp-read-offset`，所以阅读区高度不会被算错。

为什么 sticky 而不是 fixed：fixed 会让正文从第一行就被遮住，还要给正文补
padding 去躲它；sticky 仍然占位，正文起始位置天然正确，滚动时又钉在视口顶部。
"""

from __future__ import annotations

import streamlit as st

from ..storage.models import Project
from . import theme

WORKBENCH_ORDER = ("文献阅读",)

# 为什么不用 position: sticky：
# Streamlit 把页面包在若干层 flex 容器里（stMain > stMainBlockContainer >
# stVerticalBlock > stLayoutWrapper），实测在这些层里 sticky 完全不生效
# （滚动 900px 后顶栏跟着走到 -700 以后）。所以这里用 fixed + 占位，并用一个
# 小组件量出主区左边界（侧栏宽度会变），写到 CSS 变量上。
_ANCHOR_JS = """
export default function (component) {
  const root = document.documentElement;
  function apply() {
    const main = document.querySelector('section[data-testid="stMain"]');
    if (!main) return;
    const rect = main.getBoundingClientRect();
    root.style.setProperty("--rp-topbar-left", Math.max(0, Math.round(rect.left)) + "px");
    root.style.setProperty("--rp-topbar-width", Math.round(rect.width) + "px");
    // 顶栏默认是隐藏的：量到主区边界之前它是 fixed 且 width 兜底值，
    // 一旦先渲染出来就会盖住侧栏、把侧栏的点击全吃掉（实测过）。
    root.dataset.rpTopbarReady = "1";
  }
  apply();
  const main = document.querySelector('section[data-testid="stMain"]');
  if (main && window.ResizeObserver) {
    const observer = new ResizeObserver(apply);
    observer.observe(main);
    window.addEventListener("resize", apply);
    return () => { observer.disconnect(); window.removeEventListener("resize", apply); };
  }
  window.addEventListener("resize", apply);
  return () => window.removeEventListener("resize", apply);
}
"""


def _anchor_component():
    return st.components.v2.component("rp_topbar_anchor", js=_ANCHOR_JS)


def _switch(label: str) -> None:
    """快捷切换工作台。走 on_click 回调：回调在任何控件实例化之前执行，
    这时改写侧栏 radio 的 key 是安全的（写在工作台渲染中途会撞 Streamlit 的限制）。"""
    st.session_state["workbench_picker"] = label


def render(project: Project | None, workbench: str, route: str = "") -> None:
    """渲染固定顶栏。`workbench` 是当前工作台标签，`route` 是地址栏里的视图代码。"""
    if project is None:
        return
    try:
        _anchor_component()(key="rp_topbar_anchor", data={"pane": "topbar"}, height=0)
    except Exception:  # noqa: BLE001 - 量不到左边界只是顶栏会从左边缘开始，不致命
        pass
    with st.container(key="rp_topbar"):
        cols = st.columns([5, 4, 1], vertical_alignment="center")
        cols[0].markdown(
            f'<div class="rp-topbar__title">{theme.esc(project.title)}</div>'
            f'<div class="rp-topbar__sub">当前工作台：{theme.esc(workbench)}'
            + (f'　·　{theme.esc(project.domain)}' if project.domain else "")
            + "</div>",
            unsafe_allow_html=True,
        )
        with cols[1]:
            links = st.columns(len(WORKBENCH_ORDER))
            for index, label in enumerate(WORKBENCH_ORDER):
                current = label == workbench
                links[index].button(
                    label,
                    key=f"rp_topbar_nav_{index}",
                    type="primary" if current else "secondary",
                    disabled=current,
                    width="stretch",
                    on_click=_switch,
                    args=(label,),
                )
        cols[2].caption(f"?view={route}" if route else "")
    # 顶栏是 fixed（脱离文档流），这里补一个等高占位，正文起始位置才不会跑到它下面
    st.markdown('<div class="rp-topbar-spacer" aria-hidden="true"></div>', unsafe_allow_html=True)

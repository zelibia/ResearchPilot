"""记住滚动位置的小组件（左右分栏各自独立）。

为什么需要它：Streamlit 每次 rerun 都会重建 DOM，**滚动容器的 scrollTop 会回到 0**。
用户编辑一条备忘、勾一个步骤，右侧就可能跳回顶部——这在双栏阅读里很烦人。

做法（刻意保持低脆弱）：

- 组件挂载时，从**自己所在的最近一个可滚动祖先**读/写 scrollTop；
  它只碰自己所在的那个容器（Streamlit 为 `st.container(height=...)` 建的那个），
  不去选择或改写页面上其它元素，也不依赖内部类名；
- 位置存在浏览器 localStorage（按 pane 名隔离），跨 rerun 与刷新都有效；
- 滚动事件节流 150ms 写一次，不触发任何 Python 回调（不产生 rerun）；
- 找不到可滚动祖先时静默不做任何事，页面照常工作。
"""

from __future__ import annotations

import streamlit as st

STORAGE_PREFIX = "researchpilot.scroll.v1"

_JS = """
export default function (component) {
  const { data, parentElement } = component;
  const KEY = "%(prefix)s." + String((data && data.pane) || "pane");

  function scroller() {
    let node = parentElement;
    while (node && node !== document.body && node instanceof Element) {
      const s = window.getComputedStyle(node);
      if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight) return node;
      node = node.parentElement;
    }
    return null;
  }

  const box = scroller();
  if (!box) return;

  let saved = 0;
  try { saved = Number(window.localStorage.getItem(KEY)) || 0; } catch (err) { saved = 0; }

  const restore = () => { if (saved > 0 && Math.abs(box.scrollTop - saved) > 2) box.scrollTop = saved; };
  restore();
  requestAnimationFrame(restore);

  let timer = null;
  box.addEventListener("scroll", () => {
    if (timer) return;
    timer = window.setTimeout(() => {
      timer = null;
      try { window.localStorage.setItem(KEY, String(Math.round(box.scrollTop))); } catch (err) {}
    }, 150);
  }, { passive: true });
}
""" % {"prefix": STORAGE_PREFIX}


def _component():
    """注册并返回组件（v2 的注册表挂在 Runtime 上，每次 run 都要注册一次）。"""
    return st.components.v2.component("rp_scroll_keeper", js=_JS)


def mark(pane: str) -> None:
    """在滚动容器**内部**调用一次，给这个容器装上"记住位置"的能力。"""
    try:
        _component()(key=f"rp_scroll_{pane}", data={"pane": pane}, height=0)
    except Exception:  # noqa: BLE001 - 记不住位置只是体验退化，不能让页面打不开
        return


def pane_key(pane: str) -> str:
    """给测试用：localStorage 的键名。"""
    return f"{STORAGE_PREFIX}.{pane}"

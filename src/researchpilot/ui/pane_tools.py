"""阅读页用的小组件：滚动定位、实时页码、滚到底继续加载。

三个都是 Custom Components v2 小件，理由各自不同：

- `mount_scroll_to`：点「定位原文」后要让**左栏自己**滚到目标页。滚动位置是纯浏览器
  状态，用 Python 去算只能靠 rerun，反而会把用户的位置冲掉；
- `mount_page_readout`：工具条上的「第 X / N 页」要跟着滚动实时变。它由组件自己
  更新文本，**不触发 rerun**（否则每翻一页就重发几十张页图）；
- `mount_load_sentinel`：长文档按需渲染。滚到窗口底部时回调一次，Python 把窗口
  扩大一批再重跑——每次扩展一个 token，避免"到底 → 重跑 → 又到底"的循环。

组件只读写自己所在的 DOM 与 localStorage，不选择、不修改 Streamlit 内部元素。
在 AppTest 里 JS 不执行，这三个都退化成"什么都不做"，页面照常（还有手动按钮兜底）。
"""

from __future__ import annotations

import logging

import streamlit as st

log = logging.getLogger(__name__)

STORAGE_PREFIX = "researchpilot.pane.v1"

_SCROLL_JS = """
export default function (component) {
  const { data, parentElement } = component;
  const config = typeof data === "string" ? JSON.parse(data) : (data || {});
  const pane = String(config.pane || "");
  const anchor = String(config.anchor || "");
  const token = String(config.token || "");
  if (!pane || !anchor || !token) return;

  const box = paneBox(pane);
  if (!box) return;
  const key = "%(prefix)s.scrollto." + pane;
  let seen = "";
  try { seen = window.localStorage.getItem(key) || ""; } catch (err) { seen = ""; }
  if (seen === token) return;          // 同一个请求只滚一次，rerun 不再把人拽回去
  try { window.localStorage.setItem(key, token); } catch (err) {}

  const target = box.querySelector('[data-rp-anchor="' + anchor + '"]');
  if (!target) return;
  const top = target.getBoundingClientRect().top
            - box.getBoundingClientRect().top + box.scrollTop - 8;
  box.scrollTop = Math.max(0, top);
}

// 组件可能被放在 shadow root 里，`parentElement` 往上走会断在 shadow 边界上；
// 所以这里直接按 Streamlit 给的 key 类名找滚动容器（与"实时页码"同一套查找）。
function paneBox(pane) {
  const nodes = Array.from(document.querySelectorAll(".st-key-rp_" + pane + "_scroll"));
  let best = null;
  for (const item of nodes) {
    if (item.clientHeight <= 0) continue;
    if (!best || item.scrollHeight > best.scrollHeight) best = item;
  }
  return best;
}
""" % {"prefix": STORAGE_PREFIX}

_READOUT_JS = """
export default function (component) {
  const { data, parentElement } = component;
  const config = typeof data === "string" ? JSON.parse(data) : (data || {});
  const pane = String(config.pane || "");
  const total = Number(config.total || 0);
  const node = parentElement.querySelector(".rp-readout") || parentElement;
  if (!pane || !total) return;

  function paneBox() {
    const nodes = Array.from(document.querySelectorAll(".st-key-rp_" + pane + "_scroll"));
    let best = null;
    for (const item of nodes) {
      if (item.scrollHeight >= item.clientHeight && item.clientHeight > 0) best = item;
      if (!best && item.clientHeight > 0) best = item;
    }
    return best;
  }

  let box = paneBox();
  if (!box) return;
  // 用**页容器**而不是零高度锚点来量位置：锚点没有高度，Streamlit 的隐藏副本
  // 会让它算错；页容器有真实尺寸，过滤掉不可见的副本后顺序即页序。
  function pageNodes() {
    const found = [];
    for (const el of box.querySelectorAll("[class*='st-key-rp_page_']")) {
      const match = String(el.className).match(/st-key-rp_page_(\\d+)/);
      if (!match) continue;
      const rect = el.getBoundingClientRect();
      if (rect.height <= 0) continue;          // 隐藏副本
      found.push({ el: el, n: Number(match[1]), top: rect.top });
    }
    found.sort((a, b) => a.n - b.n);
    return found;
  }
  const pages = pageNodes();

  function currentPage() {
    // 每次现算：Streamlit 是流式渲染的，组件挂载时页容器可能只到了一部分，
    // 缓存下来就会永远停在那几页上（实测过"滚到底还显示第 4 页"）。
    const pages = pageNodes();
    if (!pages.length) return 0;
    // 用"相对面板可视区"的偏移比较：图片是异步加载的，用 offsetTop 会在
    // 布局稳定前算错；getBoundingClientRect 每次都是当前真实位置。
    const rect = box.getBoundingClientRect();
    const probe = Math.min(120, box.clientHeight * 0.25);
    let page = pages[0].n;
    for (const item of pages) {
      const top = item.el.getBoundingClientRect().top - rect.top;
      if (top <= probe) page = item.n;
      else break;                       // 页序排列，后面的只会更靠下
    }
    return page;
  }

  let last = 0;
  function paint() {
    const page = currentPage();
    if (!page || page === last) return;
    last = page;
    node.textContent = "第 " + page + " / " + total + " 页";
  }
  paint();
  let timer = null;
  box.addEventListener("scroll", () => {
    if (timer) return;
    timer = window.setTimeout(() => { timer = null; paint(); }, 120);
  }, { passive: true });
  // 页图是异步加载的，加载完成后版面会变；只靠 scroll 事件会留下过期读数。
  const ticker = window.setInterval(paint, 800);
  requestAnimationFrame(paint);
  return () => window.clearInterval(ticker);
}
""" % {"prefix": STORAGE_PREFIX}

_LOAD_JS = """
export default function (component) {
  const { data, setStateValue, parentElement } = component;
  const config = typeof data === "string" ? JSON.parse(data) : (data || {});
  const pane = String(config.pane || "");
  const token = String(config.token || "");
  const seq = Number(config.seq || 0);
  if (!pane || !token) return;

  const box = paneBox(pane);
  if (!box) return;
  const key = "%(prefix)s.loaded." + pane;

  // 用"离底部还有多远"判断，不依赖 IntersectionObserver 的 root 语义
  function maybeAsk() {
    if (box.scrollTop + box.clientHeight < box.scrollHeight - 480) return;
    let seen = "";
    try { seen = window.localStorage.getItem(key) || ""; } catch (err) { seen = ""; }
    if (seen === token) return;          // 这一批已经请求过，别重复触发
    try { window.localStorage.setItem(key, token); } catch (err) {}
    setStateValue("hit", seq);
  }

  let timer = null;
  box.addEventListener("scroll", () => {
    if (timer) return;
    timer = window.setTimeout(() => { timer = null; maybeAsk(); }, 150);
  }, { passive: true });
  requestAnimationFrame(maybeAsk);
  return () => { if (timer) window.clearTimeout(timer); };
}

function paneBox(pane) {
  const nodes = Array.from(document.querySelectorAll(".st-key-rp_" + pane + "_scroll"));
  let best = null;
  for (const item of nodes) {
    if (item.clientHeight <= 0) continue;
    if (!best || item.scrollHeight > best.scrollHeight) best = item;
  }
  return best;
}
""" % {"prefix": STORAGE_PREFIX}

_READOUT_HTML = '<div class="rp-readout rp-num"></div>'

# 阅读区高度按**实测**算：视口高度减去固定导航与两行工具栏的真实底边，
# 而不是写死一个"看起来对"的偏移量（1280×800 下写死的值会差出上百像素）。
_FIT_JS = """
export default function (component) {
  const root = document.documentElement;
  function apply() {
    const header = document.querySelector(".st-key-rp_read_header");
    if (!header) return;
    const bottom = header.getBoundingClientRect().bottom;
    const vh = window.innerHeight || root.clientHeight;
    // 再留 64px：block 容器自身还有 8px 底部留白与若干行间距（实测差 45px）。
    const height = Math.max(240, Math.round(vh - bottom - 64));
    root.style.setProperty("--rp-read-h", height + "px");
  }
  apply();
  requestAnimationFrame(apply);
  window.addEventListener("resize", apply);
  let observer = null;
  const header = document.querySelector(".st-key-rp_read_header");
  if (header && window.ResizeObserver) {
    observer = new ResizeObserver(apply);
    observer.observe(header);
  }
  return () => {
    window.removeEventListener("resize", apply);
    if (observer) observer.disconnect();
  };
}
"""


def _scroll_component():
    return st.components.v2.component("rp_pane_scroll", js=_SCROLL_JS)


def _readout_component():
    # isolate_styles=False：这个组件要在**亮色 DOM** 里显示文字，才能吃到主题字体
    # 与颜色（也方便验收脚本读到它）。它只渲染一行文字，不需要样式隔离。
    return st.components.v2.component(
        "rp_pane_readout", html=_READOUT_HTML, js=_READOUT_JS, isolate_styles=False
    )


def _load_component():
    return st.components.v2.component("rp_pane_load", js=_LOAD_JS)


def _fit_component():
    return st.components.v2.component("rp_pane_fit", js=_FIT_JS)


def mount_reading_height() -> None:
    """按实测把阅读区高度写到 `--rp-read-h`（视口 − 固定导航与工具栏的真实高度）。"""
    try:
        _fit_component()(key="rp_pane_fit", data={"pane": "fit"}, height=0)
    except Exception:  # noqa: BLE001 - 量不到就退回 CSS 里的 calc 兜底
        log.exception("阅读区高度组件挂载失败")
        return


def mount_scroll_to(pane: str, anchor: str, token: str) -> None:
    """把 pane 的滚动容器滚到 `data-rp-anchor=<anchor>` 的位置（每个 token 一次）。"""
    if not anchor:
        return
    try:
        _scroll_component()(
            key=f"rp_scroll_to_{pane}", data={"pane": pane, "anchor": anchor, "token": token},
            height=0,
        )
    except Exception:  # noqa: BLE001 - 滚不过去只是体验退化，不能让页面打不开
        return


def mount_page_readout(pane: str, total: int, *, key: str = "") -> None:
    """渲染一个跟随滚动更新的「第 X / N 页」读数（纯前端，不触发 rerun）。"""
    try:
        _readout_component()(
            key=key or f"rp_readout_{pane}", data={"pane": pane, "total": int(total)},
            height=24, width="stretch",
        )
    except Exception:  # noqa: BLE001 - 记一条日志再降级，别让阅读页打不开
        log.exception("页码读数组件挂载失败")
        return


def mount_load_sentinel(pane: str, token: str, seq: int, on_hit) -> None:
    """在面板末尾放一个哨兵：滚到附近时回调一次（由调用方决定怎么扩展）。"""
    try:
        _load_component()(
            key=f"rp_load_{pane}", data={"pane": pane, "token": token, "seq": int(seq)},
            default={"hit": -1}, height=0, on_hit_change=on_hit,
        )
    except Exception:  # noqa: BLE001
        log.exception("自动加载哨兵挂载失败")
        return

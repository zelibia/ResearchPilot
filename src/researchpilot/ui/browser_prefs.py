"""浏览器级偏好：这台浏览器上次打开的项目、文献与索引宽度。

为什么需要这一层：
项目 ID 存在 URL 查询参数里，刷新和分享都能恢复。但用户直接打开不带参数的
地址（新标签页、书签、localhost:8501）时 URL 里什么都没有，需要一个
"这台浏览器上次用的是什么"的兜底值。

这个兜底必须落在浏览器本地：放到服务端就成了所有用户共享的"最近项目"，
别人一操作就会改掉你的默认值。不同端口（8501 / 8502）是不同源，
localStorage 天然隔离，测试实例与正式实例不会互相串。

实现方式：
用 Streamlit 官方 Custom Components v2 写一个只读写 localStorage 的小组件，
它不碰 Streamlit 自己的 DOM，只读写自己那一个键。

组件不可用时（无头自检、浏览器禁用 JS）自动退回 session_state 里的缓存值：
功能降级为"仅本会话有效"，不会报错、不会崩。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

# localStorage 的键名。改版本号等于让旧偏好失效，所以只在结构不兼容时动它。
STORAGE_KEY = "researchpilot.prefs.v1"

_PREFS_JS = """
export default function (component) {
  const { data, setStateValue } = component;
  const STORAGE_KEY = "%(storage_key)s";
  const NARROW_BREAKPOINT = 900;

  function read() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (err) {
      return {};
    }
  }

  function write(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } catch (err) {
      /* 隐私模式或存储被禁用：静默降级为本会话有效 */
    }
  }

  let payload = data;
  if (typeof payload === "string") {
    try {
      payload = JSON.parse(payload);
    } catch (err) {
      payload = null;
    }
  }
  const incoming =
    payload && typeof payload === "object" && payload.set && typeof payload.set === "object"
      ? payload.set
      : null;

  const stored = read();
  const merged = incoming ? Object.assign({}, stored, incoming) : stored;
  if (incoming) {
    write(merged);
  }

  // 组件每次 rerun 都会重新挂载。只在结果真的变化时才上报，否则会形成
  // "挂载 -> 触发重跑 -> 再挂载" 的循环。
  const serialized = JSON.stringify(merged);
  if (window.__rpPrefsSent !== serialized) {
    window.__rpPrefsSent = serialized;
    setStateValue("prefs", merged);
  }

  // 窄窗口上报：只在跨越阈值时发送，避免每次改窗口大小都触发一次重跑。
  const syncNarrow = () => {
    const narrow = window.innerWidth <= NARROW_BREAKPOINT;
    if (window.__rpNarrow !== narrow) {
      window.__rpNarrow = narrow;
      setStateValue("narrow", narrow);
    }
  };
  syncNarrow();
  window.addEventListener("resize", syncNarrow);
  return () => {
    window.removeEventListener("resize", syncNarrow);
  };
}
""" % {"storage_key": STORAGE_KEY}


_STATE_KEY = "_browser_prefs"
_PENDING_KEY = "_browser_prefs_pending"
_NARROW_KEY = "_browser_viewport_narrow"


def _component():
    """注册并返回偏好组件（每次 run 都要注册一次）。

    组件注册表挂在 Runtime 上，不是在 import 时固定下来的：无头自检每建一个
    AppTest 实例都会换一个 Runtime，浏览器每开一个会话也是。若只在模块 import
    时注册，第二个会话就会报 "Component is not registered"。同一进程里重复
    注册**内容完全相同**的定义是幂等的，不会打告警，所以这里每次 run 注册一次。
    """
    return st.components.v2.component("rp_browser_prefs", js=_PREFS_JS)


def load() -> dict[str, Any]:
    """挂载组件并把浏览器里的偏好并入本会话缓存，返回合并后的偏好。

    每个 rerun 都要调用一次（组件通道要保持存活）；页面逻辑只从返回值里读。
    """
    pending = st.session_state.pop(_PENDING_KEY, None)

    stored: dict[str, Any] = {}
    narrow = False
    try:
        result = _component()(
            key="rp_browser_prefs",
            data={"set": pending} if pending else None,
            default={"prefs": {}, "narrow": False},
            height=0,
            on_prefs_change=lambda: None,
            on_narrow_change=lambda: None,
        )
    except Exception:  # noqa: BLE001 - 偏好只是体验优化，坏了也不能让页面打不开
        result = None

    if result is not None:
        raw = getattr(result, "prefs", None)
        if isinstance(raw, dict):
            stored = {key: value for key, value in raw.items() if value is not None}
        narrow = bool(getattr(result, "narrow", False))

    merged = dict(st.session_state.get(_STATE_KEY, {}))
    merged.update(stored)
    st.session_state[_STATE_KEY] = merged
    st.session_state[_NARROW_KEY] = narrow
    return merged


def get(name: str, default: Any = None) -> Any:
    """读一个偏好值。组件没跑起来时读到的是本会话里已写入的值。"""
    return st.session_state.get(_STATE_KEY, {}).get(name, default)


def set(**values: Any) -> None:
    """写偏好值：本会话立刻生效，同时排队在下次挂载时写进浏览器。"""
    current = dict(st.session_state.get(_STATE_KEY, {}))
    current.update(values)
    st.session_state[_STATE_KEY] = current

    pending = dict(st.session_state.get(_PENDING_KEY, {}))
    pending.update(values)
    st.session_state[_PENDING_KEY] = pending


def viewport_is_narrow() -> bool:
    """浏览器是否是窄窗口。组件没跑起来时一律当作宽窗口。"""
    return bool(st.session_state.get(_NARROW_KEY, False))

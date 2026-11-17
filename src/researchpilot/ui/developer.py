"""开发者诊断模式开关。

界面规范（docs/00_项目启动/界面规范_v1.0.md）第 1 条要求用户界面不出现开发内容。
这些内容不是删掉，而是收进一个显式开启的通道：环境变量 `RESEARCHPILOT_DEV_MODE=1`
或 URL 参数 `?dev=1`。开关由 app.py 解析后写进 session_state，页面从这里读。
"""

from __future__ import annotations

import streamlit as st


def enabled() -> bool:
    """当前会话是否处于开发者诊断模式。默认关闭。"""
    return bool(st.session_state.get("_dev_mode", False))

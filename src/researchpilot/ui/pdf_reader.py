"""证据入口：点「PDF · 第 N 页」把左侧 PDF 面板定位到那一页（P1-01）。

设计取舍：

- 只影响左侧 PDF 面板，右侧内容的滚动位置与上下文不动；
- **不跨项目展示**：请求里带 project_id，渲染前核对当前项目的论文集合，
  只有"这篇文献确实属于当前项目且在本机有 PDF"才渲染；
- **不伪造高亮**：命中与否由 `literature/pdf_view` 决定（逐字/部分/未命中/无文本层），
  界面只如实转述，包括"只跳页不高亮"的情况；
- 跳转走 `on_click` 回调：回调里改控件状态是安全的，直接在渲染中途改会撞上 Streamlit 的限制。
"""

from __future__ import annotations

import streamlit as st

from ..literature import paths as paper_paths
from ..storage.models import Paper, Project
from . import pdf_workspace, theme

def goto(project_id: str, paper_id: str, page: int, quote: str = "", *, verified: bool = True) -> None:
    """点证据：把左侧 PDF 面板切到这篇文献的对应页——**不离开当前工作台**。

    只在 on_click 回调里调用（回调里改控件状态是安全的）。
    右侧的滚动位置与上下文因此原样保留，这就是"左右联动"而不是"跳页"的区别。
    """
    pdf_workspace.open_document(project_id, paper_id, page, quote, verified=verified)
    # 文献阅读页的选中项也跟着走，切回去时对得上
    st.session_state["selected_paper_id"] = paper_id
    st.session_state["selected_paper_project"] = project_id


def evidence_buttons(rows: list[dict], *, project_id: str, key_prefix: str) -> None:
    """把一组证据渲染成「PDF · 第 N 页」按钮。

    rows: [{paper_id, page, quote, verified, label}]
    - 有本地 PDF 的文献：按钮 → 跨工作台打开阅读器；
    - 没有本地 PDF 的候选文献：按钮禁用，并说明原因（不打开虚假阅读页）。

    控件 key 必须唯一定位到"这一条证据"：同一页里同一篇论文的同一页会有多条证据
    （成果报告一节就有 13 条），只用 (前缀, 下标) 会撞 key 并让页面直接崩。
    调用方用 `row["key"]` 给稳定标识（claim_id / 区块序号）；没给才退回下标。
    """
    papers = {item.id: item for item in _papers_of(project_id)}
    seen: set[str] = set()
    for index, row in enumerate(rows):
        paper = papers.get(row.get("paper_id", ""))
        page = int(row.get("page") or 0)
        has_file = bool(paper and paper_paths.resolve_local_path(paper.local_path))
        label = row.get("label") or (f"PDF · 第 {page} 页" if page else "出处未定位")
        key = f"{key_prefix}_{row.get('key') or index}"
        # 兜底：标识仍然重复时补后缀，绝不让整页崩在重复 key 上
        if key in seen:
            suffix = 2
            while f"{key}#{suffix}" in seen:
                suffix += 1
            key = f"{key}#{suffix}"
        seen.add(key)
        if has_file:
            st.button(
                label, key=key, help="打开原文并尝试高亮这条引文",
                on_click=goto,
                args=(project_id, paper.id, page, row.get("quote", "")),
                kwargs={"verified": bool(row.get("verified", True))},
            )
        else:
            st.button(
                label, key=key, disabled=True,
                help="这篇候选文献只有元数据、本机没有 PDF，无法打开原文",
            )


def _papers_of(project_id: str) -> list[Paper]:
    from ..storage import repository as repo   # 局部导入，避免与 repository 形成环

    return repo.list_all(Paper, project_id)

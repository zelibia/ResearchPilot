"""文献工作台内的「论文大纲」目录：摘要、章节、结论、关联文献（P1）。

三条纪律：

1. **大纲只能来自原文**。优先用 PDF 自带目录书签；没有书签时用 ``fulltext``
   从文本层排版（字号/加粗）读出标题，并明确标注"来自排版、非书签"；
   既没有书签也读不到标题时，退化成"每 4 页一组"，不编造章节号。
2. **点大纲只动左边的 PDF**。右栏的滚动位置、编辑状态、未提交输入都不受影响。
3. **关联文献必须带真实关系类型**（引用/被引用/扩展/对比/替代/不同方法/人工关联），
   并显示这条关系是否已核实；没有本地 PDF 的论文按钮保持禁用并说明原因。
"""

from __future__ import annotations

import re

import streamlit as st

from ..literature import fulltext
from ..literature import paths as paper_paths
from ..literature.text_quality import sanitize_for_display
from ..storage import repository as repo
from ..storage.models import (
    Paper,
    PaperRelation,
    Project,
    RelationSource,
    RelationType,
    ReviewStatus,
)
from . import pdf_workspace

RELATION_LABEL: dict[RelationType, str] = {
    RelationType.CITES: "引用",
    RelationType.EXTENDS: "扩展",
    RelationType.COMPARES: "对比",
    RelationType.REPLACES: "替代",
    RelationType.DIVERGES: "提出不同方法",
}

SOURCE_LABEL: dict[RelationSource, str] = {
    RelationSource.OPENALEX: "OpenAlex 引用记录",
    RelationSource.USER: "人工关联",
    RelationSource.LLM: "AI 提议",
}

REVIEW_LABEL: dict[ReviewStatus, str] = {
    ReviewStatus.PENDING: "待裁决",
    ReviewStatus.CONFIRMED: "已确认",
    ReviewStatus.REJECTED: "已驳回",
}

# 摘要 / 结论只按**原文里真实存在的章节标题**定位；识别不到就不显示这一行，
# 不拿"前几页"或"最后几页"冒充。
ABSTRACT_RE = re.compile(r"(摘要|abstract)", re.IGNORECASE)
CONCLUSION_RE = re.compile(r"(结论|结语|讨论|conclusions?|discussions?)", re.IGNORECASE)


@st.cache_data(ttl=300, show_spinner=False)
def _outline(paper_id: str, local_path: str, title: str) -> dict:
    """章节大纲（缓存 5 分钟）。只读 PDF 与按页正文，不写任何东西。"""
    paper = repo.get(Paper, paper_id)
    if paper is None:
        return {"ok": False, "reason": "这篇文献已不在库里", "sections": []}
    index = fulltext.build_index(paper)
    if index is None:
        return {
            "ok": False,
            "reason": "没有可用的按页正文，无法生成大纲（重新上传同一份 PDF 可补齐）",
            "sections": [],
        }
    return {
        "ok": True,
        "reason": "",
        "source": index.section_source,
        "page_count": index.page_count,
        "notes": list(index.notes),
        "sections": [
            {"index": section.index, "title": section.title, "level": section.level,
             "start": section.start_page, "end": section.end_page,
             "toc": section.is_confirmed_toc}
            for section in index.sections
        ],
    }


def _current_section(outline: dict, page: int) -> dict | None:
    found = None
    for section in outline.get("sections", []):
        if section["start"] <= page:
            found = section
    return found


def _relation_rows(project: Project, paper_id: str) -> list[dict]:
    """与这篇论文有关、且**有真实来源**的关系。被驳回的不展示。"""
    rows: list[dict] = []
    relations = repo.list_paper_relations(project.id, paper_id=paper_id)
    for relation in relations:
        if relation.review_status is ReviewStatus.REJECTED:
            continue
        other_id = (relation.to_paper_id if relation.from_paper_id == paper_id
                    else relation.from_paper_id)
        other = repo.get(Paper, other_id)
        if other is None or other.project_id != project.id:
            continue      # 跨项目文献一律不出现
        outgoing = relation.from_paper_id == paper_id
        label = RELATION_LABEL.get(relation.relation_type, "相关")
        if relation.relation_type is RelationType.CITES:
            # 「引用」本身已经说明方向，不要再拼一次类型名（会出现"本文引用引用"）
            text = "本文引用" if outgoing else "被引用于"
        else:
            text = f"{'本文对其' if outgoing else '对方对本文'}{label}"
        rows.append({
            "paper_id": other_id,
            "title": sanitize_for_display(other.title) or "（标题未识别）",
            "year": other.year or "",
            "label": text,
            "source": SOURCE_LABEL.get(relation.source, "来源未知"),
            "verified": bool(relation.verified),
            "review": REVIEW_LABEL.get(relation.review_status, ""),
            "has_pdf": paper_paths.resolve_local_path(other.local_path) is not None,
        })
    rows.sort(key=lambda row: (not row["verified"], row["title"]))
    return rows


def _outline_row(paper: Paper, project: Project, section: dict, active: bool) -> None:
    """一行章节。高亮行单独一个容器 key，CSS 只给这一行加竖条与底色。"""
    key = "rp_outline_active" if active else f"rp_outline_row_{section['index']}"
    with st.container(key=key):
        if st.button(
            section["title"][:56],
            key=f"outline_{paper.id}_{section['index']}",
            width="stretch",
            help=f"第 {section['start']}–{section['end']} 页"
                 + ("（来自 PDF 书签）" if section["toc"] else "（按原文标题排版提取）"),
        ):
            # 只更新左侧 PDF 的阅读请求；右栏的滚动与编辑状态不动
            pdf_workspace.open_document(project.id, paper.id, page=section["start"])
            st.rerun()


def render_panel(project: Project, paper: Paper) -> None:
    """文献工作台里的可折叠目录：摘要 / 章节 / 结论 / 关联文献。

    为什么放在工作台里而不是全局侧栏：侧栏是"换项目、换工作台"的地方，
    论文大纲是"当前这篇论文怎么看"的内容导航——两者混在一起时，
    900px 高的窗口里大纲会把全局导航挤出视口（改造前实测如此）。
    """
    state = _outline(paper.id, paper.local_path or "", paper.title or "")
    current = pdf_workspace.current(project.id)
    if not current or current.get("paper_id") != paper.id:
        current = None
    page = int((current or {}).get("page") or 1) if current else 0
    now = _current_section(state, page) if (current and state.get("ok")) else None

    title = "论文大纲"
    if now:
        title = f"论文大纲 · 第 {page} 页 · {now['title'][:28]}"
    # 带 key：点章节会触发一次重跑（打开左侧原文），没有 key 时折叠区会重新收起，
    # 用户刚要看的"当前章节"就没了。
    with st.expander(
        title, icon=":material/list_alt:", expanded=False, key=f"rp_outline_{paper.id}"
    ):
        if not state.get("ok"):
            st.caption(state.get("reason") or "暂无大纲")
        else:
            sections = state.get("sections", [])
            source = {
                "toc": "章节来自 PDF 目录书签",
                "heading": "章节按原文标题排版提取",
                "pages": "无可识别章节，按每 4 页分组",
            }.get(state.get("source"), "")
            st.markdown(
                f'<div class="rp-meta">共 {state.get("page_count", 0)} 页'
                + (f"　·　{source}" if source else "")
                + "</div>",
                unsafe_allow_html=True,
            )

            jump = []
            for pattern, label in ((ABSTRACT_RE, "摘要"), (CONCLUSION_RE, "结论")):
                hit = next((s for s in sections if pattern.search(s["title"] or "")), None)
                if hit is not None:
                    jump.append((label, hit))
            if jump:
                columns = st.columns(len(jump))
                for index, (label, section) in enumerate(jump):
                    if columns[index].button(
                        f"跳到{label}", key=f"jump_{label}_{paper.id}", width="stretch",
                        help=f"第 {section['start']} 页",
                    ):
                        pdf_workspace.open_document(
                            project.id, paper.id, page=section["start"]
                        )
                        st.rerun()

            for section in sections:
                _outline_row(
                    paper, project, section,
                    active=now is not None and section["index"] == now["index"],
                )

        rows = _relation_rows(project, paper.id)
        with st.expander(f"关联文献（{len(rows)}）", expanded=False):
            if not rows:
                st.caption("还没有经过来源核实的关联文献。")
            for row in rows:
                mark = "已核实" if row["verified"] else row["review"] or "未核实"
                st.markdown(f"**{row['title'][:52]}**")
                st.markdown(
                    f'<div class="rp-meta">{row["label"]}　·　{row["source"]}　·　{mark}'
                    + ("" if row["has_pdf"] else "　·　无可用原文")
                    + "</div>",
                    unsafe_allow_html=True,
                )
                if row["has_pdf"]:
                    if st.button(
                        "打开原文", key=f"rel_open_{paper.id}_{row['paper_id']}",
                        icon=":material/open_in_new:", width="stretch",
                    ):
                        pdf_workspace.open_document(project.id, row["paper_id"], page=1)
                        st.rerun()

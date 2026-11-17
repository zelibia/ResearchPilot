"""文献库面板：跨项目查看、加入本项目、删除文献。

三件事，都在这一处：

1. **看**：库里有哪些文献、分别被哪些项目用着、本地文件还在不在；
2. **用**：把库里已有的文献直接加入当前项目（不重新上传、不重新解析）；
3. **删**：把文献从当前项目移除，或者从所有项目彻底删除（可选同时删本地文件）。

界面规范 v1.0 的两条在这里同样适用：不出现内部编号与绝对路径；
删除必须先报清"会连带删掉什么"，再让用户确认。
"""

from __future__ import annotations

import logging

import streamlit as st

from ..literature import library
from ..literature import links
from ..literature import repair
from ..literature.text_quality import sanitize_for_display
from ..storage import repository as repo
from ..storage.models import Paper, Project
from . import theme

log = logging.getLogger(__name__)

LIST_PAGE = 20


def _titles() -> dict[str, str]:
    return library.project_titles()


def _short(text: str, limit: int = 66) -> str:
    # 库里可能有解析乱码的文献（原始值带控制字符）：显示的文本一律先清洗，
    # 原始值留在记录里不动——与目录、详情用的是同一套规则。
    text = sanitize_for_display(text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _entry_title(entry: library.LibraryEntry) -> str:
    """库列表里的标题：走与目录一致的显示规则（乱码时标"待修正"）。"""
    return _short(repair.display_title(entry.paper), 72)


def _entry_caption(entry: library.LibraryEntry, titles: dict[str, str], current: str) -> str:
    """一行归属说明：年份 · 作者 · 被哪些项目用着。"""
    bits = [entry.paper.year or "年份未知"]
    if entry.paper.authors:
        bits.append(_short(entry.paper.authors, 28))
    others = [titles.get(pid, "其它项目") for pid in entry.project_ids if pid != current]
    if others:
        bits.append("也在：" + "、".join(others))
    if not entry.file_available:
        bits.append("无原文")
    if entry.paper.doi:
        bits.append("DOI " + _short(entry.paper.doi, 34))
    return "　·　".join(bits)


def _delete_control(entry: library.LibraryEntry, key: str, *, current_has: Paper | None) -> None:
    """删除入口。删除范围与后果先写清楚，再要求勾选确认。"""
    with st.popover("删除…", width="stretch"):
        st.markdown(f"**{_entry_title(entry)}**")
        st.caption(
            f"这篇文献在 {len(entry.members)} 个项目里有记录，"
            f"关联关系与 AI 条目会一并清理。"
        )
        if current_has is not None:
            if st.button("只从本项目移除", key=f"{key}_rm_project"):
                result = library.remove_paper(current_has, delete_file=False)
                st.session_state["_library_notice"] = (
                    "info",
                    f"已从本项目移除：关系 {result['relations']} 条、AI 条目 "
                    f"{result['claims']} 条一并清理；其余项目不受影响。",
                )
                st.rerun()
            st.divider()
        keep_file = st.checkbox(
            "同时删除本地 PDF 文件", value=False, key=f"{key}_purge_file",
            help="仅当没有别的项目还在用这份文件时才真的删除。",
        )
        confirm = st.checkbox(
            "我确认从**所有项目**删除这篇文献", key=f"{key}_purge_ok"
        )
        if st.button(
            "彻底删除", key=f"{key}_purge_btn", type="primary",
            disabled=not confirm,
        ):
            result = library.purge_entry(entry, delete_file=bool(keep_file))
            note = (
                f"已删除 {result['records']} 条记录（关系 {result['relations']}、"
                f"AI 条目 {result['claims']}）。"
            )
            if result.get("file_deleted"):
                note += "本地 PDF 与按页正文已删除。"
            elif keep_file:
                note += "本地文件仍有其它记录在用，未删除。"
            st.session_state["_library_notice"] = ("info", note)
            st.rerun()


def _row(entry: library.LibraryEntry, project: Project, titles: dict[str, str], index: int) -> None:
    current_has = entry.in_project(project.id)
    key = f"lib_{index}_{entry.key.split(':', 1)[0]}"
    # 列表行而不是卡片：库里动辄几十条，层层边框会把真正的内容压没。
    with st.container(key=f"rp_library_row_{index}"):
        external_url = links.paper_external_url(entry.paper)
        if external_url:
            st.markdown(
                f'<div class="rp-library-title"><b><a '
                f'href="{theme.esc(external_url)}" target="_blank" '
                f'rel="noopener noreferrer">{theme.esc(_entry_title(entry))}</a>'
                f'</b></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f"**{_entry_title(entry)}**")
        st.markdown(
            f'<div class="rp-meta">{theme.esc(_entry_caption(entry, titles, project.id))}</div>',
            unsafe_allow_html=True,
        )
        cols = st.columns([1, 1, 3])
        if current_has is not None:
            if cols[0].button(
                "打开", key=f"{key}_open", icon=":material/open_in_new:", width="stretch"
            ):
                st.session_state["selected_paper_id"] = current_has.id
                st.session_state["selected_paper_project"] = project.id
                st.rerun()
        elif cols[0].button(
            "加入本项目", key=f"{key}_add", type="primary", width="stretch"
        ):
            paper, created = library.attach_to_project(project.id, entry.paper)
            st.session_state["selected_paper_id"] = paper.id
            st.session_state["selected_paper_project"] = project.id
            st.session_state["_library_notice"] = (
                "success" if created else "info",
                "已加入本项目" if created else "本项目已经有这篇文献了。",
            )
            st.rerun()
        with cols[1]:
            _delete_control(entry, key, current_has=current_has)


def render(project: Project, *, height: int = 420, expanded: bool = False) -> None:
    """文献库（跨项目）。默认收起——它是"需要时才查的资料库"，不是主界面。"""
    entries = library.entries()
    titles = _titles()
    notice = st.session_state.pop("_library_notice", None)
    if notice:
        kind, text = notice
        {"success": st.success, "info": st.info, "error": st.error}[kind](text)

    header = f"文献库（{len(entries)} 篇 · 跨 {len(titles)} 个项目共用）"
    with st.expander(header, expanded=expanded):
        query = st.text_input(
            "在文献库里搜索", key=f"lib_search_{project.id}",
            placeholder="标题 / 作者 / DOI", label_visibility="collapsed",
        )
        needle = query.strip().lower()
        filtered = [
            entry for entry in entries
            if not needle or needle in " ".join(
                [
                    entry.paper.title or "",
                    entry.paper.title_raw or "",
                    entry.paper.authors or "",
                    entry.paper.doi or "",
                ]
            ).lower()
        ]
        limit_key = f"lib_limit_{project.id}"
        shown = int(st.session_state.get(limit_key, LIST_PAGE))
        st.caption(f"匹配 {len(filtered)} 篇｜本次显示 {min(shown, len(filtered))} 篇")
        with st.container(height=height, border=False):
            for index, entry in enumerate(filtered[:shown]):
                _row(entry, project, titles, index)
        if len(filtered) > shown:
            if st.button(
                f"显示更多（还有 {len(filtered) - shown} 篇）",
                key=f"lib_more_{project.id}",
            ):
                st.session_state[limit_key] = shown + LIST_PAGE
                st.rerun()

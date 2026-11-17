"""文献阅读工作区。

页面在浏览态采用“左侧项目文献库 + 右侧文献详情”，进入阅读后切换为
“左侧原 PDF + 右侧 AI 解读”。全局侧栏只负责项目和模型设置。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import streamlit as st

from ..literature import bibliography as bib
from ..literature import citations as cite_mod
from ..literature import fulltext
from ..literature import graph_preferences
from ..literature import graph_versions
from ..literature import links
from ..literature import library
from ..literature import online
from ..literature import paths as paper_paths
from ..literature import repair
from ..literature.ingest import (
    IngestResult,
    attach_pdf_bytes_to_paper,
    ingest_pdf_bytes,
)
from ..literature.papers import pymupdf_status
from ..literature.text_quality import has_suspicious_controls, sanitize_for_display
from .. import display_text
from ..agent import extract as extract_mod
from ..agent import online_summary
from ..agent import session as session_mod
from ..agent.client import LLMClient
from ..agent.providers import LLMError
from ..storage import repository as repo
from ..storage.models import (
    Certainty,
    Paper,
    PaperClaim,
    PaperRelation,
    Project,
    RelationSource,
    RelationType,
    ReviewStatus,
    VerificationStatus,
)
from . import (
    browser_prefs,
    citation_graph,
    library_mover,
    library_panel,
    llm_settings,
    paper_outline,
    pdf_workspace,
    reading_guide,
    screening_page,
    split_layout,
    theme,
)

log = logging.getLogger(__name__)

MAX_FILES = 3
CATALOG_PAGE = 50          # 目录单次最多渲染多少条
RELATION_PAGE = 20         # 单篇详情里每个方向最多渲染多少条关系

# 索引栏与内部滚动区的高度。两边用同一个值，分隔条才能和目录一样高。
INDEX_HEIGHT = 560

CATEGORIES = ["全部", "我的论文", "参考文献", "施引论文", "待筛选候选"]


def _ingest_upload(project: Project, uploaded) -> IngestResult:
    """把上传的 PDF 落地并解析入库。

    实现放在 literature/ingest.py，与 scripts/ingest_pdf.py 共用同一条路径，
    这样"命令行能导入"与"界面能导入"不会出现行为分叉。
    """
    return ingest_pdf_bytes(project, uploaded.name, bytes(uploaded.getbuffer()))


_STATUS_LABEL: dict[VerificationStatus, str] = {
    VerificationStatus.UNVERIFIED: "未核查",
    VerificationStatus.VERIFIED: "已核实",
    VerificationStatus.PARTIAL: "部分一致",
    VerificationStatus.CONFLICT: "**存在冲突**",
}

_CHECK_LABEL = {
    "match": "一致",
    "mismatch": "冲突",
    "missing_local": "本地为空",
    "missing_external": "外部为空",
}

_RELATION_LABEL: dict[RelationType, str] = {
    RelationType.CITES: "引用",
    RelationType.EXTENDS: "扩展",
    RelationType.COMPARES: "对比",
    RelationType.REPLACES: "替代",
    RelationType.DIVERGES: "提出不同方法",
}


@st.cache_resource
def _http_client() -> bib.HttpClient:
    """全应用共用一个 HTTP 客户端：限速是按客户端实例记账的，
    每个页面各自新建实例会让限速形同虚设。"""
    return bib.HttpClient()


def _verification_block(paper: Paper) -> None:
    """核查状态、核查入口与明细。"""
    status = paper.verification_status
    st.write("核实状态：", _STATUS_LABEL.get(status, status.value))
    if paper.verified_at:
        st.caption(f"核实时间：{paper.verified_at}")
    if paper.external_ids:
        st.caption(
            "外部 ID："
            + "；".join(f"{key}={value}" for key, value in paper.external_ids.items())
        )

    col_a, col_b = st.columns([1, 2])
    if col_a.button("核查元数据", key=f"verify_{paper.id}"):
        with st.spinner("正在查询 Crossref 与 OpenAlex…"):
            try:
                report = bib.verify.verify_paper(paper, _http_client())
            except Exception as exc:  # 网络以外的意外也要显示真实原因
                st.error(f"核查失败：{exc}")
                log.exception("核查失败 %s", paper.id)
            else:
                st.success(f"核查完成：{report.describe()}")
                for error in report.errors:
                    st.warning(error)
                st.rerun()
    col_b.caption(
        "核查会用 DOI 查 Crossref 与 OpenAlex，逐字段比对。"
        "冲突时保留你本地已有的值，不自动覆盖。"
    )

    if paper.metadata_checks:
        with st.expander(f"核查明细（{len(paper.metadata_checks)} 条）"):
            st.dataframe(
                [
                    {
                        "字段": item.get("field"),
                        "来源": item.get("source"),
                        "结果": _CHECK_LABEL.get(item.get("status"), item.get("status")),
                        "本地值": str(item.get("local"))[:80],
                        "外部值": str(item.get("external"))[:80],
                        "说明": item.get("note", ""),
                    }
                    for item in paper.metadata_checks
                ],
                width="stretch",
                hide_index=True,
            )

    records = repo.list_source_records(paper.project_id, paper_id=paper.id)
    if records:
        with st.expander(f"外部查询来源记录（{len(records)} 条）"):
            st.dataframe(
                [
                    {
                        "来源": record.provider,
                        "查询类型": record.query_kind.value,
                        "查询值": record.query_value,
                        "外部 ID": record.external_id,
                        "抓取时间": record.fetched_at,
                        "状态": record.status.value,
                        "缓存": ("过期缓存" if record.stale else "缓存" if record.from_cache else "实时"),
                        "说明": record.note[:60],
                    }
                    for record in records
                ],
                width="stretch",
                hide_index=True,
            )


def _paper_year(paper: Paper | None) -> int:
    try:
        return int(str(paper.year or ""))
    except ValueError:
        return 0


def _openalex_citation_count(paper: Paper | None) -> int:
    """OpenAlex count is ranking metadata only; it is not a quality verdict."""
    if paper is None:
        return 0
    openalex = (paper.external_metadata or {}).get("openalex")
    if not isinstance(openalex, dict):
        return 0
    try:
        return max(0, int(openalex.get("cited_by_count") or 0))
    except (TypeError, ValueError):
        return 0


def _title_terms(paper: Paper) -> set[str]:
    title = (paper.title or "").lower()
    latin = set(re.findall(r"[a-z][a-z0-9-]{3,}", title))
    cjk = {
        title[index:index + 2]
        for index in range(max(0, len(title) - 1))
        if "\u4e00" <= title[index] <= "\u9fff"
        and "\u4e00" <= title[index + 1] <= "\u9fff"
    }
    return latin | cjk


def _title_relevance(current: Paper, other: Paper | None) -> int:
    if other is None:
        return 0
    return len(_title_terms(current) & _title_terms(other))


def _relation_description(
    relation: PaperRelation,
    current: Paper,
    other: Paper | None,
    slot: str,
) -> str:
    """Describe the literature-to-literature relation, not the paper's own method."""
    current_year, other_year = _paper_year(current), _paper_year(other)
    kind = relation.relation_type
    if kind is RelationType.CITES:
        if slot == "out":
            if (
                current_year
                and other_year
                and other_year < current_year - 8
                and _openalex_citation_count(other) >= 100
            ):
                return "本文方法与理论来源的重点溯源文献，优先用于核查核心方法基础。"
            if current_year and other_year and other_year < current_year:
                return "本文研究背景或方法设计所引用的前期工作，可用于方法来源核对。"
            return "本文引用的同期或后续研究，可用于方法演进与横向对照。"
        return "引用本文的后续研究，可作为方法延伸、应用与影响对照。"
    if kind is RelationType.EXTENDS:
        if relation.from_paper_id == current.id:
            return "本文前期研究基础，当前论文在此基础上作了方法扩展。"
        return "本文方法的后续延伸研究，可用于追踪改进路线。"
    if kind is RelationType.COMPARES:
        return "与本文方法构成直接对照，可用于比较适用条件与结果差异。"
    if kind is RelationType.REPLACES:
        if relation.from_paper_id == current.id:
            return "本文提出替代方案所针对的前期方法。"
        return "提出对本文方法的替代路线，可用于方法边界对照。"
    if kind is RelationType.DIVERGES:
        return "与本文在方法路线或研究结论上存在分歧，适合并列核查。"
    return "该文献与本文存在需要人工核验的学术关联。"


def _needs_verification(relation: PaperRelation, other: Paper | None) -> bool:
    if _is_official_citation(relation):
        return False
    return (
        other is None
        or not relation.verified
        or relation.certainty is not Certainty.STATED
    )


def _graph_group_label(relation: PaperRelation, current: Paper) -> str:
    group = relation.graph_group
    if not group:
        group = (
            "related" if relation.relation_type is not RelationType.CITES
            else "method_source" if relation.from_paper_id == current.id
            else "later_extension"
        )
    return {
        "method_source": "方法来源",
        "later_extension": "后续延伸",
        "related": "相关研究",
    }.get(group, "相关研究")


def _is_official_citation(relation: PaperRelation) -> bool:
    """OpenAlex DOI/ID citation: system-verified, no repetitive manual confirm."""
    return (
        relation.relation_type is RelationType.CITES
        and relation.source is RelationSource.OPENALEX
        and relation.verified
        and bool(relation.external_id or relation.locator)
    )


_GRAPH_GROUPS = {
    "method_source": "方法来源",
    "later_extension": "后续延伸",
    "related": "相关研究",
}


def _save_relation_note(
    current_id: str,
    relation_id: str,
    version_id: str,
    auto_version: bool,
    widget_key: str,
) -> None:
    current = repo.get(Paper, current_id)
    relation = repo.get(PaperRelation, relation_id)
    if current is None or relation is None:
        return
    note = str(st.session_state.get(widget_key) or "").strip()[:500]
    if auto_version:
        relation.review_note = note
        repo.save(relation)
    else:
        graph_versions.update_node(
            current, version_id, relation.id, note=note
        )


def _save_relation_group(
    current_id: str,
    relation_id: str,
    version_id: str,
    auto_version: bool,
    widget_key: str,
) -> None:
    current = repo.get(Paper, current_id)
    relation = repo.get(PaperRelation, relation_id)
    if current is None or relation is None:
        return
    group = str(st.session_state.get(widget_key) or "related")
    if group not in _GRAPH_GROUPS:
        return
    if auto_version:
        relation.graph_group = group
        repo.save(relation)
    else:
        graph_versions.update_node(
            current, version_id, relation.id, graph_group=group
        )


def _toggle_relation_focus(current_id: str, relation_id: str) -> None:
    current = repo.get(Paper, current_id)
    relation = repo.get(PaperRelation, relation_id)
    if current is None or relation is None:
        return
    focused, message = graph_preferences.toggle_focus(current, relation)
    st.session_state["_library_notice"] = (
        "success" if focused else "info",
        message,
    )


def _locate_relation_in_graph(
    current_id: str, relation_id: str
) -> None:
    current = repo.get(Paper, current_id)
    if current is None:
        return
    graph_preferences.select_relation(current, relation_id)
    st.session_state[f"citation_view_{current_id}"] = "图谱视图"
    st.session_state["_detail_default_tab"] = "引用"
    st.session_state[f"citation_scroll_relation_{current_id}"] = relation_id
    browser_prefs.set(citation_view="图谱视图")
    st.rerun()


def _relation_row(
    relation: PaperRelation,
    other: Paper | None,
    current: Paper,
    slot: str,
) -> None:
    """One compact relation card; only ambiguous relations ask for a decision."""
    key_prefix = f"{current.id}_{relation.id}"
    official = _is_official_citation(relation)
    version_id = str(st.session_state.get(
        f"cite_graph_version_{current.id}", graph_versions.AUTO_VERSION
    ))
    version_config = graph_versions.node_config(current, version_id, relation.id)
    auto_version = version_id == graph_versions.AUTO_VERSION
    display_note = (
        str(version_config.get("note") or "")
        if "note" in version_config
        else relation.review_note
    )
    is_focus = graph_preferences.is_focused(current, relation.id)
    is_selected = (
        graph_preferences.selected_relation_id(current) == relation.id
    )
    group_key = str(
        version_config.get("graph_group")
        or relation.graph_group
        or citation_graph._default_group(relation, current)
    )
    if group_key not in _GRAPH_GROUPS:
        group_key = "related"
    with st.container(key=f"rp_relation_card_{key_prefix}"):
        if is_selected:
            st.markdown(
                '<span class="rp-relation-selected-marker" aria-label="当前联动条目"></span>',
                unsafe_allow_html=True,
            )
        if is_focus:
            st.markdown(
                '<span class="rp-relation-focus-marker" aria-label="重点条目"></span>',
                unsafe_allow_html=True,
            )
        head = st.columns([8.8, 1.1, 1.3], vertical_alignment="center")
        title = (
            other.title.strip()
            if other and other.title.strip()
            else "（未知文献）"
        )
        if other:
            if head[0].button(
                title[:120],
                key=f"open_relation_{key_prefix}",
                type="tertiary",
                width="stretch",
                help=f"打开文献详情：{title}",
            ):
                st.session_state["selected_paper_id"] = other.id
                st.session_state["selected_paper_project"] = current.project_id
                browser_prefs.set(**{_paper_pref_key(current.project_id): other.id})
                st.rerun()
            if other.year:
                head[1].markdown(
                    f'<span class="rp-rel-year">（{theme.esc(other.year)}）</span>',
                    unsafe_allow_html=True,
                )
        else:
            head[0].markdown(f"**{theme.esc(title)}**")
        if official:
            head[2].markdown(
                '<span class="rp-rel-verified">OpenAlex 已核实</span>',
                unsafe_allow_html=True,
            )
        elif _needs_verification(relation, other):
            head[2].markdown(
                '<span class="rp-rel-verify">待人工判断</span>',
                unsafe_allow_html=True,
            )
        elif relation.downstream_ready:
            head[2].markdown(
                '<span class="rp-rel-verified">已纳入图谱</span>',
                unsafe_allow_html=True,
            )

        description = _relation_description(relation, current, other, slot)
        group_label = _GRAPH_GROUPS[group_key]
        st.markdown(
            f'<div class="rp-rel-description">'
            f'<span class="rp-graph-group">{theme.esc(group_label)}</span>'
            f'{theme.esc(description)}</div>',
            unsafe_allow_html=True,
        )

        controls = st.columns([1.15, 1.05, 1.3, 2.2], vertical_alignment="bottom")
        controls[0].button(
            "在图谱中定位",
            key=f"locate_{key_prefix}",
            icon=":material/my_location:",
            width="stretch",
            help="切换到图谱视图并居中该节点",
            on_click=_locate_relation_in_graph,
            args=(current.id, relation.id),
        )
        controls[1].button(
            "取消重点" if is_focus else "标记重点",
            key=f"focus_{key_prefix}",
            icon=":material/star:" if is_focus else ":material/star_border:",
            type="primary" if is_focus else "secondary",
            width="stretch",
            on_click=_toggle_relation_focus,
            args=(current.id, relation.id),
        )
        group_widget = f"group_{key_prefix}"
        group_sync = f"group_sync_{key_prefix}"
        if st.session_state.get(group_sync) != group_key:
            st.session_state[group_widget] = group_key
            st.session_state[group_sync] = group_key
        controls[2].selectbox(
            "图谱分类",
            list(_GRAPH_GROUPS),
            format_func=_GRAPH_GROUPS.get,
            key=group_widget,
            on_change=_save_relation_group,
            args=(
                current.id,
                relation.id,
                version_id,
                auto_version,
                group_widget,
            ),
        )
        controls[3].caption(
            "重点会置顶并同步到图谱星标；分类仅改变图谱分组，不改引用方向。"
        )

        note = display_note
        with st.expander(
            "添加备注（可选）",
            expanded=False,
            key=f"note_expander_{key_prefix}",
        ):
            note_key = f"note_{key_prefix}"
            note_sync = f"note_sync_{key_prefix}"
            if st.session_state.get(note_sync) != display_note:
                st.session_state[note_key] = display_note
                st.session_state[note_sync] = display_note
            note = st.text_input(
                "备注",
                key=note_key,
                label_visibility="collapsed",
                placeholder="例如：核心方法来源，纳入综述",
                on_change=_save_relation_note,
                args=(
                    current.id,
                    relation.id,
                    version_id,
                    auto_version,
                    note_key,
                ),
            )
            if st.button(
                "保存备注",
                key=f"save_note_{key_prefix}",
                width="stretch",
            ):
                if auto_version:
                    relation.review_note = note.strip()[:500]
                    repo.save(relation)
                else:
                    graph_versions.update_node(
                        current, version_id, relation.id, note=note.strip()[:500]
                    )
                st.session_state["_library_notice"] = ("success", "引用备注已保存")
                st.rerun()

        if official:
            action = st.columns([1, 5])
            if action[0].button(
                "从图谱移除",
                key=f"no_{key_prefix}",
                type="tertiary",
                help="不删除论文或查询记录，只把这条引用排除出下游分析",
            ):
                repo.review_paper_relation(
                    relation,
                    ReviewStatus.REJECTED,
                    note if auto_version else relation.review_note,
                )
                st.rerun()
        else:
            buttons = st.columns([0.9, 0.9, 4.2])
            if buttons[0].button(
                "确认纳入",
                key=f"ok_{key_prefix}",
            ):
                repo.review_paper_relation(
                    relation,
                    ReviewStatus.CONFIRMED,
                    note if auto_version else relation.review_note,
                )
                st.rerun()
            if buttons[1].button(
                "驳回",
                key=f"no_{key_prefix}",
                type="tertiary",
            ):
                repo.review_paper_relation(
                    relation,
                    ReviewStatus.REJECTED,
                    note if auto_version else relation.review_note,
                )
                st.rerun()


def _relation_sort_key(
    relation: PaperRelation,
    current: Paper,
    other: Paper | None,
    slot: str,
):
    """Rank sources first for references; cited works by relevance and reach."""
    year = _paper_year(other)
    current_year = _paper_year(current)
    cited_by = _openalex_citation_count(other)
    relevance = _title_relevance(current, other)
    if slot == "out":
        age = max(0, current_year - year) if year and current_year else 0
        foundational = cited_by + age * 50 + relevance * 100
        return (-foundational, year or 9999, relation.id)
    if slot == "in":
        return (-relevance, -cited_by, -(year or 0), relation.id)
    return (relation.created_at, relation.id)


def _graph_label(value: str, limit: int = 34) -> str:
    text = sanitize_for_display(value or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _pending_relation_block(paper: Paper, relations: list[PaperRelation]) -> None:
    if not relations:
        return
    with st.expander(f"待人工判断（{len(relations)}）", expanded=False):
        st.caption(
            "这里只放低置信度引用或语义判断。确认后才纳入关系图谱；"
            "没有证据位置的确认仍不会被视为已验证。"
        )
        labels: dict[str, str] = {}
        for relation in relations:
            other_id = (
                relation.to_paper_id
                if relation.from_paper_id == paper.id
                else relation.from_paper_id
            )
            other = repo.get(Paper, other_id)
            labels[relation.id] = (
                f"{_RELATION_LABEL.get(relation.relation_type, '关系')} · "
                f"{_graph_label(other.title if other else '未知文献', 48)}"
            )
        selected = st.multiselect(
            "选择待裁决关系",
            list(labels),
            format_func=labels.get,
            key=f"batch_relation_pick_{paper.id}",
        )
        actions = st.columns([1, 1, 3])
        if actions[0].button(
            "批量确认",
            key=f"batch_relation_ok_{paper.id}",
            disabled=not selected,
        ):
            for relation_id in selected:
                relation = repo.get(PaperRelation, relation_id)
                if relation is not None:
                    repo.review_paper_relation(
                        relation, ReviewStatus.CONFIRMED, relation.review_note
                    )
            st.rerun()
        if actions[1].button(
            "批量驳回",
            key=f"batch_relation_no_{paper.id}",
            type="tertiary",
            disabled=not selected,
        ):
            for relation_id in selected:
                relation = repo.get(PaperRelation, relation_id)
                if relation is not None:
                    repo.review_paper_relation(
                        relation, ReviewStatus.REJECTED, relation.review_note
                    )
            st.rerun()
        for relation in relations[:RELATION_PAGE]:
            slot = (
                "out" if relation.from_paper_id == paper.id
                else "in" if relation.to_paper_id == paper.id
                else "semantic"
            )
            other_id = (
                relation.to_paper_id if slot == "out"
                else relation.from_paper_id if slot == "in"
                else relation.to_paper_id
            )
            _relation_row(relation, repo.get(Paper, other_id), paper, slot)


def _citation_view_switch(paper: Paper) -> str:
    key = f"citation_view_{paper.id}"
    options = ["列表视图", "图谱视图"]
    remembered = str(browser_prefs.get("citation_view", "图谱视图"))
    if remembered not in options:
        remembered = "图谱视图"
    if st.session_state.get(key) not in options:
        st.session_state[key] = remembered
    selected = str(st.session_state[key])
    with st.container(key="rp_citation_view_switch"):
        buttons = st.columns([1, 1, 5])
        if buttons[0].button(
            "列表视图",
            key=f"citation_view_list_{paper.id}",
            type="primary" if selected == "列表视图" else "secondary",
            width="stretch",
            help="逐条查看并编辑引用关系",
        ):
            st.session_state[key] = "列表视图"
            browser_prefs.set(citation_view="列表视图")
            st.rerun()
        if buttons[1].button(
            "图谱视图",
            key=f"citation_view_graph_{paper.id}",
            type="primary" if selected == "图谱视图" else "secondary",
            width="stretch",
            help="在关系网络中浏览，并与下方引用列表联动",
        ):
            st.session_state[key] = "图谱视图"
            browser_prefs.set(citation_view="图谱视图")
            st.rerun()
        buttons[2].caption(
            "两种视图共用同一份引用数据；状态、备注、分类和重点标记实时同步。"
        )
    if browser_prefs.get("citation_view", "图谱视图") != selected:
        browser_prefs.set(citation_view=selected)
    return selected


def _relations_block(paper: Paper, view: str = "列表视图") -> None:
    """Layered citation network: verified official edges, pending judgments,
    confirmed semantic relations, and retained rejected records."""
    all_relations = repo.list_paper_relations(
        paper.project_id, paper_id=paper.id, direction="both"
    )
    active = [
        relation for relation in all_relations
        if relation.review_status is not ReviewStatus.REJECTED
    ]
    rejected = [
        relation for relation in all_relations
        if relation.review_status is ReviewStatus.REJECTED
    ]
    official = [relation for relation in active if _is_official_citation(relation)]
    cites_out = [
        relation for relation in official
        if relation.from_paper_id == paper.id
    ]
    cites_in = [
        relation for relation in official
        if relation.to_paper_id == paper.id
    ]
    pending = [
        relation for relation in active
        if not _is_official_citation(relation)
        and (
            relation.review_status is ReviewStatus.PENDING
            or not relation.verified
        )
    ]
    pending_ids = {relation.id for relation in pending}
    semantic = [
        relation for relation in active
        if relation.relation_type is not RelationType.CITES
        and relation.id not in pending_ids
    ]
    with_related = bool(st.session_state.get(f"rel_{paper.id}", False))

    if view == "图谱视图":
        project = repo.get(Project, paper.project_id)
        if project is not None:
            with st.expander("打开引用关系图谱工作区", expanded=True):
                citation_graph.render(project, paper, all_relations)
        selected_relation = graph_preferences.selected_relation_id(paper)
        if selected_relation:
            citation_graph.scroll_to_relation(paper.id, selected_relation)

    with st.expander(f"本文引用（{len(cites_out)}）", expanded=bool(cites_out)):
        _relation_section(paper, "本文引用", cites_out, "out")
    with st.expander(f"被引文献（{len(cites_in)}）", expanded=bool(cites_in)):
        _relation_section(paper, "被引文献", cites_in, "in")
    _pending_relation_block(paper, pending)
    with st.expander(f"语义关系（{len(semantic)}）", expanded=with_related):
        _relation_section(paper, "语义关系", semantic, "semantic")
    if rejected:
        with st.expander(f"已驳回（{len(rejected)}）", expanded=False):
            for relation in rejected[:RELATION_PAGE]:
                other_id = (
                    relation.to_paper_id
                    if relation.from_paper_id == paper.id
                    else relation.from_paper_id
                )
                other = repo.get(Paper, other_id)
                head = st.columns([5, 1], vertical_alignment="center")
                head[0].markdown(
                    f"**{theme.esc(_graph_label(other.title if other else '未知文献', 70))}**"
                )
                if head[1].button(
                    "恢复",
                    key=f"restore_{paper.id}_{relation.id}",
                    width="stretch",
                ):
                    repo.review_paper_relation(
                        relation, ReviewStatus.PENDING, relation.review_note
                    )
                    st.rerun()
                if relation.review_note:
                    st.caption(relation.review_note)


def _relation_section(
    paper: Paper, title: str, relations: list[PaperRelation], slot: str
) -> None:
    """One direction's ranked, paginated relation list."""
    if not relations:
        st.caption(f"暂无{title}。")
        return
    entries: list[tuple[PaperRelation, Paper | None]] = []
    for relation in relations:
        if slot == "out":
            other_id = relation.to_paper_id
        elif slot == "in":
            other_id = relation.from_paper_id
        else:
            other_id = (
                relation.to_paper_id
                if relation.from_paper_id == paper.id
                else relation.from_paper_id
            )
        entries.append((relation, repo.get(Paper, other_id)))
    focused = set(graph_preferences.focused_ids(paper))
    entries.sort(key=lambda item: (
        0 if item[0].id in focused else 1,
        _relation_sort_key(item[0], paper, item[1], slot),
    ))

    limit_key = f"rel_limit_{paper.id}_{slot}"
    shown = int(st.session_state.get(limit_key, RELATION_PAGE))
    for relation, other in entries[:shown]:
        _relation_row(relation, other, paper, slot)
    if len(entries) > shown:
        remaining = len(entries) - shown
        if st.button(
            f"显示更多（还有 {remaining} 条）",
            key=f"relmore_{paper.id}_{slot}",
        ):
            st.session_state[limit_key] = shown + RELATION_PAGE
            st.rerun()


def _citation_fetch_block(paper: Paper) -> None:
    cols = st.columns([1, 1, 1, 3])
    limit = cols[0].number_input(
        "每方向上限", min_value=1, max_value=50, value=10, key=f"limit_{paper.id}",
        help="参考文献与施引论文各自最多拉多少条",
    )
    with_related = cols[1].checkbox(
        "含相关候选", value=False, key=f"rel_{paper.id}",
        help="OpenAlex 的相关文献只是算法相似度，不是引用，只导入为候选、不建边",
    )
    if cols[2].button("拉取引用关系", key=f"cite_{paper.id}"):
        with st.spinner("正在查询 OpenAlex 的参考文献与施引论文…"):
            try:
                report = cite_mod.fetch_citation_network(
                    paper, _http_client(), limit=int(limit), include_related=bool(with_related)
                )
            except Exception as exc:
                st.error(f"拉取失败：{exc}")
                log.exception("拉取引用关系失败 %s", paper.id)
            else:
                if report.errors:
                    for error in report.errors:
                        st.warning(error)
                for warning in report.warnings:
                    st.info(warning)
                st.success(f"完成：{report.describe()}")
                st.rerun()
    cols[3].caption(
        "官方引用会自动进入关系图谱；相关候选只导入论文，不建立引用边"
    )


def _headline_status(paper: Paper, pending_relations: int) -> None:
    """标题下方的一行状态。短、只列需要用户注意的项，不重复长说明。"""
    bits: list[str] = []
    status = paper.verification_status
    if status is VerificationStatus.CONFLICT:
        bits.append("⚠ 元数据冲突")
    if has_suspicious_controls(paper.title_raw or paper.title):
        bits.append("⚠ 标题待确认")
    if pending_relations:
        bits.append(f"待裁决关系 {pending_relations}")
    if bits:
        st.caption("　｜　".join(bits))


_CERTAINTY_MARK = {"stated": "原文事实", "implied": "原文近似"}

# AI 分析的终态：每一种都要有明确说法，不允许"转完圈什么都没发生"
_AI_VERDICT = {
    "success": ("success", "分析完成"),
    "partial": ("warning", "分析完成：部分内容未通过原文证据校验"),
    "all_rejected": ("warning", "AI 已返回结果，但没有通过原文证据校验的结论"),
    "empty": ("warning", "AI 没有返回任何可抽取的条目"),
    "failed": ("error", "分析失败"),
    "refused": ("info", "这篇文献暂不做 AI 分析"),
}
AI_RECORD_KEY = "ai_analysis"
ONLINE_RECORD_KEY = "online_source"
ANALYSIS_DONE_SECONDS = 5
_ANALYSIS_JOBS: dict[str, dict] = {}
_ANALYSIS_JOBS_LOCK = threading.Lock()


def _analysis_done_key(paper_id: str) -> str:
    return f"_analysis_done_at_{paper_id}"


def _analysis_job_key(paper_id: str) -> str:
    return f"_analysis_job_{paper_id}"


def _analysis_job_snapshot(job_id: str) -> dict | None:
    with _ANALYSIS_JOBS_LOCK:
        job = _ANALYSIS_JOBS.get(job_id)
        return dict(job) if job else None


def _persist_analysis_result(paper: Paper, outcome) -> None:
    summary = {**(paper.summary or {}), AI_RECORD_KEY: outcome.to_dict()}
    online_record = summary.get(ONLINE_RECORD_KEY)
    if (
        isinstance(online_record, dict)
        and outcome.kept
        and online_record.get("status") == "abstract"
    ):
        summary[ONLINE_RECORD_KEY] = {
            **online_record,
            "status": "fulltext",
        }
    paper.summary = summary
    repo.save(paper)


def _analysis_worker(job_id: str, paper: Paper, client: LLMClient) -> None:
    """在后台执行分析；这里不访问 Streamlit 会话或界面。"""
    try:
        outcome = extract_mod.extract_paper(client, paper)
        _persist_analysis_result(paper, outcome)
    except Exception as exc:  # noqa: BLE001 - 后台任务必须把真实错误回传到状态
        log.exception("AI 分析异常 %s", paper.id)
        outcome = extract_mod.ExtractionOutcome(
            paper_id=paper.id,
            status="failed",
            reason=f"分析过程中出现未预期的错误：{type(exc).__name__}: {str(exc)[:200]}",
        )
        _persist_analysis_result(paper, outcome)

    with _ANALYSIS_JOBS_LOCK:
        if job_id in _ANALYSIS_JOBS:
            _ANALYSIS_JOBS[job_id]["state"] = "done"
            _ANALYSIS_JOBS[job_id]["finished_at"] = time.time()


def _start_analysis(paper: Paper) -> None:
    """先启动后台任务并立即重绘，保证首帧就能看到“正在分析”。"""
    credentials = session_mod.store().current()
    if credentials is None:
        st.error("尚未连接 AI 模型。")
        return
    snapshot = {
        session_mod.SESSION_KEY: {
            "provider": credentials.provider,
            "base_url": credentials.base_url,
            "model": credentials.model,
            "timeout": credentials.timeout,
            "api_key": credentials.api_key,
            "source": credentials.source,
            "thinking": credentials.thinking,
            "reasoning_effort": credentials.reasoning_effort,
        }
    }
    job_id = repo.new_id("analysis")
    with _ANALYSIS_JOBS_LOCK:
        _ANALYSIS_JOBS[job_id] = {
            "state": "running",
            "paper_id": paper.id,
            "started_at": time.time(),
        }
    st.session_state[_analysis_job_key(paper.id)] = job_id
    client = LLMClient(session_mod.CredentialStore(snapshot))
    worker = threading.Thread(
        target=_analysis_worker,
        args=(job_id, paper, client),
        name=f"rp-analysis-{paper.id}",
        daemon=True,
    )
    worker.start()
    st.rerun()


@st.fragment(run_every=0.5)
def _analysis_runtime(paper_id: str) -> None:
    """轮询后台任务；完成后切换到 5 秒的短提示。"""
    job_key = _analysis_job_key(paper_id)
    job_id = str(st.session_state.get(job_key) or "")
    job = _analysis_job_snapshot(job_id) if job_id else None
    if job is None:
        st.session_state.pop(job_key, None)
        return
    if job.get("state") == "running":
        st.status("正在分析", expanded=True, state="running")
        return

    st.session_state.pop(job_key, None)
    with _ANALYSIS_JOBS_LOCK:
        _ANALYSIS_JOBS.pop(job_id, None)
    st.session_state[_analysis_done_key(paper_id)] = time.time()
    st.rerun(scope="app")


@st.fragment(run_every=0.5)
def _analysis_complete_flash(paper_id: str) -> None:
    """短暂显示完成状态；5 秒后清理并触发整页重跑。"""
    key = _analysis_done_key(paper_id)
    finished_at = float(st.session_state.get(key) or 0)
    if not finished_at or time.time() - finished_at >= ANALYSIS_DONE_SECONDS:
        st.session_state.pop(key, None)
        st.rerun(scope="app")
        return
    with st.container(key="rp_analysis_done"):
        st.success("分析完成")


OVERVIEW_LIMITS = {
    "problem": 2,
    "method": 4,
    "conclusion": 4,
    "limitation": 3,
    "assumption": 1,
}


def _claim_pages(claim: PaperClaim, paper: Paper) -> list[int]:
    """Return locally located page(s), falling back to the legacy locator."""
    pages: list[int] = []
    if claim.page:
        pages.append(int(claim.page))
    for raw in re.findall(r"第\s*(\d+)\s*页", claim.locator or ""):
        page = int(raw)
        if page > 0:
            pages.append(page)
    if paper.page_count:
        pages = [page for page in pages if page <= paper.page_count]
    return list(dict.fromkeys(pages))


def _claim_citation(claim: PaperClaim, project_id: str, paper: Paper) -> str:
    """Render an inline page citation without changing the PDF jump contract."""
    if claim.certainty is Certainty.INFERRED:
        return ""
    pages = _claim_pages(claim, paper)
    if not pages:
        return ""
    mood = (
        "rp-cite--exact"
        if claim.certainty is Certainty.STATED
        else "rp-cite--implied"
    )
    links = []
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", claim.id)
    for page in pages:
        query = urlencode(
            {
                "project": project_id,
                "view": "literature",
                "cite_claim": claim.id,
                "cite_page": page,
            }
        )
        title = f"定位至第 {page} 页"
        links.append(
            f'<a class="rp-cite {mood}" id="claim_pdf_{safe_id}_{page}" '
            f'href="?{theme.esc(query)}" title="{title}" aria-label="{title}">'
            f"{page}</a>"
        )
    return f"<sup>{', '.join(links)}</sup>"


def _claim_sentence(claim: PaperClaim, project_id: str, paper: Paper) -> str:
    text = " ".join(str(claim.claim or "").split()).strip()
    ending = "" if text.endswith(("。", "！", "？", "!", "?", ".", "；", ";")) else "。"
    if claim.certainty is Certainty.INFERRED:
        inferred = "" if "推断" in text[-12:] else "（推断）"
        return f"{theme.esc(text)}{ending}{inferred}"
    return f"{theme.esc(text)}{ending}{_claim_citation(claim, project_id, paper)}"


def _overview_claims(
    claims: list[PaperClaim], kinds: tuple[str, ...], limit: int
) -> list[PaperClaim]:
    """Keep useful, de-duplicated claims in page order."""
    kept: list[PaperClaim] = []
    seen: set[str] = set()
    ordered = sorted(
        (claim for claim in claims if claim.kind in kinds),
        key=lambda claim: (
            claim.page if claim.page and claim.page > 0 else 10**9,
            claim.created_at,
            claim.id,
        ),
    )
    for claim in ordered:
        text = " ".join(str(claim.claim or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        kept.append(claim)
        if len(kept) >= limit:
            break
    return kept


def _overview_paragraph(
    claims: list[PaperClaim], project_id: str, paper: Paper
) -> None:
    if not claims:
        theme.markdown('<p class="rp-overview-copy">论文未提及。</p>')
        return
    body = " ".join(_claim_sentence(claim, project_id, paper) for claim in claims)
    theme.markdown(f'<p class="rp-overview-copy">{body}</p>')


def _overview_list(
    claims: list[PaperClaim], project_id: str, paper: Paper
) -> None:
    if not claims:
        theme.markdown('<p class="rp-overview-copy">论文未提及。</p>')
        return
    items = "".join(
        f'<li class="rp-overview-item">'
        f"{_claim_sentence(claim, project_id, paper)}</li>"
        for claim in claims
    )
    theme.markdown(f'<ul class="rp-overview-list">{items}</ul>')



def _ai_record(paper: Paper) -> dict | None:
    """上一次分析的结果记录。存在 paper.summary 里，刷新与导出都还在。"""
    record = (paper.summary or {}).get(AI_RECORD_KEY)
    return record if isinstance(record, dict) else None


def _render_ai_record(record: dict) -> None:
    verdict = str(record.get("verdict") or "failed")
    level, title = _AI_VERDICT.get(verdict, ("info", "分析结束"))
    if verdict in {"failed", "refused"}:
        getattr(st, level)(title)
    else:
        theme.markdown(theme.hidden_compat(title))

    if verdict == "failed":
        st.error(f"原因：{record.get('reason') or '未记录'}")
    elif verdict == "refused":
        st.info(record.get("reason") or "这篇文献暂不做 AI 分析")
    else:
        theme.markdown(theme.hidden_compat(
            f"模型返回 {record.get('raw_item_count', 0)} 条　→　"
            f"保留 {record.get('kept_count', 0)} 条（通过原文校验）　·　"
            f"丢弃 {record.get('dropped_count', 0)} 条"
        ))

    facts = []
    if record.get("model"):
        facts.append(f"模型 {record['model']}")
    if record.get("elapsed_s"):
        facts.append(f"耗时 {record['elapsed_s']}s")
    # P0-B：正文范围按**实际覆盖**说，不再写死"前 N 页"
    pages_done = record.get("pages_analyzed") or 0
    page_total = record.get("page_count") or 0
    if pages_done and page_total:
        scope = f"正文覆盖 {pages_done}/{page_total} 页"
        if record.get("requests"):
            scope += f"（{record['requests']} 段分批送入）"
        facts.append(scope)
    elif record.get("excerpt_pages"):
        facts.append(f"正文 {record['excerpt_pages']} 页 / {record.get('excerpt_chars', 0)} 字符")
    if record.get("sections_total"):
        facts.append(f"章节块 {record.get('sections_analyzed', 0)}/{record['sections_total']}")
    if record.get("timeout_s"):
        facts.append(f"超时上限 {record['timeout_s']:g}s")
    if record.get("finished_at"):
        facts.append(f"完成于 {record['finished_at']}")
    if facts:
        theme.markdown(theme.hidden_compat("　·　".join(facts)))

    notes = record.get("coverage_notes") or []
    dropped = record.get("dropped") or []
    if notes or dropped:
        theme.markdown(theme.hidden_compat(
            f"覆盖说明 {len(notes)} 条；未采用 {len(dropped)} 条"
        ))


def _ai_analysis_block(paper: Paper, project: Project) -> None:
    """Four-section skim overview with inline, clickable page citations."""
    online_record = _online_record(paper)
    if online_record and online_record.get("status") == "fulltext":
        license_name = str(online_record.get("license") or "开放获取")
        st.caption(f"概览依据在线开放全文 · {license_name}")
    if _analysis_job_key(paper.id) in st.session_state:
        _analysis_runtime(paper.id)
        return
    if _analysis_done_key(paper.id) in st.session_state:
        _analysis_complete_flash(paper.id)
    # 先渲染上一次的终态：重跑与刷新之后，用户仍然知道发生了什么
    record = _ai_record(paper)
    if record:
        _render_ai_record(record)

    claims = extract_mod.claims_of(paper)
    problem = _overview_claims(
        claims, ("problem",), OVERVIEW_LIMITS["problem"]
    )
    method = _overview_claims(
        claims, ("method",), OVERVIEW_LIMITS["method"]
    )
    assumptions = _overview_claims(
        claims, ("assumption",), OVERVIEW_LIMITS["assumption"]
    )
    conclusion = _overview_claims(
        claims, ("conclusion",), OVERVIEW_LIMITS["conclusion"]
    )
    limitation = _overview_claims(
        claims, ("limitation",), OVERVIEW_LIMITS["limitation"]
    )

    # 一次都没分析过时，不要用四段「论文未提及」冒充分析结论——那会被读成
    # 「AI 读了，但什么都没找到」。这里只给一句空状态，按钮照常在下面出现。
    if not claims and not record:
        theme.empty_state("尚未分析这篇文献：点下方按钮生成四段式概览")
    else:
        st.markdown("### 研究背景与问题")
        _overview_paragraph(problem, project.id, paper)

        st.markdown("### 核心方法与数据")
        _overview_list(method + assumptions, project.id, paper)

        st.markdown("### 主要结果与结论")
        _overview_list(conclusion, project.id, paper)

        st.markdown("### 局限与展望")
        _overview_paragraph(limitation, project.id, paper)

    ready, reason = extract_mod.extraction_readiness(paper)
    if not ready:
        theme.markdown(theme.hidden_compat(f"这篇文献暂不做 AI 分析：{reason}"))
        return

    if not llm_settings.connected():
        llm_settings.require_connection("AI 文献分析")
        return

    label = "重新分析这篇文献" if (record or claims) else "分析这篇文献"
    credentials = llm_settings.session_credentials()
    pages = extract_mod.paper_pages(paper)
    if credentials is not None:
        index = fulltext.build_index(paper)
        sections = len(index.sections) if index else 0
        chunks = len(fulltext.plan_requests(index)) if index else 0
        scope = f"全文 {len(pages)} 页"
        if sections:
            scope += f"、{sections} 个章节块"
        if chunks:
            scope += f"，分 {chunks} 次送入模型"
        st.caption(f"本次最多等待 {credentials.timeout:g} 秒；分析范围是{scope}。")
    action_key = "rp_reanalyze_action" if (record or claims) else "rp_analyze_action"
    with st.container(key=action_key):
        if st.button(label, key=f"ai_extract_{paper.id}", width="stretch"):
            _start_analysis(paper)


def _online_record(paper: Paper) -> dict | None:
    record = (paper.summary or {}).get(ONLINE_RECORD_KEY)
    return record if isinstance(record, dict) else None


def _render_online_abstract_overview(paper: Paper, record: dict) -> None:
    overview = record.get("overview")
    if not isinstance(overview, dict):
        return
    st.markdown("### 研究背景与问题")
    theme.markdown(
        f'<p class="rp-overview-copy">'
        f'{theme.esc(str(overview.get("background") or "摘要未说明"))}</p>'
    )
    st.markdown("### 核心方法与数据")
    theme.markdown(
        f'<p class="rp-overview-copy">'
        f'{theme.esc(str(overview.get("methods_and_data") or "摘要未说明"))}</p>'
    )
    st.markdown("### 主要结果与结论")
    theme.markdown(
        f'<p class="rp-overview-copy">'
        f'{theme.esc(str(overview.get("results") or "摘要未说明"))}</p>'
    )
    st.markdown("### 局限与展望")
    theme.markdown(
        f'<p class="rp-overview-copy">'
        f'{theme.esc(str(overview.get("limitations") or "摘要未说明"))}</p>'
    )
    source_url = str(record.get("source_url") or "")
    if source_url:
        st.link_button(
            "打开在线摘要来源",
            source_url,
            icon=":material/open_in_new:",
        )
    st.caption(
        "以上为依据 OpenAlex 公开摘要生成的摘要级概览；"
        "未读取全文，方法细节和量化结果仍以原文为准。"
    )


def _run_online_reading(paper: Paper) -> None:
    """Resolve an online paper, prefer an OA PDF, then summarize it."""
    running_key = f"online_reading_{paper.id}"
    st.session_state[running_key] = True
    try:
        with st.spinner("正在检索公开摘要与开放获取全文……"):
            source = online.fetch_online_source(paper, _http_client())
            binary = None
            download_note = ""
            if source.pdf_url:
                try:
                    binary = online.download_open_pdf(source, _http_client())
                except online.OnlineSourceUnavailable as exc:
                    download_note = str(exc)

        if binary is not None:
            with st.spinner("正在解析在线全文并生成带页码的概览……"):
                paper = online.attach_pdf_to_paper(
                    paper,
                    binary.content,
                    source,
                )
                outcome = extract_mod.extract_paper(LLMClient(), paper)
            summary = {**(paper.summary or {}), AI_RECORD_KEY: outcome.to_dict()}
            if outcome.kept:
                summary[ONLINE_RECORD_KEY] = {
                    **(summary.get(ONLINE_RECORD_KEY) or {}),
                    "status": "fulltext",
                    "provider": "openalex",
                    "landing_url": source.landing_url,
                    "license": source.license,
                }
                paper.summary = summary
                repo.save(paper)
                st.rerun()
            if source.abstract:
                with st.spinner("全文分析未形成有效结论，正在读取公开摘要……"):
                    fallback = online_summary.build_abstract_overview(
                        LLMClient(),
                        paper,
                        source.abstract,
                        source_url=source.landing_url,
                        provider="openalex",
                    )
                fallback["fulltext_note"] = (
                    outcome.reason
                    or "在线全文已获取，但未形成通过本地证据校验的条目"
                )
                summary[ONLINE_RECORD_KEY] = fallback
                paper.summary = summary
                repo.save(paper)
                st.rerun()
            paper.summary = summary
            repo.save(paper)
            st.rerun()

        if source.abstract:
            with st.spinner("正在依据公开摘要生成四段式概览……"):
                record = online_summary.build_abstract_overview(
                    LLMClient(),
                    paper,
                    source.abstract,
                    source_url=source.landing_url,
                    provider="openalex",
                )
            if download_note:
                record["fulltext_note"] = download_note
            paper.summary = {**(paper.summary or {}), ONLINE_RECORD_KEY: record}
            repo.save(paper)
            st.rerun()

        raise online.OnlineSourceUnavailable(
            download_note
            or "这篇文献既没有可读取的公开摘要，也没有开放获取全文。"
        )
    except (online.OnlineSourceUnavailable, LLMError) as exc:
        st.error(str(exc))
    finally:
        st.session_state.pop(running_key, None)


def _metadata_only_overview(paper: Paper, project: Project) -> None:
    """Honest overview for an AI-recommended paper without a local full text."""
    online_record = _online_record(paper)
    if online_record and online_record.get("status") == "abstract":
        st.markdown("### AI 在线概览")
        st.caption("摘要级在线概览 · 尚未读取全文")
        _render_online_abstract_overview(paper, online_record)
        if paper_paths.resolve_local_path(paper.local_path):
            if st.button(
                "AI 重新分析已获取全文",
                key=f"online_reanalyze_{paper.id}",
            ):
                _start_analysis(paper)
        elif st.button(
            "AI 重新在线阅读",
            key=f"online_read_refresh_{paper.id}",
        ):
            if llm_settings.require_connection("在线读取文献"):
                _run_online_reading(paper)
        return

    st.markdown("### AI 文献分析")
    running = bool(st.session_state.get(f"online_reading_{paper.id}"))
    if st.button(
        "AI 在线阅读并生成概览",
        key=f"online_read_{paper.id}",
        type="primary",
        disabled=running,
    ):
        if llm_settings.require_connection("在线读取文献"):
            _run_online_reading(paper)
    st.caption(
        "无需先上传 PDF。AI 会优先读取 OpenAlex 标注的开放获取全文；"
        "没有全文时使用公开摘要，并明确标注摘要级边界。"
        "读取内容会发送到当前 AI 模型。"
    )

    title = repair.detail_title(paper)
    authors = sanitize_for_display(paper.authors)
    meta = " · ".join(item for item in (paper.year, paper.doi) if item)
    st.markdown("### 推荐文献概况")
    theme.markdown(
        f'<p class="rp-overview-copy">'
        f"该文献目前仅有推荐元数据，平台尚未读取全文。"
        f"题名为「{theme.esc(title)}」"
        f"{('，' + theme.esc(meta)) if meta else ''}。"
        "因此不生成研究方法、结果或局限等全文结论。"
        f"</p>"
    )
    if authors:
        theme.markdown(
            f'<div class="rp-paper-card__meta">作者：{theme.esc(authors[:240])}</div>'
        )
    external_url = links.paper_external_url(paper)
    if external_url:
        st.link_button(
            "在来源网站阅读",
            external_url,
            icon=":material/open_in_new:",
        )
    else:
        st.caption("暂无可用的外部阅读链接。")
    with st.expander("从本地上传 PDF", expanded=False):
        _attach_pdf_uploader(project, paper)


def _primary_tab(paper: Paper, project: Project) -> None:
    """Default view: four-section skim overview with inline page citations."""
    online_record = _online_record(paper)
    if online_record and online_record.get("status") == "abstract":
        _metadata_only_overview(paper, project)
        return
    if paper_paths.resolve_local_path(paper.local_path) is None:
        _metadata_only_overview(paper, project)
        return
    _ai_analysis_block(paper, project)


def _author_candidate_hint(paper: Paper) -> None:
    """作者为空但文件名里能识别出来时，就在这里给一次"采用"。

    P0-1：中文论文常把作者写在文件名尾部（`标题_作者`），以前只有把「作者修正」
    展开才看得到候选，概览里永远是"—"，看起来像数据丢了。这里只**提示候选**，
    采用与否仍由用户点；点了才写入作者字段（未确认的数据保持候选状态）。
    """
    candidates = [
        item for item in repair.candidate_authors(paper)
        if item.get("source") == "filename:author" and str(item.get("value", "")).strip()
    ]
    if not candidates:
        return
    candidate = candidates[0]
    cols = st.columns([4, 1])
    cols[0].caption(
        f"文件名里有作者候选：**{sanitize_for_display(str(candidate['value']))}**"
        "（来自文件名，尚未确认）"
    )
    if cols[1].button("采用", key=f"quick_author_{paper.id}"):
        repair.apply_author(paper, str(candidate["value"]), "filename:author", candidate.get("note", ""))
        st.rerun()

    # 标题里若还带着作者尾巴（`标题_作者`），同时给出干净标题候选
    stem = paper_paths.stem(paper.local_path)
    value = sanitize_for_display(str(candidate["value"]))
    if stem.endswith(value) and len(stem) > len(value) + 2:
        clean = stem[: -len(value)].rstrip("_-—－ ")
        if clean and clean != paper.title:
            cols = st.columns([4, 1])
            cols[0].caption(f"标题里包含作者部分，可用标题候选：**{clean}**（尚未采用）")
            if cols[1].button("采用", key=f"quick_title_{paper.id}"):
                repair.apply_title(paper, clean, "filename", "从「标题_作者」结构中拆出的标题")
                st.rerun()


def _evidence_tab(paper: Paper) -> None:
    """二级详情：来源、核查、完整文件信息与低频修正操作，全部默认收起。"""
    origin = "用户上传" if paper.origin != "openalex" else "OpenAlex 自动导入的候选"
    # 只显示文件名：Windows 与 WSL 的分隔符都要处理（pathlib 在跨平台时会把整条路径当成文件名）
    file_label = paper_paths.file_name(paper.local_path) or "—"
    st.caption(f"{paper.year or '年份未知'}　·　{origin}　·　{paper.page_count or '?'} 页")

    with st.expander("来源与文件", expanded=False):
        st.markdown(
            f"- **DOI**：{paper.doi or '—'}\n"
            f"- **年份**：{paper.year or '—'}\n"
            f"- **文件**：{file_label}\n"
            f"- **正文页数**：{paper.page_count or '—'}"
        )
        with st.expander("修正年份 / DOI", expanded=False):
            new_year = st.text_input("年份", paper.year, key=f"y_{paper.id}")
            new_doi = st.text_input("DOI", paper.doi, key=f"d_{paper.id}")
            if st.button("保存修改", key=f"save_{paper.id}"):
                paper.year, paper.doi = new_year, new_doi
                repo.save(paper)
                st.success("已保存。")
                st.rerun()

    _title_repair_block(paper)
    _author_repair_block(paper)


def _parse_warnings(paper: Paper) -> None:
    if not paper.parse_warnings:
        return
    with st.expander(f"解析警告（{len(paper.parse_warnings)}）", expanded=False):
        for warning in paper.parse_warnings[:20]:
            st.warning(warning)


def _title_repair_block(paper: Paper) -> None:
    """标题修正：给出候选与来源，人工点「采用」才写入。"""
    current = paper.title_raw or paper.title
    abnormal = has_suspicious_controls(current)
    # 低频操作，默认收起；异常时标题旁会有一行提示指向这里
    with st.expander(
        "标题修正" + ("（检测到乱码，待确认）" if abnormal else ""), expanded=False
    ):
        if abnormal:
            st.warning(
                "当前标题含异常控制字符，通常是 PDF 子集化字体缺少 ToUnicode 映射。"
                "原始解析值已保留，不会被覆盖。"
            )
        if paper.title_raw and paper.title_raw != paper.title:
            st.caption("原始解析值（保留，未覆盖）：")
            st.code(paper.title_raw)
        st.caption(
            f"当前标题来源：{repair.source_label(paper.title_source)}"
            + (f"｜确认时间：{paper.title_confirmed_at}" if paper.title_confirmed_at else "")
        )

        st.markdown("**候选标题**")
        candidates = repair.candidate_titles(paper)
        for index, item in enumerate(candidates):
            cols = st.columns([5, 1])
            cols[0].write(sanitize_for_display(item["value"])[:160])
            note = f"｜{item['note']}" if item.get("note") else ""
            cols[0].caption(f"来源：{repair.source_label(item['source'])}{note}")
            if cols[1].button("采用", key=f"use_{paper.id}_{index}"):
                repair.apply_title(paper, item["value"], item["source"], item.get("note", ""))
                st.rerun()

        manual = st.text_input(
            "或手动输入标题", key=f"manual_{paper.id}",
            placeholder="粘贴正确标题后点保存",
        )
        if st.button("保存手动标题", key=f"manual_save_{paper.id}"):
            if not manual.strip():
                st.error("标题不能为空。")
            else:
                repair.apply_title(paper, manual.strip(), "manual", "人工输入")
                st.rerun()


def _author_repair_block(paper: Paper) -> None:
    """作者修正。与标题同一套纪律：只给候选，人工确认才写入。"""
    current = (paper.authors or "").strip()
    abnormal = has_suspicious_controls(current)
    with st.expander("作者修正", expanded=False):
        if abnormal:
            st.warning("当前作者字段含异常字符；原始值已保留，不会被覆盖。")
        if paper.authors_raw and paper.authors_raw != paper.authors:
            st.caption("原始解析值（保留，未覆盖）：")
            st.code(paper.authors_raw)
        if paper.author_source:
            st.caption(
                f"当前来源：{repair.source_label(paper.author_source)}"
                + (f"｜确认时间：{paper.author_confirmed_at}"
                   if paper.author_confirmed_at else "")
            )
        st.markdown("**候选作者**")
        candidates = repair.candidate_authors(paper)
        if not candidates:
            st.caption("没有可用的作者候选（文件名里没有识别出作者部分）。")
        for index, item in enumerate(candidates):
            cols = st.columns([5, 1])
            cols[0].write(sanitize_for_display(item["value"])[:120])
            note = f"｜{item['note']}" if item.get("note") else ""
            cols[0].caption(f"来源：{repair.source_label(item['source'])}{note}")
            if cols[1].button("采用", key=f"use_author_{paper.id}_{index}"):
                repair.apply_author(paper, item["value"], item["source"], item.get("note", ""))
                st.rerun()

        manual = st.text_input("或手动输入作者", key=f"manual_author_{paper.id}",
                               placeholder="多个作者用顿号分隔")
        if st.button("保存手动作者", key=f"manual_author_save_{paper.id}"):
            if not manual.strip():
                st.error("作者不能为空。")
            else:
                repair.apply_author(paper, manual.strip(), "manual:author", "人工输入")
                st.rerun()


def _uploads_state_key(project_id: str) -> str:
    """本会话里，这个项目**已经处理过**的上传（按文件内容 sha256 记账）。

    为什么必须有这本账：``st.file_uploader`` 的取值会一直留在控件里，
    处理完再 ``st.rerun()``，同一个文件会被再处理一次，于是形成
    "解析 → 提示 → 重跑 → 又解析" 的死循环（用户看到页面一直在转）。
    记账之后每个文件最多处理一次，重跑多少次都只读结果。
    """
    return f"_upload_done_{project_id}"


def _upload_results_key(project_id: str) -> str:
    return f"_upload_results_{project_id}"


def _record_upload_result(project: Project, name: str, result: IngestResult) -> None:
    """把一次上传的结果记进本会话，供后续重跑继续显示。"""
    rows = st.session_state.setdefault(_upload_results_key(project.id), [])
    rows.append(
        {
            "name": name,
            "status": "created" if result.created else result.matched_by,
            "pages": result.paper.page_count,
            "title": result.paper.title or result.paper.title_raw or name,
            "note": result.note,
            "paper_id": result.paper.id,
            "library_project_id": result.library_project_id,
        }
    )


def select_new_uploads(done: dict[str, bool], digests: list[str]) -> list[int]:
    """挑出本次**真正要处理**的上传下标，并把它们记进"已处理"账本。

    单独抽出来是为了能被无头测试直接覆盖：``st.file_uploader`` 在一次会话里会一直
    返回同一个文件，没有这本账就会"解析→重跑→再解析"地死循环。
    """
    fresh: list[int] = []
    for index, digest in enumerate(digests):
        if digest in done:
            continue
        done[digest] = True
        fresh.append(index)
    return fresh


def _render_upload_results(project: Project) -> None:
    """上传结果常驻显示：新建的、重复的、库里已有的，各说各的，不混在一起。"""
    rows = st.session_state.get(_upload_results_key(project.id)) or []
    if not rows:
        return
    titles = library.project_titles()
    for index, row in enumerate(rows):
        status = row["status"]
        if status == "created":
            st.success(f"已解析并保存：{row['name']}（{row['pages']} 页）")
        elif status == "library":
            owner = titles.get(row.get("library_project_id", ""), "另一个项目")
            st.info(
                f"{row['name']}：这篇文献已经在文献库里（{owner}里用过），"
                "没有重复入库。要在这个项目里用，点下面的「加入本项目」。"
            )
            if st.button(
                f"把「{_short_title(row['title'])}」加入本项目",
                key=f"add_from_upload_{index}_{row['paper_id']}",
            ):
                source = repo.get(Paper, row["paper_id"])
                if source is None:
                    st.warning("这篇文献已经不在库里了，请刷新后重试。")
                else:
                    paper, created = library.attach_to_project(project.id, source)
                    st.session_state["selected_paper_id"] = paper.id
                    st.session_state["selected_paper_project"] = project.id
                    st.session_state["_library_notice"] = (
                        "success" if created else "info",
                        "已加入本项目" if created else "本项目已经有这篇文献了。",
                    )
                    st.rerun()
        elif status == "doi":
            st.info(f"{row['name']}：本项目已有同一篇文献（DOI 相同），没有重复入库。")
        else:
            st.info(
                f"{row['name']}：本项目已有这份文件（内容完全相同），没有重复入库。"
            )
    if st.button("清除以上提示", key=f"clear_upload_{project.id}"):
        st.session_state[_upload_results_key(project.id)] = []
        st.rerun()


def _short_title(text: str, limit: int = 40) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _upload_block(project: Project) -> None:
    """上传入口（非双栏模式直接展示；双栏模式由 _render_with_workspace 包进展开区）。

    解析与入库发生在**本函数内**，而文献列表在本函数之后才读取，
    所以新文献会出现在同一次渲染里——不需要、也不应该再 ``st.rerun()``。
    """
    parse_ok, parse_detail = pymupdf_status()
    if not parse_ok:
        st.error(
            "**本机缺少 PDF 解析库，上传后会解析失败。**\n\n"
            f"运行本应用的 Python：`{sys.executable}`\n\n"
            f"修复命令：`{sys.executable} -m pip install pymupdf`\n\n"
            "注意必须装进这个环境，装到别的 Python 里不生效。"
        )
    uploaded = st.file_uploader(
        f"上传 PDF（一次最多 {MAX_FILES} 篇）",
        type=["pdf"],
        accept_multiple_files=True,
        key=f"uploader_{project.id}",
        help=(
            f"本地解析，不需要大模型也不需要联网（PyMuPDF {parse_detail}）。"
            "同一份文件或同一篇 DOI 不会重复入库；已在别的项目里用过的文献，"
            "可以直接从文献库加入本项目。"
        ),
    )

    if uploaded:
        if len(uploaded) > MAX_FILES:
            st.error(f"一次最多上传 {MAX_FILES} 篇，当前选了 {len(uploaded)} 篇。")
        else:
            done = st.session_state.setdefault(_uploads_state_key(project.id), {})
            digests = [hashlib.sha256(bytes(item.getbuffer())).hexdigest() for item in uploaded]
            for index in select_new_uploads(done, digests):
                item = uploaded[index]
                try:
                    result = _ingest_upload(project, item)
                except Exception as exc:  # 失败要显示真实原因，不吞掉
                    st.error(f"解析 {item.name} 失败：{exc}")
                    log.exception("解析 PDF 失败：%s", item.name)
                    continue
                _record_upload_result(project, item.name, result)

    _render_upload_results(project)


def _attach_pdf_uploader(project: Project, paper: Paper) -> None:
    """One targeted upload control for filling an existing Paper record."""
    uploaded = st.file_uploader(
        "上传当前论文 PDF",
        type=["pdf"],
        accept_multiple_files=False,
        key=f"attach_pdf_{paper.id}",
    )
    if uploaded is None:
        return
    digest = hashlib.sha256(bytes(uploaded.getbuffer())).hexdigest()
    processed_key = f"_attach_pdf_digest_{paper.id}"
    if st.session_state.get(processed_key) == digest:
        return
    try:
        changed = attach_pdf_bytes_to_paper(
            paper,
            uploaded.name,
            bytes(uploaded.getbuffer()),
        )
    except Exception as exc:  # noqa: BLE001 - show the real parse/DOI error
        st.error(f"上传失败：{exc}")
        return
    st.session_state[processed_key] = digest
    st.session_state["selected_paper_id"] = changed.id
    st.session_state["selected_paper_project"] = project.id
    st.session_state["_library_notice"] = (
        "success",
        "PDF 已附加到当前文献，可以开始阅读和 AI 分析。",
    )
    st.rerun()


def _missing_pdf_actions(project: Project, paper: Paper | None) -> None:
    """Actionable replacement for the old no-local-PDF dead end."""
    with st.container(key="rp_missing_pdf_actions"):
        st.markdown("### 打开当前论文")
        if paper is None:
            st.warning("没有找到对应的文献记录，请从左侧文献库重新选择。")
            return
        title = repair.display_title(paper)
        external_url = links.paper_external_url(paper)
        if external_url:
            st.link_button(
                title[:100],
                external_url,
                icon=":material/open_in_new:",
                help="在来源网站阅读或下载论文",
                width="stretch",
            )
        else:
            st.markdown(f"**{theme.esc(title)}**")
            st.caption("暂无可用的外部阅读链接。")
        _attach_pdf_uploader(project, paper)



def render(project: Project) -> None:
    # 阅读模式：左原文 + 右 AI 解读，顶部两行固定（pdf_workspace 负责布局）
    if pdf_workspace.visible(project.id):
        papers_for_header = repo.list_all(Paper, project.id)
        if not papers_for_header:
            # 阅读模式下一篇都没有时，上传入口不能跟着消失——否则这里是个死胡同。
            st.info("还没有文献。先上传第一篇 PDF，或从文献库加入已有文献。")
            _upload_block(project)
            library_panel.render(project, height=320)
            return
        _ensure_selection(project, papers_for_header)
        relations_for_header = repo.list_paper_relations(project.id)
        pdf_workspace.render(
            project,
            lambda: _reading_right_pane(project, papers_for_header, relations_for_header),
            header_extra=_reading_view_switch,
            missing_pdf_render=_missing_pdf_actions,
        )
        return

    papers = repo.list_all(Paper, project.id)
    relations = repo.list_paper_relations(project.id)
    if papers:
        _ensure_selection(project, papers)

    def _detail_or_empty() -> None:
        if st.session_state.get("_show_screening_candidates"):
            screening_page.render(project)
            return
        if papers:
            _detail(project, papers, relations)
        else:
            with st.container(key="rp_main_toolbar"):
                row = st.columns([9, 1], vertical_alignment="center")
                row[0].markdown("**暂无选中文献**")
                with row[1]:
                    _literature_more_menu(project)
            st.info("还没有文献。可以从左侧文献库上传，或加入库里已有的文献。")

    def _collapsed_rail() -> None:
        st.button(
            "",
            key=f"expand_literature_rail_{project.id}",
            icon=":material/keyboard_double_arrow_right:",
            help="展开文献库",
            on_click=lambda: split_layout.set_collapsed("literature", False),
        )

    def _content() -> None:
        split_layout.render(
            workbench="literature",
            index_title=f"文献库（{len(papers)}）",
            index_caption="",
            index_render=lambda: _project_rail(project, papers, relations),
            detail_render=_detail_or_empty,
            index_height=INDEX_HEIGHT,
            index_side="left",
            index_controls_label="项目栏",
            index_width_default=320,
            collapsed_rail_render=_collapsed_rail,
            show_header=False,
        )

    pdf_workspace.render(project, _content)


# ---------------------------------------------------------------------------
# 中栏：文献目录（可收起、可搜索、可筛选、独立滚动）
# ---------------------------------------------------------------------------


def _paper_pref_key(project_id: str) -> str:
    """每个项目各记各的"上次看的是哪篇"，切回项目时不会落在别的文献上。"""
    return f"paper_for_{project_id}"


def _ensure_selection(project: Project, papers: list[Paper]) -> None:
    """保证当前选中的文献属于当前项目。

    切换项目后 session_state 里可能还留着上一个项目的文献 ID。跨项目的文献
    一旦被渲染进详情区就是实打实的错内容，所以这里在渲染前统一纠偏：
    能恢复就恢复本项目上次看的那篇，否则落到本项目默认的一篇。
    """
    ids = {paper.id for paper in papers}
    current = st.session_state.get("selected_paper_id")
    owner = st.session_state.get("selected_paper_project")
    if owner == project.id and current in ids:
        return

    remembered = browser_prefs.get(_paper_pref_key(project.id))
    if remembered in ids:
        chosen = remembered
    else:
        # 默认优先用户主动上传的文献，而不是一进来就是候选
        mine = [paper for paper in papers if paper.origin != "openalex"]
        chosen = (mine or papers)[0].id

    st.session_state["selected_paper_id"] = chosen
    st.session_state["selected_paper_project"] = project.id


def _identity_labels(
    papers: list[Paper], relations: list[PaperRelation]
) -> dict[str, list[str]]:
    """每篇文献的身份标签。

    分类是**项目级**的，不随当前选中项变化——否则列表会在选中时跳动，
    反而更难定位。
    """
    cited = {r.to_paper_id for r in relations if r.relation_type is RelationType.CITES}
    citing = {r.from_paper_id for r in relations if r.relation_type is RelationType.CITES}
    labels: dict[str, list[str]] = {}
    for paper in papers:
        tags: list[str] = []
        if paper.origin != "openalex":
            tags.append("我的论文")
        if paper.id in cited:
            tags.append("参考文献")
        if paper.id in citing:
            tags.append("施引论文")
        if paper.origin == "openalex" and paper.id not in cited and paper.id not in citing:
            tags.append("待筛选候选")
        labels[paper.id] = tags or ["待筛选候选"]
    return labels


def _catalog_sort_key(paper: Paper):
    """我的论文优先，其次年份倒序，再次标题——让用户上传的排在前面。"""
    year = int(paper.year) if str(paper.year).isdigit() else 0
    return (0 if paper.origin != "openalex" else 1, -year, (paper.title or "").lower())


def _rail_state(project_id: str) -> dict:
    key = f"_lit_rail_state_{project_id}"
    state = st.session_state.get(key)
    if not isinstance(state, dict):
        state = {"selected": set(), "epoch": 0}
        st.session_state[key] = state
    state.setdefault("selected", set())
    state.setdefault("epoch", 0)
    return state


def _rail_toggle(project_id: str, paper_id: str, widget_key: str) -> None:
    state = _rail_state(project_id)
    if st.session_state.get(widget_key):
        state["selected"].add(paper_id)
    else:
        state["selected"].discard(paper_id)


def _rail_clear_selection(project_id: str) -> None:
    state = _rail_state(project_id)
    state["selected"] = set()
    state["epoch"] = int(state.get("epoch", 0)) + 1


def _rail_select_many(project_id: str, paper_ids: list[str]) -> None:
    """Programmatically select visible rows and rebuild checkbox widgets."""
    state = _rail_state(project_id)
    state["selected"] = set(paper_ids)
    state["epoch"] = int(state.get("epoch", 0)) + 1


def _rail_selected(project_id: str, papers: list[Paper]) -> list[str]:
    ids = {paper.id for paper in papers}
    return sorted(_rail_state(project_id)["selected"] & ids)


def _rail_notice() -> None:
    notice = st.session_state.pop("_library_notice", None)
    if notice:
        kind, text = notice
        {"success": st.success, "info": st.info, "error": st.error}[kind](text)


def _literature_more_menu(project: Project) -> None:
    with st.popover("⋯", help="更多", width="stretch"):
        collapsed = split_layout.is_collapsed("literature")
        st.button(
            "显示文献管理" if collapsed else "隐藏文献管理",
            key=f"toggle_literature_manager_{project.id}",
            width="stretch",
            on_click=lambda: split_layout.set_collapsed(
                "literature", not collapsed
            ),
        )


def _rail_row(
    project: Project,
    paper: Paper,
    tags: list[str],
) -> None:
    state = _rail_state(project.id)
    selected = paper.id in state["selected"]
    active = st.session_state.get("selected_paper_id") == paper.id
    title = repair.display_title(paper)[:58]
    row_key = (
        f"rp_library_row_selected_{paper.id}"
        if selected
        else ("rp_index_row_selected" if active else f"rp_row_{paper.id}")
    )
    with st.container(key=row_key):
        row = st.columns([0.32, 5.55, 0.63], vertical_alignment="center")
        widget_key = (
            f"lit_sel_{project.id}_{int(state.get('epoch', 0))}_{paper.id}"
        )
        row[0].checkbox(
            f"选择 {title}",
            value=selected,
            key=widget_key,
            label_visibility="collapsed",
            on_change=_rail_toggle,
            args=(project.id, paper.id, widget_key),
        )
        label = f"{'▸ ' if active else ''}{paper.year or '????'}　{title}"
        if row[1].button(
            label,
            key=f"pick_{paper.id}",
            type="tertiary",
            width="stretch",
        ):
            st.session_state["selected_paper_id"] = paper.id
            st.session_state["selected_paper_project"] = paper.project_id
            browser_prefs.set(**{_paper_pref_key(paper.project_id): paper.id})
            st.rerun()
        edit_key = f"_rail_edit_{project.id}_{paper.id}"
        if row[2].button(
            "",
            key=f"edit_rail_{project.id}_{paper.id}",
            icon=":material/edit:",
            help="编辑整理备注",
            width="stretch",
        ):
            st.session_state[edit_key] = not bool(st.session_state.get(edit_key))
            st.rerun()
        bits: list[str] = []
        if paper.library_categories:
            bits.append("分类：" + "、".join(paper.library_categories[:3]))
        if tags:
            bits.append("・".join(tags))
        if paper.verification_status is VerificationStatus.CONFLICT:
            bits.append("元数据冲突")
        elif paper.verification_status is VerificationStatus.PARTIAL:
            bits.append("字段部分一致")
        if paper.library_note:
            bits.append(paper.library_note[:32])
        if bits:
            st.caption("　｜　".join(bits))
        if st.session_state.get(edit_key):
            note = st.text_area(
                "整理备注",
                value=paper.library_note,
                key=f"rail_inline_note_{project.id}_{paper.id}",
                placeholder="例如：优先精读、方法可复用、待核验",
                height=76,
            )
            save_cols = st.columns([1, 1, 3])
            if save_cols[0].button(
                "保存",
                key=f"rail_inline_save_{project.id}_{paper.id}",
                type="primary",
            ):
                library.organize_papers(project.id, [paper.id], note=note)
                st.session_state[edit_key] = False
                st.session_state["_library_notice"] = (
                    "success", "整理备注已更新"
                )
                st.rerun()
            if save_cols[1].button(
                "取消",
                key=f"rail_inline_cancel_{project.id}_{paper.id}",
            ):
                st.session_state[edit_key] = False
                st.rerun()


def _batch_delete(project: Project, selected_ids: list[str]) -> None:
    impacts = []
    for paper_id in selected_ids:
        paper = repo.get(Paper, paper_id)
        if paper is not None and paper.project_id == project.id:
            impacts.append(library.deletion_impact(paper))
    relation_count = sum(int(item.get("relations") or 0) for item in impacts)
    claim_count = sum(int(item.get("claims") or 0) for item in impacts)
    st.markdown(f"**将处理 {len(selected_ids)} 篇文献**")
    st.caption(
        f"当前项目会清理引用关系 {relation_count} 条、AI 条目 {claim_count} 条。"
    )
    scope = st.radio(
        "删除范围",
        ["仅从当前项目移除", "从所有项目彻底删除"],
        key=f"rail_delete_scope_{project.id}",
    )
    delete_file = st.checkbox(
        "同时删除本地 PDF（仅当没有其它记录使用时）",
        value=False,
        key=f"rail_delete_file_{project.id}",
    )
    confirm = st.checkbox(
        "我确认执行这次批量删除",
        key=f"rail_delete_confirm_{project.id}",
    )
    if st.button(
        "确认删除",
        key=f"rail_delete_go_{project.id}",
        type="primary",
        disabled=not confirm,
        width="stretch",
    ):
        if scope == "仅从当前项目移除":
            result = library.remove_many(
                project.id, selected_ids, delete_file=delete_file
            )
            text = (
                f"已从本项目移除 {result['records']} 篇；清理关系 "
                f"{result['relations']} 条、AI 条目 {result['claims']} 条。"
            )
        else:
            entries = {}
            for paper_id in selected_ids:
                paper = repo.get(Paper, paper_id)
                entry = library.entry_for(paper) if paper is not None else None
                if entry is not None:
                    entries[entry.key] = entry
            deleted = 0
            for entry in entries.values():
                library.purge_entry(entry, delete_file=delete_file)
                deleted += 1
            text = f"已从所有项目彻底删除 {deleted} 篇文献。"
        st.session_state["selected_paper_id"] = ""
        _rail_clear_selection(project.id)
        st.session_state["_library_notice"] = ("info", text)
        st.rerun()


def _batch_actions(
    project: Project,
    selectable_papers: list[Paper],
    selected_ids: list[str],
) -> None:
    """Sticky batch workspace for selection, classification and deletion."""
    with st.container(key="rp_library_batch_workspace"):
        head = st.columns([4.2, 1], vertical_alignment="center")
        head[0].markdown("**批量整理**")
        head[1].button(
            "全选",
            key=f"rail_select_all_{project.id}",
            width="stretch",
            disabled=not selectable_papers,
            on_click=_rail_select_many,
            args=(project.id, [paper.id for paper in selectable_papers]),
        )
        st.caption(f"已选 {len(selected_ids)} 篇")
        if not selected_ids:
            return

        actions = st.columns([0.62, 1, 1])
        with actions[0]:
            library_mover.render(project, selected_ids)
        with actions[1].popover("分类", width="stretch"):
            mode = st.segmented_control(
                "分类操作",
                ["加入", "移出", "替换"],
                default="加入",
                key=f"rail_cat_mode_{project.id}",
            )
            mode = mode or "加入"
            existing = library.categories_in_project(project.id)
            picked = st.multiselect(
                "我的分类",
                existing,
                key=f"rail_cat_existing_{project.id}",
                placeholder="选择已有分类" if existing else "还没有分类",
                disabled=not existing,
            )
            values = st.text_input(
                "新建分类（逗号分隔）",
                key=f"rail_cat_values_{project.id}",
                placeholder="例如：核心方法, 待精读",
            )
            typed = [
                item.strip()
                for item in values.replace("，", ",").split(",")
                if item.strip()
            ]
            parsed = list(dict.fromkeys([*picked, *typed]))
            if st.button(
                "保存分类",
                key=f"rail_cat_apply_{project.id}",
                disabled=not parsed,
                width="stretch",
            ):
                kwargs = {}
                if mode == "加入":
                    kwargs["add_categories"] = parsed
                elif mode == "移出":
                    kwargs["remove_categories"] = parsed
                else:
                    kwargs["replace_categories"] = parsed
                library.organize_papers(project.id, selected_ids, **kwargs)
                st.session_state["_library_notice"] = (
                    "success", f"已更新 {len(selected_ids)} 篇文献的分类"
                )
                _rail_clear_selection(project.id)
                st.rerun()

        with actions[2].popover("删除", width="stretch"):
            _batch_delete(project, selected_ids)


def _project_rail(
    project: Project,
    papers: list[Paper],
    relations: list[PaperRelation],
) -> None:
    labels = _identity_labels(papers, relations)
    _rail_notice()
    head = st.columns([5, 1], vertical_alignment="center")
    head[0].markdown("**文献库**")
    head[1].button(
        "",
        key=f"collapse_literature_rail_{project.id}",
        icon=":material/keyboard_double_arrow_left:",
        help="收起文献库",
        on_click=lambda: split_layout.set_collapsed("literature", True),
    )
    panel_actions = st.columns(2)
    with panel_actions[0].popover(
        "上传新文献", icon=":material/upload:", width="stretch"
    ):
        _upload_block(project)
    with panel_actions[1].popover(
        f"全库 {len(library.entries())}", width="stretch"
    ):
        library_panel.render(project, height=420, expanded=True)
    theme.markdown(theme.hidden_compat("一条文献一行；勾选后批量整理"))

    query = st.text_input(
        "搜索题目 / 作者 / DOI / 分类",
        key=f"search_{project.id}",
        placeholder="搜索题目、作者、DOI 或分类",
        label_visibility="collapsed",
    )
    custom_categories = library.categories_in_project(project.id)
    filter_options = list(dict.fromkeys([
        "全库", *CATEGORIES[1:], *custom_categories,
    ]))
    choice = st.selectbox(
        "分类筛选",
        filter_options,
        key=f"rail_filter_{project.id}",
        label_visibility="collapsed",
    )
    st.session_state["_show_screening_candidates"] = choice == "待筛选候选"

    needle = query.strip().lower()
    filtered = []
    for paper in papers:
        category_hit = (
            choice == "全库"
            or choice in labels[paper.id]
            or choice in (paper.library_categories or [])
        )
        haystack = " ".join([
            paper.title or "", paper.title_raw or "", paper.authors or "",
            paper.doi or "", " ".join(paper.library_categories or []),
            paper.library_note or "",
        ]).lower()
        if category_hit and (not needle or needle in haystack):
            filtered.append(paper)
    filtered.sort(key=_catalog_sort_key)
    limit_key = f"limit_{project.id}"
    shown = int(st.session_state.get(limit_key, CATALOG_PAGE))
    st.caption(f"匹配 {len(filtered)} 篇｜当前显示 {min(shown, len(filtered))} 篇")
    visible = filtered[:shown]
    _batch_actions(
        project,
        filtered,
        _rail_selected(project.id, papers),
    )
    with st.container(height=330, border=False, key="rp_catalog"):
        if not filtered:
            st.caption("没有匹配的文献。换个关键词或分类试试。")
        for paper in visible:
            _rail_row(project, paper, labels[paper.id])
    if len(filtered) > shown:
        if st.button(
            f"显示更多（还有 {len(filtered) - shown} 篇）",
            key=f"more_{project.id}",
            width="stretch",
        ):
            st.session_state[limit_key] = shown + CATALOG_PAGE
            st.rerun()

# ---------------------------------------------------------------------------
# 右栏：单篇详情（一次只渲染当前选中的那篇）
# ---------------------------------------------------------------------------


def _detail(project: Project, papers: list[Paper], relations: list[PaperRelation]) -> None:
    ids = {paper.id for paper in papers}
    current = st.session_state.get("selected_paper_id")
    if current not in ids:
        # 默认优先用户主动上传的文献，而不是一进来就是候选
        mine = [paper for paper in papers if paper.origin != "openalex"]
        current = (mine or papers)[0].id
        st.session_state["selected_paper_id"] = current

    paper = repo.get(Paper, current)
    if paper is None or paper.project_id != project.id:
        # 项目隔离：切项目后旧选中项可能还留在 session_state 里
        st.info("选中的文献不属于当前项目，请从左侧项目栏重新选择。")
        return

    pending = sum(
        1 for relation in relations
        if relation.review_status is ReviewStatus.PENDING
        and not _is_official_citation(relation)
        and paper.id in (relation.from_paper_id, relation.to_paper_id)
    )

    # 外层容器（key="rp_detail"）由 split_layout 提供，这里不再包一层，避免重复 key
    _paper_summary_card(project, paper)
    _headline_status(paper, pending)
    # 论文大纲（摘要 / 章节 / 结论 / 关联文献）属于"这篇论文怎么看"，
    # 放在文献工作台内部，不占全局侧栏。
    paper_outline.render_panel(project, paper)
    default_tab = st.session_state.pop("_detail_default_tab", "概览")
    tab_overview, tab_relations = st.tabs(
        ["概览", "引用"],
        default=default_tab if default_tab in {"概览", "引用"} else "概览",
    )
    with tab_overview:
        _primary_tab(paper, project)
    with tab_relations:
        citation_view = _citation_view_switch(paper)
        _citation_fetch_block(paper)
        _relations_block(paper, citation_view)


def _paper_summary_card(project: Project, paper: Paper) -> None:
    """豆包式摘要卡片：标题、作者与元数据直接置顶，操作收进右上角 ⋯ 菜单。"""
    title = repair.detail_title(paper)
    authors = sanitize_for_display(paper.authors)
    meta = [item for item in (paper.year,) if item]
    if paper.doi:
        meta.append(f"DOI {paper.doi}")
    has_pdf = paper_paths.resolve_local_path(paper.local_path) is not None
    external_url = links.paper_external_url(paper)
    with st.container(key="rp_paper_card"):
        head = st.columns([11, 1], vertical_alignment="center")
        with head[0]:
            theme.markdown(
                f'<div class="rp-paper-card__title">{theme.esc(title)}</div>'
                f'<div class="rp-paper-card__meta">{theme.esc(" · ".join(meta + ([authors] if authors else [])))}</div>'
            )
        with head[1]:
            if has_pdf:
                if st.button(
                    "",
                    key=f"open_pdf_{paper.id}",
                    icon=":material/open_in_new:",
                    help="打开 PDF",
                    width="stretch",
                ):
                    pdf_workspace.open_document(project.id, paper.id, 1, "")
                    st.rerun()
            elif external_url:
                st.link_button(
                    "",
                    external_url,
                    icon=":material/open_in_new:",
                    help="打开文献网站",
                    width="stretch",
                )
            else:
                st.button(
                    "",
                    key=f"open_pdf_{paper.id}",
                    icon=":material/open_in_new:",
                    disabled=True,
                    help="没有本地原文，也没有可用的外部链接",
                    width="stretch",
                )
    # Compatibility controls remain available to existing automation but are
    # visually removed; the right rail's own arrow is the only management toggle.
    collapsed = split_layout.is_collapsed("literature")
    with st.container(key="rp_compat_controls"):
        st.button(
            "显示文献管理" if collapsed else "隐藏文献管理",
            key=f"card_toggle_lit_{project.id}",
            on_click=lambda: split_layout.set_collapsed(
                "literature", not collapsed
            ),
        )


def _paper_card_menu(project: Project, paper: Paper) -> None:
    has_pdf = paper_paths.resolve_local_path(paper.local_path) is not None
    if st.button(
        "打开 PDF",
        key=f"open_pdf_{paper.id}",
        icon=":material/picture_as_pdf:",
        disabled=not has_pdf,
        width="stretch",
        help=None if has_pdf else "这篇文献没有本地原文",
    ):
        pdf_workspace.open_document(project.id, paper.id, 1, "")
        st.rerun()
    st.divider()
    collapsed = split_layout.is_collapsed("literature")
    if st.button(
        "显示文献管理" if collapsed else "隐藏文献管理",
        key=f"card_toggle_lit_{project.id}",
        width="stretch",
    ):
        split_layout.set_collapsed("literature", not collapsed)
        st.rerun()


def _paper_admin_block(paper: Paper) -> None:
    """这篇文献的归属与移除。

    删除是不可逆动作，所以先把"会连带删掉什么、别的项目受不受影响"写清楚，
    再要求勾选确认。想让它同时出现在别的项目里，用文献库的「加入本项目」，
    不需要在这里删。
    """
    entry = library.entry_for(paper)
    owners = entry.project_ids if entry else [paper.project_id]
    titles = library.project_titles()
    impact = library.deletion_impact(paper)

    with st.expander("归属与移除"):
        st.caption(
            "这篇文献在 "
            + "、".join(titles.get(pid, "某个项目") for pid in owners)
            + f" 里有记录（共 {len(owners)} 个项目）。"
        )
        st.caption(
            f"从本项目移除会连带清理：引用关系 {impact['relations']} 条、"
            f"AI 条目 {impact['claims']} 条、外部查询记录 {impact['source_records']} 条。"
        )
        delete_file = st.checkbox(
            "同时删除本地 PDF",
            value=False,
            key=f"rm_file_{paper.id}",
            help="仅当没有别的项目还在用这份文件时才真的删除。",
        )
        confirm = st.checkbox(
            "我确认从本项目移除这篇文献", key=f"rm_ok_{paper.id}"
        )
        if st.button(
            "从本项目移除",
            key=f"rm_{paper.id}",
            type="primary",
            disabled=not confirm,
        ):
            result = library.remove_paper(paper, delete_file=bool(delete_file))
            note = (
                f"已从本项目移除：关系 {result['relations']} 条、AI 条目 "
                f"{result['claims']} 条一并清理。"
            )
            if result.get("file_deleted"):
                note += "本地 PDF 与按页正文已删除。"
            elif delete_file:
                note += "本地文件仍有其它记录在用，未删除。"
            st.session_state["_library_notice"] = ("info", note)
            st.session_state.pop("selected_paper_id", None)
            st.session_state.pop("selected_paper_project", None)
            st.rerun()


def _manual_relation_block(project: Project) -> None:
    """手工建立语义关系：模型/人都可以提，但必须有证据位置，且要人工确认。"""
    papers = repo.list_all(Paper, project.id)
    st.subheader("手工建立关系")
    if len(papers) < 2:
        st.caption("项目里至少要有两篇文献才能建立关系。")
        return
    labels = {f"{p.year or '????'}｜{(p.title or p.id)[:60]}": p for p in papers}
    semantic_types = [t for t in RelationType if t is not RelationType.CITES]
    with st.expander("新增一条方法关系（扩展 / 对比 / 替代 / 提出不同方法）"):
        left = st.selectbox("起点文献（施动方）", list(labels), key=f"from_{project.id}")
        right = st.selectbox("终点文献", list(labels), key=f"to_{project.id}")
        relation_type = st.selectbox(
            "关系类型", semantic_types,
            format_func=lambda t: _RELATION_LABEL[t], key=f"type_{project.id}",
        )
        locator = st.text_input(
            "证据位置（必填）", key=f"loc_{project.id}",
            placeholder="例如：第 7 页 §4.2 或参考文献 [12]",
        )
        note = st.text_input("备注", key=f"mnote_{project.id}")
        if st.button("保存关系", key=f"save_rel_{project.id}"):
            if not locator.strip():
                st.error("必须给出证据位置——没有出处的语义关系不得入库。")
            elif labels[left].id == labels[right].id:
                st.error("起点与终点不能是同一篇文献。")
            else:
                relation = PaperRelation(
                    id=repo.new_id("prel"),
                    project_id=project.id,
                    from_paper_id=labels[left].id,
                    to_paper_id=labels[right].id,
                    relation_type=relation_type,
                    source=RelationSource.USER,
                    locator=locator.strip(),
                    review_note=note.strip(),
                    verified=False,
                    review_status=ReviewStatus.PENDING,
                )
                _, created = repo.add_paper_relation(relation)
                if created:
                    st.success("已保存。人工录入的语义关系默认未核实，需在文献卡片里确认。")
                else:
                    st.info("同样的关系已存在，未重复创建。")
                st.rerun()


READING_VIEWS = ("AI 解读", "论文详情")


def _reading_view_switch(slot) -> None:
    """阅读页顶部第一行右侧的视图切换：默认 AI 解读，随时可回到论文详情。"""
    with slot:
        st.segmented_control(
            "右侧内容", list(READING_VIEWS), key="literature_reading_view",
            default=READING_VIEWS[0], label_visibility="collapsed",
        )


def _reading_right_pane(project: Project, papers: list[Paper],
                        relations: list[PaperRelation]) -> None:
    """阅读页右栏：AI 解读（默认）或论文详情。

    右栏默认是导览而不是目录——目录在左栏（原文）与浏览模式里各有一份，
    在阅读时再显示一遍索引对"读懂论文"没有帮助。
    """
    request = pdf_workspace.current(project.id) or {}
    paper = repo.get(Paper, str(request.get("paper_id") or ""))
    if paper is None or paper.project_id != project.id:
        _detail(project, papers, relations)
        return
    view = st.session_state.get("literature_reading_view") or READING_VIEWS[0]
    if view == READING_VIEWS[1]:
        # 阅读页里的「论文详情」必须就是**正在读的那篇**：目录选中项可能还停在
        # 别的文献上（例如刚加进来的候选），那样详情会张冠李戴。
        st.session_state["selected_paper_id"] = paper.id
        st.session_state["selected_paper_project"] = project.id
        _detail(project, papers, relations)
        return
    reading_guide.render(project, paper, _http_client())

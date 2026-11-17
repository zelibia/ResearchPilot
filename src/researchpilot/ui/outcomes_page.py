"""工作台 4：科研成果。

报告只写库里真实存在的内容：文献证据带逐字引文与页码，实验数字与图只来自
执行成功的运行记录。没有真实数据的章节写「未执行 / 尚无证据」，不补示意图、
不编结论。报告可以导出成 Markdown。
"""

from __future__ import annotations

import json

import streamlit as st

from ..exports import reporting
from .. import display_text
from ..storage import repository as repo
from ..storage.models import (
    AssessmentStatus,
    ExperimentRun,
    Goal,
    GoalAssessment,
    Project,
    RunStatus,
)
from . import developer, pdf_reader, pdf_workspace, split_layout, theme

INDEX_HEIGHT = 560

ALL_SECTIONS = "__all__"


def _page_of(block: dict) -> int:
    """从「PDF · 第 N 页」标签里取回页码，用于打开阅读器。"""
    import re

    match = re.search(r"第\s*(\d+)\s*页", str(block.get("page_label", "")))
    return int(match.group(1)) if match else 0


def _index_pane(doc: reporting.ReportDoc) -> str:
    """左栏：章节目录与导出。返回当前选中的章节 key。"""
    if doc.is_empty:
        theme.empty_state("暂无科研成果")
        return ALL_SECTIONS

    options = [ALL_SECTIONS] + [section.key for section in doc.sections]
    labels = {ALL_SECTIONS: "查看完整报告"}
    for section in doc.sections:
        labels[section.key] = f"{section.title}　{'有内容' if section.has_content else '未执行'}"

    key = f"report_section_{doc.project_id}"
    if st.session_state.get(key) not in options:
        st.session_state[key] = ALL_SECTIONS
    with st.container(key="rp_toc"):
        choice = st.radio(
            "报告章节", options, format_func=lambda item: labels.get(item, item),
            key=key, label_visibility="collapsed",
        )

    st.download_button(
        "导出 Markdown 报告",
        data=reporting.to_markdown(doc),
        file_name=f"{doc.project_title}研究成果报告.md",
        mime="text/markdown",
        key=f"report_download_{doc.project_id}",
    )
    return choice


def _render_section(section: reporting.ReportSection, project_id: str) -> None:
    st.markdown(f"**{section.title}**")
    if not section.has_content:
        theme.empty_state(section.empty_reason or reporting.NO_EVIDENCE)
        return
    if section.blocks:
        # 结构化渲染：每条证据一个块；同一节的证据按钮**一次性渲染**，
        # 每条按钮用 claim_id（或区块序号）作稳定标识，避免重复 key。
        evidence_rows: list[dict] = []
        for index, block in enumerate(section.blocks):
            if block.get("kind") == "paper":
                st.markdown(f"**《{block['title']}》**")
                continue
            theme.markdown(
                f'<div style="font-size:var(--rp-text-sm)">{theme.esc(block.get("text", ""))}</div>'
                + theme.chip_row([
                    (block.get("certainty_label", "未核实"),
                     "evidence" if block.get("verified") else "warn"),
                ])
            )
            evidence_rows.append({
                "paper_id": block.get("paper_id", ""),
                "page": _page_of(block),
                "quote": block.get("quote", ""),
                "verified": bool(block.get("verified")),
                "label": block.get("page_label", "打开原文"),
                "key": block.get("claim_id") or f"block{index}",
            })
        if evidence_rows:
            pdf_reader.evidence_buttons(
                evidence_rows, project_id=project_id,
                key_prefix=f"report_pdf_{section.key}",
            )
        return
    for line in section.lines:
        if line.strip():
            st.markdown(line)
        else:
            st.write("")


def _detail_pane(doc: reporting.ReportDoc, choice: str) -> None:
    if doc.is_empty:
        theme.empty_state("暂无科研成果")
        return

    theme.page_title("研究成果")
    theme.markdown(
        theme.chip_row([
            (f"生成于 {doc.generated_at}", ""),
            (f"{len(doc.sections)} 个章节", ""),
            (f"{len(doc.gaps)} 个章节尚无真实数据", "warn" if doc.gaps else ""),
        ])
    )
    if choice == ALL_SECTIONS:
        for section in doc.sections:
            _render_section(section, doc.project_id)
            st.divider()
        if doc.gaps:
            st.warning("尚未完成的部分：" + "；".join(doc.gaps))
        return

    for section in doc.sections:
        if section.key == choice:
            _render_section(section, doc.project_id)
            return


def render(project: Project) -> None:
    claims = repo.claims_of_project(project.id)
    runs = repo.list_all(ExperimentRun, project.id)
    goals = repo.list_all(Goal, project.id)

    # 紧凑状态条替代一排大数字卡片（方案 C）
    theme.status_strip([
        (f"文献证据 {len(claims)}", "evidence" if claims else ""),
        (f"实验记录 {len(runs)}", "ok" if runs else ""),
        (f"研究目标 {len(goals)}", ""),
    ])

    doc = reporting.build(project)
    def _content() -> None:
        if pdf_workspace.visible(project.id) and not doc.is_empty:
            options = [ALL_SECTIONS] + [section.key for section in doc.sections]
            labels = {ALL_SECTIONS: "查看完整报告"}
            for section in doc.sections:
                labels[section.key] = f"{section.title}　{'有内容' if section.has_content else '未执行'}"
            key = f"report_section_{project.id}"
            if st.session_state.get(key) not in options:
                st.session_state[key] = ALL_SECTIONS
            st.selectbox("报告章节", options, format_func=lambda item: labels[item], key=key)
            st.download_button(
                "导出 Markdown 报告", data=reporting.to_markdown(doc),
                file_name=f"{doc.project_title}研究成果报告.md", mime="text/markdown",
                key=f"report_download_compact_{project.id}",
            )
            _detail_pane(doc, st.session_state.get(key, ALL_SECTIONS))
            return
        split_layout.render(
            workbench="outcomes",
            index_title="",
            index_render=lambda: _index_pane(doc),
            detail_render=lambda: _detail_pane(doc, st.session_state.get(f"report_section_{project.id}", ALL_SECTIONS)),
            index_height=INDEX_HEIGHT,
            index_side="right",
            index_controls_label="索引",
            collapsed_rail_render=lambda: split_layout.collapsed_rail_button(
                "outcomes", project.id
            ),
        )

    pdf_workspace.render(project, _content)

    _goal_assessment_block(project)

    if developer.enabled():
        _developer_snapshot(project)


_ASSESSMENT_LABEL = {
    AssessmentStatus.DONE: "达成",
    AssessmentStatus.PARTIAL: "部分达成",
    AssessmentStatus.NOT_DONE: "未达成",
    AssessmentStatus.FAILED: "失败",
}


def _shorten(text: str, limit: int) -> str:
    """目标文字里带行内条目编号，展示前先收拾干净再截断。"""
    value = display_text.strip_claim_refs(str(text or "")).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _goal_assessment_block(project: Project) -> None:
    """目标对照：把**真实运行**对着目标判一次。

    判定必须由用户给出——系统不会替你下"目标已达成"的结论。存下来之后，
    报告第五节会显示这次对照以及它依据了哪次运行。
    """
    goals = repo.list_all(Goal, project.id)
    if not goals:
        return
    runs = [run for run in repo.list_all(ExperimentRun, project.id)
            if run.status is RunStatus.SUCCEEDED]
    st.divider()
    theme.markdown(theme.hidden_compat("目标对照"))
    assessments = repo.list_all(GoalAssessment)
    for goal in goals:
        mine = sorted(
            (item for item in assessments if item.goal_id == goal.id),
            key=lambda item: item.created_at,
        )
        st.markdown(f"**{_shorten(goal.statement, 70)}**")
        st.caption(f"判据：{_shorten(goal.acceptance, 110)}")
        if mine:
            latest = mine[-1]
            theme.markdown(theme.chip_row([
                (f"最近判定：{_ASSESSMENT_LABEL.get(latest.status, latest.status.value)}",
                 "ok" if latest.status is AssessmentStatus.DONE else "warn"),
                (f"依据：{', '.join(latest.evidence_ids) or '未关联运行'}", ""),
                (f"记录于 {latest.created_at}", ""),
            ]))
            if latest.reason:
                st.caption(f"理由：{latest.reason}")
            if latest.next_step:
                st.caption(f"下一步：{latest.next_step}")
        else:
            theme.markdown(theme.hidden_compat("还没有做过目标对照。报告里这一节会显示「未评估」。"))

        options = ["（暂不关联运行）"] + [
            f"{run.time_window or '未注明日期'} · {run.id[-6:]}" for run in runs
        ]
        with st.form(key=f"goal_assess_{goal.id}"):
            columns = st.columns([2, 2, 3])
            choice = columns[0].selectbox(
                "依据哪次真实运行", options, key=f"assess_run_{goal.id}",
                help="没有可选运行时说明这个项目还没有成功的实验",
            )
            status = columns[1].radio(
                "达成情况", list(_ASSESSMENT_LABEL), key=f"assess_status_{goal.id}",
                format_func=lambda item: _ASSESSMENT_LABEL[item],
            )
            reason = columns[2].text_input(
                "理由（写明依据的指标）", key=f"assess_reason_{goal.id}",
                placeholder="例如：互相关与叠加已跑通，但频散测量未执行，因此只算部分达成",
            )
            next_step = st.text_input(
                "下一步", key=f"assess_next_{goal.id}",
                placeholder="例如：补齐频散测量后再判一次",
            )
            if st.form_submit_button("保存目标对照", type="primary"):
                run_id = ""
                if choice in options[1:]:
                    run_id = runs[options.index(choice) - 1].id
                if not reason.strip():
                    st.warning("先写一句理由：判定必须有依据，不能只留一个状态。")
                else:
                    repo.save(GoalAssessment(
                        id=repo.new_id("assess"),
                        goal_id=goal.id,
                        status=status,
                        evidence_ids=[run_id] if run_id else [],
                        reason=reason.strip(),
                        next_step=next_step.strip(),
                    ))
                    st.rerun()


def _developer_snapshot(project: Project) -> None:
    """数据库快照与原始 JSON。只在开发者诊断模式下显示。"""
    st.divider()
    st.subheader("项目数据快照")
    st.caption("仅开发者模式可见：用于检查持久化是否生效。")
    snapshot = repo.export_project_json(project.id)
    counts = {k: len(v) for k, v in snapshot.items()}
    st.write(counts)
    st.download_button(
        "下载项目 JSON 快照",
        data=repo.export_project_json_text(project.id),
        file_name=f"{project.id}_snapshot.json",
        mime="application/json",
    )
    with st.expander("查看原始 JSON"):
        st.code(json.dumps(snapshot, ensure_ascii=False, indent=2)[:8000], language="json")

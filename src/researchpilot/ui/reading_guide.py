"""右栏：AI 解读（读懂论文），不是目录。

这一处负责：

1. 三种阅读模式各自使用独立结构：筛选单段、复现步骤、深度七维；
2. 顶部原文校验统计、生成模式、重新生成与具体失败原因；
3. 「第 X 页」角标：显式点击才让左侧 PDF 跳到对应页，浏览右侧不改变左侧；
4. 相关文献核验与加入项目文献库；未核验条目不提供加入入口。

数据来自 `agent/guide.py`（存在 paper.summary 里），本模块只负责渲染和显式动作
（生成、重新生成、加入文献库）。
"""

from __future__ import annotations

import logging

import streamlit as st

from ..agent import guide as guide_mod
from ..agent import guide_prompts
from ..agent import guide_schemas
from ..literature import links
from ..literature.text_quality import sanitize_for_display
from ..storage import repository as repo
from ..storage.models import Paper, Project
from . import assistant, llm_settings, pdf_workspace, theme

log = logging.getLogger(__name__)

VERIFY_LABEL = {
    "openalex": "OpenAlex 已核验",
    "crossref": "Crossref 已核验",
    "unmatched": "未能核实",
    "no_doi": "无 DOI，未能核实",
    "skipped": "本次未核验",
}


# ---------------------------------------------------------------------------
# 生成 / 连接
# ---------------------------------------------------------------------------


@st.dialog("连接 AI 模型")
def _connect_dialog() -> None:
    llm_settings.render(namespace="reading_dialog")


def _run_build(paper: Paper, http_client, mode: str) -> None:
    """流式生成一次导览：阶段、思考、正文分别实时显示，最后一起落库。

    展示的状态都来自**真实事件**：模型什么时候连上、思考收到多少字、正文收到多少字、
    核验了几篇文献——不画假的进度条。思考与正文分开显示；思考只在模型真的返回
    reasoning_content 时才出现。
    """
    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    outcome: guide_mod.GuideOutcome | None = None

    status = st.status("正在生成 AI 解读…", expanded=True)
    thinking_box = st.empty()
    try:
        for event in guide_mod.build_stream(
            paper, llm_settings.client(), mode=mode, http_client=http_client
        ):
            if event.stage == "prepare":
                status.update(label=f"准备文献证据：{event.detail}")
            elif event.stage == "connect":
                status.update(label=f"等待模型响应：{event.detail}")
            elif event.stage == "thinking":
                thinking_parts.append(event.text)
                thinking_box.markdown(_live_markdown("AI 思考中", thinking_parts, "过程性内容"))
                status.update(label=f"AI 正在思考…（已收到 {event.thinking_chars} 字）")
            elif event.stage == "answering":
                answer_parts.append(event.text)
                status.update(label=f"正在生成导览正文…（已收到 {event.answer_chars} 字）")
            elif event.stage == "verify":
                status.update(label=f"核验证据与外部文献：{event.detail}")
            elif event.stage == "retry":
                status.update(label=f"首次响应未通过，自动重试：{event.detail}")
                answer_parts.clear()
            elif event.stage == "fallback":
                status.update(label=f"模型接口兼容处理：{event.detail}")
            elif event.stage in {"done", "failed", "refused"} and event.outcome is not None:
                outcome = event.outcome
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可见终态
        log.exception("生成 AI 解读异常 %s", paper.id)
        outcome = guide_mod.GuideOutcome(
            paper_id=paper.id, status="failed",
            reason=f"生成过程中出现未预期的错误：{type(exc).__name__}: {str(exc)[:200]}",
            thinking_text="".join(thinking_parts),
        )

    if outcome is None:
        outcome = guide_mod.GuideOutcome(
            paper_id=paper.id, status="failed", reason="生成过程没有返回终态",
            thinking_text="".join(thinking_parts),
        )
    if not outcome.thinking_text and thinking_parts:
        outcome.thinking_text = "".join(thinking_parts)
    if outcome.guide:
        status.update(
            label=(
                f"已保存（自动重试 {outcome.attempts - 1} 次）"
                if outcome.attempts > 1 else "已保存"
            ),
            state="complete",
        )
    else:
        status.update(label="生成未完成", state="error")

    # 两件事分开存：终态记录（成功/失败/覆盖情况 + 思考）与导览正文本身。
    record = outcome.to_dict()
    record["mode"] = mode
    if outcome.thinking_text and not outcome.guide:
        # 生成没成功也要把已经收到的思考留下（标注为未完成）
        record["thinking"] = {
            "text": outcome.thinking_text,
            "note": "本次请求未完成，这段是过程性内容，不作为原文证据或结论",
        }
    paper.summary = {**(paper.summary or {}), "reading_guide_record": record}
    if outcome.guide:
        guide_mod.store(paper, outcome.guide)
    else:
        repo.save(paper)
    st.rerun()


def _live_markdown(title: str, parts: list[str], note: str) -> str:
    """流式期间的实时预览（纯文本，不解释 Markdown，免得半截 JSON 乱跳）。"""
    body = "".join(parts)
    tail = body[-1200:]
    return f"**{title}**（{note}）\n\n```text\n{tail}\n```"


def _thinking_block(guide: dict, record: dict | None) -> None:
    """思考记录：默认折叠，随时可展开；与正式导览分开显示。"""
    payload = (guide or {}).get("thinking") or {}
    text = str(payload.get("text") or "")
    note = str(payload.get("note") or "")
    if not text and record:
        payload = record.get("thinking") or {}
        text = str(payload.get("text") or "")
        note = str(payload.get("note") or "")
    if not text:
        return
    with st.expander(f"查看 AI 思考过程（{len(text)} 字）", expanded=False):
        st.caption(
            note or "模型生成的过程性内容：它不是回答，也不作为原文证据或科学结论。"
        )
        st.markdown(f"```text\n{text}\n```")


def _analysis_header(
    paper: Paper,
    project: Project,
    http_client,
    guide: dict | None,
) -> None:
    """AI 解读顶部：标题、生成模式、重新生成与失败原因。"""
    connected = llm_settings.connected()
    ready, reason = guide_mod.readiness(paper)
    record = (paper.summary or {}).get("reading_guide_record") or {}
    existing = guide or {}
    mode_ids = list(guide_prompts.READING_MODE_ORDER)
    label_for_id = dict(guide_prompts.READING_MODE_LABELS)
    id_for_label = {label_for_id[mode_id]: mode_id for mode_id in mode_ids}
    current_mode = str(existing.get("mode") or "deep")
    if current_mode not in mode_ids:
        current_mode = "deep"

    theme.markdown('<div class="rp-insight-title">AI 解读</div>')

    if record and record.get("status") in {"failed", "refused"} and not guide_mod.load(paper):
        level = "warning" if record.get("status") == "refused" else "error"
        detail = record.get("reason") or "生成没有完成"
        getattr(st, level)(detail)

    if not ready:
        st.caption(f"这篇文献暂不能生成导览：{reason}")
        return

    controls = st.columns([3.2, 2.5, 2.2], vertical_alignment="bottom")
    with controls[0]:
        chosen_label = st.selectbox(
            "生成模式",
            list(id_for_label),
            index=list(id_for_label).index(label_for_id[current_mode]),
            key=f"guide_mode_{paper.id}",
        )
    mode = id_for_label.get(chosen_label or label_for_id[current_mode], "deep")
    action_label = (
        "重新生成"
        if guide or record.get("status") in {"failed", "refused"}
        else "生成 AI 解读"
    )
    with controls[2]:
        clicked = st.button(
            action_label,
            key=f"guide_build_{paper.id}",
            type="primary",
            width="stretch",
            icon=":material/refresh:" if guide else ":material/auto_awesome:",
            disabled=not connected,
            help=None if connected else "需要先连接 AI 模型",
        )
    with controls[1]:
        st.caption(
            guide_prompts.MODE_SPECS[mode].scene
            + "；切换模式后点击生成。"
        )

    if clicked and connected:
        # 用户明确要求重新生成时先清空当前结果；失败记录仍会保留具体原因。
        paper.summary = {
            key: value for key, value in (paper.summary or {}).items()
            if key not in {guide_mod.GUIDE_KEY, "reading_guide_record"}
        }
        repo.save(paper)
        _run_build(paper, http_client, mode)

    if not connected:
        if st.button(
            "连接 AI 模型",
            key=f"guide_connect_{paper.id}",
            icon=":material/key:",
        ):
            _connect_dialog()

    if st.button(
        "带这篇论文问项目助手",
        key=f"guide_handoff_assistant_{paper.id}",
        icon=":material/forum:",
        width="stretch",
    ):
        title = paper.title or "当前论文"
        assistant.open_with_draft(
            project,
            "文献阅读",
            f"请结合《{title}》的 AI 解读，比较它与本项目其他文献的方法和证据，"
            "并指出还缺哪些验证。",
        )
        st.rerun()


# ---------------------------------------------------------------------------
# 讲解
# ---------------------------------------------------------------------------


def _page_badge(
    project: Project,
    paper: Paper,
    page: int,
    quote: str,
    key: str,
) -> None:
    """页角标：点击后才让左侧 PDF 跳到该页并高亮引文。"""
    if st.button(
        f"第 {int(page)} 页",
        key=key,
        icon=":material/location_on:",
        help="定位到左侧原文并高亮这条引文",
    ):
        pdf_workspace.open_document(project.id, paper.id, int(page), quote)
        st.rerun()


def _evidence_block(
    evidence: list[dict],
    project: Project,
    paper: Paper,
    dimension_key: str,
) -> None:
    if not evidence:
        return
    theme.markdown(
        '<div class="rp-guide-label">原文'
        f'<span class="rp-guide-note">　{len(evidence)} 条</span></div>'
        + theme.hidden_compat("原文依据")
    )
    for index, point in enumerate(evidence):
        page = point.get("page")
        theme.markdown(
            f'<div class="rp-guide-point">'
            f'<div class="rp-guide-body">{theme.esc(point.get("text") or "")}</div>'
            f'<div class="rp-guide-point__q">{theme.esc(point.get("quote") or "")}</div>'
            f'</div>'
        )
        if page:
            _page_badge(
                project, paper, int(page), point.get("quote") or "",
                f"guide_page_{paper.id}_{dimension_key}_{index}",
            )
        else:
            theme.markdown(theme.chip_row([("出处未定位", "warn")]))


def _structure_block(
    dimension: dict,
    project: Project,
    paper: Paper,
    position: int,
) -> None:
    items = dimension.get("items") or []
    if not items:
        return
    for index, item in enumerate(items):
        title = sanitize_for_display(str(item.get("title") or "")) or "（未命名章节）"
        start = int(item.get("page_start") or 1)
        end = int(item.get("page_end") or start)
        cols = st.columns([8, 2], vertical_alignment="center")
        cols[0].markdown(f"**{title}**")
        with cols[1]:
            _page_badge(
                project, paper, start, "",
                f"guide_structure_{paper.id}_{position}_{index}",
            )
        cols[0].caption(f"第 {start}–{end} 页" if end != start else f"第 {start} 页")


def _page_range_badge(project: Project, paper: Paper, start: int, end: int) -> None:
    """精简版只在末尾给一个页码范围，不逐段标注。"""
    label = f"第 {start} 页" if start == end else f"第 {start}–{end} 页"
    if st.button(
        label,
        key=f"guide_page_range_{paper.id}",
        icon=":material/location_on:",
        help="定位到本次概览覆盖的原文范围",
    ):
        pdf_workspace.open_document(project.id, paper.id, start, "")
        st.rerun()


def _render_quick_mode(guide: dict, project: Project, paper: Paper) -> None:
    content = guide.get("mode_content") or {}
    text = str(content.get("text") or "").strip()
    if text:
        theme.markdown(f'<p class="rp-overview-copy">{theme.esc(text)}</p>')
    pages = [
        int(page) for page in (guide.get("core_input") or {}).get("pages") or []
        if str(page).isdigit()
    ]
    if pages:
        _page_range_badge(project, paper, min(pages), max(pages))


def _render_audit_mode(guide: dict, project: Project, paper: Paper) -> None:
    content = guide.get("mode_content") or {}
    core_goal = str(content.get("core_goal") or "").strip()
    if core_goal:
        theme.markdown(
            '<div class="rp-guide-label">核心目标</div>'
            f'<div class="rp-guide-lead">{theme.esc(core_goal)}</div>'
        )

    for position, step in enumerate(content.get("steps") or [], start=1):
        order = int(step.get("order") or position)
        title = str(step.get("title") or f"步骤 {order}").strip()
        theme.markdown(
            f'<div class="rp-insight-dimension">'
            f'<div class="rp-insight-dimension__index">{order:02d}</div>'
            f'<div class="rp-insight-dimension__title">{theme.esc(title)}</div>'
            "</div>"
        )
        fields = [
            ("目的", step.get("purpose")),
            ("输入", step.get("input")),
            ("方法", step.get("method")),
            ("输出", step.get("output")),
            ("评价指标", step.get("metric")),
            ("风险", step.get("risk")),
        ]
        rows = [
            {"字段": label, "内容": str(value).strip()}
            for label, value in fields if str(value or "").strip()
        ]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        params = step.get("params") or []
        if params:
            st.dataframe(
                [
                    {
                        "参数": item.get("name") or "",
                        "取值": item.get("value") or "",
                        "单位": item.get("unit") or "",
                        "说明": item.get("note") or "",
                    }
                    for item in params
                ],
                width="stretch",
                hide_index=True,
            )
        page = step.get("page")
        if page:
            _page_badge(
                project, paper, int(page), "", f"guide_audit_{paper.id}_{position}"
            )

    boundaries = [
        str(item).strip() for item in content.get("boundaries") or []
        if str(item).strip()
    ]
    if boundaries:
        theme.markdown('<div class="rp-guide-label">适用边界</div>')
        theme.markdown(
            '<ul class="rp-guide-list">'
            + "".join(f"<li>{theme.esc(item)}</li>" for item in boundaries)
            + "</ul>"
        )


def _dimension_block(
    key: str,
    dimension: dict,
    project: Project,
    paper: Paper,
    position: int,
) -> None:
    title = str(dimension.get("title") or guide_schemas.INSIGHT_DIMENSION_LABELS.get(key) or key)
    theme.markdown(
        f'<div class="rp-insight-dimension" id="rp-dimension-{theme.esc(key)}">'
        f'<div class="rp-insight-dimension__index">{position:02d}</div>'
        f'<div class="rp-insight-dimension__title">{theme.esc(title)}</div>'
        "</div>"
    )
    summary = sanitize_for_display(str(dimension.get("summary") or "")).strip()
    if summary:
        theme.markdown(f'<div class="rp-guide-lead">{theme.esc(summary)}</div>')
    points = [str(item).strip() for item in dimension.get("points") or [] if str(item).strip()]
    if points and key != "structure":
        body = "".join(f"<li>{theme.esc(item)}</li>" for item in points)
        theme.markdown(f'<ul class="rp-guide-list">{body}</ul>')
    if key == "structure":
        _structure_block(dimension, project, paper, position)
    else:
        _evidence_block(
            dimension.get("evidence") or [], project, paper, f"{key}_{position}"
        )


def _validation_strip(guide: dict) -> None:
    stats = guide.get("stats") or {}
    raw = int(stats.get("raw_points") or 0)
    kept = int(stats.get("kept_points") or 0)
    if not raw:
        raw = kept = sum(
            len((dimension or {}).get("evidence") or [])
            for dimension in (guide.get("dimensions") or {}).values()
        )
    items = [
        f"模型返回 {raw} 条",
        f"保留 {kept} 条",
        "通过原文校验" if kept else "暂无原文依据",
    ]
    core = guide.get("core_input") or {}
    if core.get("sections"):
        items.append(f"输入 {len(core['sections'])} 个核心段落")
    theme.markdown(theme.hidden_compat(" · ".join(items)))


def _metadata_footer(guide: dict, record: dict | None) -> None:
    stats = guide.get("stats") or {}
    mode = guide_prompts.READING_MODE_LABELS.get(
        str(guide.get("mode") or "deep"), "标准详细版"
    )
    details = [f"模型 {guide.get('model') or '未记录'}", mode]
    if record and record.get("elapsed_s") is not None:
        details.append(f"耗时 {float(record['elapsed_s']):.1f} 秒")
    if record and record.get("attempts"):
        details.append(f"请求 {int(record['attempts'])} 次")
    if guide.get("generated_at"):
        details.append(f"生成于 {guide['generated_at']}")
    if stats.get("input_truncated"):
        details.append("核心原文已按预算截取")
    theme.markdown(theme.hidden_compat(" · ".join(details)))


# ---------------------------------------------------------------------------
# 相关文献 → 加入文献库
# ---------------------------------------------------------------------------


def _related_rows(items: list[dict], already: dict[str, str], paper: Paper,
                  offset: int) -> None:
    for position, item in enumerate(items):
        index = offset + position
        key = guide_mod.related_key(item)
        existing = already.get(key)
        verified = bool(item.get("verified"))
        external_url = links.item_external_url(item)
        title = theme.esc(item.get("title") or "")
        if external_url:
            title = (
                f'<a class="rp-related-title" href="{theme.esc(external_url)}" '
                f'target="_blank" rel="noopener noreferrer">{title}</a>'
            )
        head = st.columns([5, 2])
        # 一律走 theme.markdown：它带 unsafe_allow_html，否则这些 div 会以源码形式
        # 显示在页面上（验收时真的看到过一屏 <div class="rp-guide-body">）。
        with head[0]:
            theme.markdown(
                f'<div class="rp-guide-body"><b>{title}</b></div>'
                f'<div class="rp-guide-note">'
                f'{theme.esc(" · ".join(x for x in (item.get("authors"), item.get("year"), item.get("venue")) if x))}'
                f'{"　·　DOI " + theme.esc(item.get("doi") or "") if item.get("doi") else ""}</div>'
            )
        label = VERIFY_LABEL.get(str(item.get("verification")), "未核实")
        with head[1]:
            theme.markdown(theme.chip_row([(label, "evidence" if verified else "warn")]))
        if item.get("why"):
            theme.markdown(f'<div class="rp-guide-body">{theme.esc(item["why"])}</div>')

        cols = st.columns([1, 2, 5])
        if existing:
            with cols[1]:
                theme.markdown(theme.chip_row([("已在文献库", "ok")]))
        elif not verified:
            cols[1].caption("未能核实，不能加入")
        else:
            cols[0].checkbox(
                "选", key=f"guide_pick_{paper.id}_{index}", label_visibility="collapsed"
            )
        theme.markdown('<div class="rp-guide-related"></div>')


def _related_section(items: list[dict], project: Project, paper: Paper) -> None:
    if not items:
        return
    theme.markdown('<div class="rp-guide-h rp-guide-h--lv3">相关文献</div>')
    already = guide_mod.existing_related_ids(project.id, items)
    verified = [item for item in items if item.get("verified")]
    st.caption(
        f"共 {len(items)} 篇，其中 {len(verified)} 篇已通过外部核验"
        f"（已核验的才能加入文献库；当前项目：{project.title}）"
    )
    categories: dict[str, list[dict]] = {}
    for item in items:
        categories.setdefault(str(item.get("category") or "理论基础"), []).append(item)
    offset = 0
    for category, group in categories.items():
        theme.markdown(f'<div class="rp-guide-label">{theme.esc(category)}</div>')
        _related_rows(group, already, paper, offset)
        offset += len(group)

    picked = [
        item for index, item in enumerate(items)
        if st.session_state.get(f"guide_pick_{paper.id}_{index}") and item.get("verified")
        and guide_mod.related_key(item) not in already
    ]
    cols = st.columns([3, 4])
    if cols[0].button(
        f"加入文献库（已选 {len(picked)} 篇）",
        key=f"guide_add_{paper.id}",
        type="primary" if picked else "secondary",
        disabled=not picked,
        width="stretch",
    ):
        added = 0
        skipped = 0
        for item in picked:
            _, created, _ = guide_mod.add_related_to_project(project.id, item)
            added += int(created)
            skipped += int(not created)
        for index in range(len(items)):
            st.session_state.pop(f"guide_pick_{paper.id}_{index}", None)
        st.session_state["_guide_notice"] = (
            f"已加入 {added} 篇" + (f"，{skipped} 篇已在库里" if skipped else "")
        )
        st.rerun()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def render(project: Project, paper: Paper, http_client) -> None:
    """渲染右栏 AI 解读：按当前模式显示单段、复现步骤或固定七维。"""
    notice = st.session_state.pop("_guide_notice", None)
    if notice:
        st.success(notice)

    guide = guide_mod.load(paper)
    record = (paper.summary or {}).get("reading_guide_record") or {}
    _analysis_header(paper, project, http_client, guide)
    # 思考记录（有就显示，默认折叠；生成失败但有部分思考时也在这里）
    _thinking_block(guide or {}, record)

    if not guide:
        if record.get("status") in {"failed", "refused"}:
            # 终态已经在上面的控制区里如实说明；这里不再重复一句空状态
            return
        theme.empty_state(
            "还没有 AI 解读。选择阅读目标后生成：精简版判断价值，方法版整理复现步骤，"
            "标准版展开固定七维。所有正式引文都会回到原文核验。"
        )
        return

    mode = str(guide.get("mode") or "deep")
    mode_content = guide.get("mode_content")
    if not isinstance(mode_content, dict):
        dimensions = guide.get("dimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            dimensions = guide_mod.legacy_dimensions_from_sections(
                guide.get("sections") or [], paper
            )
        elif {"type_domain", "content", "structure"} & set(dimensions):
            dimensions = guide_mod.legacy_dimensions_from_sections(
                guide.get("sections") or [], paper
            )
        mode_content = {"kind": "deep", "dimensions": dimensions}
        mode = "deep"

    _validation_strip(guide)
    if guide.get("legacy"):
        st.warning(
            "这是旧版导览的兼容展示。点击右上角「重新生成」，"
            "可按新的三种阅读模式规范重新生成。"
        )

    stats = guide.get("stats") or {}
    if stats.get("dropped_points"):
        theme.markdown(
            f'<div class="rp-guide-note">有 {stats["dropped_points"]} 条引文在原文里定位不到，'
            "未计入正式解读；所有展示内容都通过了原文校验。</div>"
        )

    kind = str(mode_content.get("kind") or mode)
    if kind == "quick":
        _render_quick_mode(guide, project, paper)
    elif kind == "audit":
        _render_audit_mode(guide, project, paper)
    else:
        dimensions = mode_content.get("dimensions") or {}
        for position, key in enumerate(guide_schemas.INSIGHT_DIMENSION_KEYS, start=1):
            dimension = dimensions.get(key) or {
                "title": guide_schemas.INSIGHT_DIMENSION_LABELS[key],
                "summary": "论文未提供可提取的信息。",
                "points": [],
                "evidence": [],
            }
            _dimension_block(key, dimension, project, paper, position)

    notes = guide.get("notes") or []
    if notes:
        with st.expander("输入与覆盖情况", expanded=False):
            for note in notes:
                st.caption(f"- {note}")

    if kind != "quick":
        _related_section(guide.get("related") or [], project, paper)
    _metadata_footer(guide, record)

"""工作台 2：复现规划（持续科研协作，而不是一次性表单）。

页面顺序（P0-A）：

- 有草稿时以**草稿为主内容**：方案内容、步骤索引、与 AI 讨论、决策台账；
- 有已确认版本时先看**当前版本 + 进度 + 历史**，"另起新方案"收进折叠按钮；
- 两个动作分开：「继续完善当前方案」派生可改草稿（确认后成为下一版），
  「另起新方案」开新的研究主题与新的修订历史。

三条证据边界（沿用 P0-1）：

- 出处只认本地 ``PaperClaim``，界面显示成「PDF · 第 N 页」入口，引文附在下面；
- AI 的修改建议必须用户点「应用到草稿」才生效，已确认版本不可被覆盖；
- 人工完成状态与真实实验执行结果分开显示，没有引擎的步骤不给运行入口。
"""

from __future__ import annotations

import logging

import streamlit as st

from .. import display_text
from ..agent import chat_schemas
from ..agent import plan_draft as draft_mod
from ..agent import plan_schemas
from ..agent.client import LLMClient
from ..agent.schemas import KIND_LABELS
from ..planning import discussion as discuss_mod
from ..planning import draft as plan_store
from ..planning import steps as steps_mod
from ..storage import repository as repo
from ..storage.models import (
    ExperimentRun,
    Paper,
    PlanDraft,
    PlanDraftStatus,
    PlanMode,
    PlanVersion,
    Project,
)
from . import llm_settings, pdf_reader, pdf_workspace, split_layout, theme

log = logging.getLogger(__name__)

INDEX_HEIGHT = 560

MODE_CHOICES: tuple[tuple[PlanMode, str], ...] = (
    (PlanMode.REPRODUCE, "A 原文复现：尽量忠实论文设定地复现某一步"),
    (PlanMode.IMPROVE, "B 文献改进对比：基线方法 vs 后续方法，做控制变量实验"),
    (PlanMode.OWN, "C 自主研究设想：提出自己的方法或指标，先形成可检验假设"),
)

CHAT_ROLES: dict[str, str] = {
    "讨论：解释、质疑、比较文献": "discuss",
    "调整计划：给出可应用的字段修改": "workflow",
}

_OUTCOME_VERDICTS: dict[str, tuple[str, str]] = {
    "success": ("success", "已生成"),
    "partial": ("warning", "部分步骤没有文献依据"),
    "no_evidence": ("warning", "没有步骤挂上文献依据"),
    "empty": ("warning", "模型没有给出任何可用的步骤"),
    "failed": ("error", "生成失败"),
    "refused": ("info", "暂不生成计划草稿"),
}


def _outcome_key(project_id: str) -> str:
    return f"plan_draft_outcome_{project_id}"


def _pick_key(project_id: str) -> str:
    return f"planner_pick_{project_id}"


def _stage_choices() -> tuple[list[str], dict[str, str]]:
    labels = ["不关联引擎"] + [f"{label}" for label in steps_mod.ENGINE_STAGES.values()]
    mapping = {f"{label}": key for key, label in steps_mod.ENGINE_STAGES.items()}
    mapping["不关联引擎"] = ""
    return labels, mapping


# ---------------------------------------------------------------------------
# 顶部与左栏
# ---------------------------------------------------------------------------


def _selection_options(groups: list[dict]) -> tuple[list[str], dict[str, str]]:
    """左栏选项：显示用的是人话标签，映射回内部的 draft/version 标识。

    这里刻意不用 ``format_func``：选项本身就是标签，浏览器与无头自检看到的
    是同一个字符串，不会出现"组件值对不上选项"的坑。
    """
    options: list[str] = []
    mapping: dict[str, str] = {}
    used: set[str] = set()

    def _unique(label: str, tail: str) -> str:
        if label not in used:
            used.add(label)
            return label
        # 同一系列里出现两份同步骤数的草稿时，用 id 尾号区分
        candidate = f"{label}·{tail}"
        used.add(candidate)
        return candidate

    for group in groups:
        title = (group["title"] or "未命名方案")[:18]
        step_count = {}
        for draft in group["drafts"]:
            key = f"draft:{draft.id}"
            step_count[draft.id] = len(draft.steps)
            if draft.status is PlanDraftStatus.CONFIRMED:
                mark = "已确认"
            elif draft.derived_from_version_id:
                mark = "草稿 · 基于已确认版本"
            else:
                mark = "草稿"
            label = _unique(f"{title}｜〔{mark}〕{len(draft.steps)} 步", draft.id[-4:])
            options.append(label)
            mapping[label] = key
        for version in group["versions"]:
            key = f"version:{version.id}"
            label = _unique(
                f"{title}｜版本 v{version.version}｜{len(version.steps)} 步", version.id[-4:]
            )
            options.append(label)
            mapping[label] = key
    return options, mapping


def _index_pane(project: Project, groups: list[dict], progress_by_series: dict[str, dict]) -> None:
    """左栏：按研究主题分组列出修订历史与在写草稿。"""
    options, _ = _selection_options(groups)

    if not options:
        st.caption("暂无复现方案")
        return

    key = _pick_key(project.id)
    if st.session_state.get(key) not in options:
        st.session_state[key] = options[0]
    # The original option strings stay in the DOM for existing automation, but
    # the visible index uses compact labels without version/draft wording.
    with st.container(key="rp_index_compat"):
        st.radio("方案与版本", options, key=key, label_visibility="collapsed")

    def compact(label: str) -> str:
        text = label.replace("〔已确认〕", "").replace("〔草稿〕", "")
        text = text.replace("〔草稿 · 基于已确认版本〕", "")
        import re

        text = re.sub(r"版本 v\d+｜", "", text)
        return text.replace("｜", " · ")

    for option in options:
        selected = st.session_state.get(key) == option
        with st.container(key="rp_index_row_selected" if selected else f"rp_plan_row_{abs(hash(option))}"):
            st.button(
                compact(option),
                key=f"plan_pick_{project.id}_{abs(hash(option))}",
                type="tertiary",
                width="stretch",
                on_click=lambda value=option: st.session_state.__setitem__(key, value),
            )
    for group in groups:
        item = group["latest_version"] or (
            group["drafts"][0] if group["drafts"] else None
        )
        if item is None:
            continue
        stats = progress_by_series.get(group["series_id"], {})
        bits = [
            (group["title"] or "未命名方案")[:18],
            f"{stats.get('done', 0)}/{stats.get('total', 0)}",
        ]
        if stats.get("engine_succeeded"):
            bits.append(f"{stats['engine_succeeded']} 次实验")
        st.caption("　".join(bits))


def _select(project_id: str, internal: str) -> None:
    """把左栏选中项切到某个草稿/版本（左栏存的是显示标签，这里做一次换算）。"""
    _, mapping = _selection_options(plan_store.list_series(project_id))
    label = next((key for key, value in mapping.items() if value == internal), internal)
    st.session_state[_pick_key(project_id)] = label

# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _claim_label(claim, papers_by_id: dict[str, Paper]) -> str:
    """复选框只留「类型 + 结论 + 页码」。

    论文标题由分组标题统一显示一次，这里再拼一遍会让每行都拖着同一串灰字。
    """
    tag = KIND_LABELS.get(claim.kind, "未分类")
    page = f"第 {claim.page} 页" if claim.page else ""
    body = claim.claim[:60]
    return f"[{tag}] {body}（{page}）" if page else f"[{tag}] {body}"


def _claim_picker(project: Project, claims: list, papers_by_id: dict[str, Paper]) -> list:
    if not claims:
        st.info("还没有已分析的文献条目。先在「文献探索」里对论文做一次 AI 分析，再回到这里挑依据。")
        return []
    by_paper: dict[str, list] = {}
    for claim in claims:
        by_paper.setdefault(claim.paper_id, []).append(claim)
    selected: list = []
    for paper_id, items in by_paper.items():
        paper = papers_by_id.get(paper_id)
        st.caption(f"《{(getattr(paper, 'title', '') or '未命名文献')[:60]}》")
        for claim in items:
            if st.checkbox(_claim_label(claim, papers_by_id), key=f"claim_{claim.id}"):
                selected.append(claim)
    st.caption(f"已选 {len(selected)} 条作为依据。")
    return selected


def _render_outcome(record: dict) -> None:
    verdict = str(record.get("verdict") or "failed")
    level, text = _OUTCOME_VERDICTS.get(verdict, ("info", "计划草稿已生成"))
    getattr(st, level)(text)
    if verdict in {"failed", "refused"}:
        st.caption(record.get("reason") or "未记录原因")
    else:
        st.caption(
            f"依据条目 {record.get('claim_count', 0)} 条　·　"
            f"挂上出处的步骤 {record.get('evidence_count', 0)} 个　·　"
            f"标为 AI 推断的步骤 {record.get('ai_step_count', 0)} 个"
        )
    facts = []
    if record.get("model"):
        facts.append(f"模型 {record['model']}")
    if record.get("elapsed_s"):
        facts.append(f"耗时 {record['elapsed_s']}s")
    if facts:
        st.caption("　·　".join(facts))


def _run_generate(
    project: Project, mode: PlanMode, selected: list,
    papers_by_id: dict[str, Paper], user_notes: str, *, into: PlanDraft | None = None,
) -> None:
    """跑一次生成：终态先记下来再重跑（一次性提示，不会一直挂着）。"""
    try:
        with st.spinner("正在依据条目草拟方案……"):
            outcome = draft_mod.generate_draft(
                LLMClient(), project, mode, selected,
                papers=papers_by_id, user_notes=user_notes, into=into,
            )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可见终态
        log.exception("生成计划草稿异常 %s", project.id)
        outcome = draft_mod.DraftOutcome(
            status="failed",
            reason=f"生成过程中出现未预期的错误：{type(exc).__name__}: {str(exc)[:200]}",
            claim_count=len(selected),
        )
    st.session_state[_outcome_key(project.id)] = outcome.to_dict()
    if outcome.draft is not None:
        _select(project.id, f"draft:{outcome.draft.id}")
    st.rerun()


def _regenerate_block(project: Project, draft: PlanDraft, claims: list,
                      papers_by_id: dict[str, Paper]) -> None:
    """把新勾选的条目重新生成到**当前这份草稿**里（保留系列与版本谱系）。"""
    with st.expander("重新生成方案", expanded=False):
        selected = _claim_picker(project, claims, papers_by_id)
        if not selected:
            return
        if not llm_settings.require_connection("重新生成方案"):
            return
        if st.button("重新生成", key=f"regen_{draft.id}"):
            _run_generate(
                project, draft.mode, selected, papers_by_id, draft.user_notes, into=draft
            )


def _new_series_block(project: Project, claims: list, papers_by_id: dict[str, Paper]) -> None:
    """另起新方案：新主题、新修订历史，不与当前方案混在一起。"""
    with st.expander("另起新方案", expanded=False):
        labels = [text for _, text in MODE_CHOICES]
        choice = st.radio("方案类型", labels, key=f"new_mode_{project.id}", label_visibility="collapsed")
        title = st.text_input("主题", key=f"new_title_{project.id}",
                              placeholder="例如：频散测量的台站间一致性检验")
        if st.button("创建新方案", key=f"new_series_{project.id}"):
            mode = MODE_CHOICES[labels.index(choice)][0]
            draft = plan_store.start_new_series(project, mode, title)
            _select(project.id, f"draft:{draft.id}")
            st.rerun()


# ---------------------------------------------------------------------------
# 步骤索引与详情（P0-C）
# ---------------------------------------------------------------------------


def _evidence_lines(evidence: list[dict], project_id: str = "", key_prefix: str = "plan_ev") -> None:
    rows = []
    for item in evidence:
        rows.append({
            "paper_id": item.get("paper_id", ""), "page": item.get("page"),
            "quote": item.get("quote", ""),
            "verified": item.get("certainty") == "stated",
            "label": (f"PDF · 第 {item['page']} 页" if item.get("page") else "打开原文"),
        })
    if rows:
        pdf_reader.evidence_buttons(rows, project_id=project_id,
                                    key_prefix=f"{key_prefix}_{len(rows)}")
    for item in evidence:
        st.markdown(f"- {item.get('claim', '')}")
        # 出处入口只留上面那一排按钮，这里不再重复一遍页码（同一行出现两次=噪声）
        st.caption(
            f"　{(item.get('paper_title') or '未命名文献')[:30]}"
            f"　·　{item.get('certainty_label', '未核实')}"
        )


def _param_lines(params: list[dict]) -> None:
    for item in params:
        label = plan_schemas.param_origin_label(item.get("origin", "suggested"))
        unit = item.get("unit", "")
        suffix = f" {unit}" if unit else ""
        st.markdown(
            f"- {item.get('name', '')}：{item.get('suggested_value', '')}{suffix}　`{label}`"
        )
        if item.get("reason"):
            st.caption(f"　理由：{item['reason']}")


def _step_index(items: list[dict], key: str) -> dict:
    labels = [
        f"{item['status_mark']} {item['order']}. {(item.get('title') or '未命名')[:26]}"
        for item in items
    ]
    current = st.session_state.get(key)
    if current not in labels:
        st.session_state[key] = labels[0]
    with st.container(key="rp_steps"):
        chosen = st.radio("步骤索引", labels, key=key, label_visibility="collapsed")
    return items[labels.index(chosen)]


def _step_state_form(
    project: Project, item, series_id: str, step: dict,
    *, editable_text: bool,
) -> None:
    """当前步骤：文本（草稿可改）+ 人工状态 + 待办备忘 + 引擎关联。"""
    state = steps_mod.state_for(project.id, series_id, step["order"])
    stage_labels, stage_map = _stage_choices()
    current_stage = steps_mod.stage_label(state.engine_stage) or "不关联引擎"
    status_labels = {label: value for value, label in steps_mod.STATUS_LABELS.items()}
    runs = repo.list_all(ExperimentRun, project.id)
    run_options = ["（不关联实验记录）"] + [f"{run.id}｜{run.status.value}" for run in runs]
    current_run = next(
        (option for option in run_options if option.startswith(state.run_id) and state.run_id),
        run_options[0],
    )

    with st.form(key=f"step_form_{getattr(item, 'id', '')}_{step['order']}"):
        st.markdown(f"**第 {step['order']} 步｜{step.get('title') or '未命名'}**")
        if editable_text:
            title = st.text_input("标题", value=display_text.strip_claim_refs(str(step.get("title") or "")))
            purpose = st.text_area("要解决的问题",
                                   value=display_text.strip_claim_refs(str(step.get("purpose") or "")),
                                   height=60)
            method = st.text_area("做法",
                                  value=display_text.strip_claim_refs(str(step.get("method") or "")),
                                  height=60)
            metric = st.text_input("预期指标（预期，不是实测）",
                                   value=display_text.strip_claim_refs(str(step.get("expected_metric") or "")))
        else:
            title = purpose = method = metric = ""
            if step.get("purpose"):
                st.caption(f"要解决的问题：{display_text.strip_claim_refs(step['purpose'])}")
            if step.get("method"):
                st.caption(f"做法：{display_text.strip_claim_refs(step['method'])}")
            if step.get("expected_metric"):
                st.caption(f"预期指标：{display_text.strip_claim_refs(step['expected_metric'])}")

        cols = st.columns(2)
        status_label = cols[0].selectbox(
            "人工状态", list(status_labels),
            index=list(status_labels).index(steps_mod.STATUS_LABELS[state.status]),
        )
        stage_label = cols[1].selectbox(
            "关联计算阶段", stage_labels, index=stage_labels.index(current_stage),
        )
        todo = st.text_input("待办（一句话）", value=state.todo)
        note = st.text_area("备忘", value=state.note, height=80)
        run_label = st.selectbox("关联实验记录", run_options, index=run_options.index(current_run))
        saved = st.form_submit_button("保存这一步")

    if saved:
        try:
            if editable_text:
                plan_store.apply_edits(
                    item,
                    steps={step["order"] - 1: {
                        "title": title, "purpose": purpose,
                        "method": method, "expected_metric": metric,
                    }},
                )
            run_id = "" if run_label == run_options[0] else run_label.split("｜")[0]
            steps_mod.update_state(
                state,
                status=status_labels[status_label],
                todo=todo,
                note=note,
                engine_stage=stage_map[stage_label],
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - 显示真实原因
            st.error(f"保存失败：{exc}")
        else:
            st.rerun()

    # 人工状态与引擎执行分开说清楚
    st.caption(
        f"人工状态：{step['status_label']}（你自己勾的）"
        f"　｜　引擎执行：{step['run_label'] if step['run_id'] else '未关联实验记录'}"
    )
    if not step["has_engine"]:
        st.caption(
            "这一步没有可用的计算引擎（现有引擎只覆盖质控、预处理、互相关、叠加），"
            "需人工执行并在这里记录。"
        )
    if step.get("evidence"):
        st.markdown("**文献依据**")
        _evidence_lines(step["evidence"], project.id, f"plan_step_{step.get('order')}")


def _step_panel(project: Project, item, series_id: str, *, editable_text: bool) -> None:
    states = repo.list_step_states(project.id, series_id)
    runs = steps_mod.runs_by_id(project.id)
    merged = steps_mod.merge(list(getattr(item, "steps", []) or []), states, runs)
    if not merged:
        st.caption("这份方案还没有步骤。")
        return
    step = _step_index(merged, f"step_pick_{getattr(item, 'id', '')}")
    _step_state_form(project, item, series_id, step, editable_text=editable_text)


# ---------------------------------------------------------------------------
# 与 AI 讨论（P0-B）
# ---------------------------------------------------------------------------


def _suggestion_card(project: Project, draft: PlanDraft | None, message, suggestion: dict,
                     pages: dict) -> None:
    st.markdown(
        f"**建议修改 · {suggestion.get('field_label') or suggestion.get('field')}**"
        + (f"（第 {suggestion['step_order']} 步）" if suggestion.get("step_order") else "")
    )
    cols = st.columns(2)
    cols[0].caption("改动前")
    cols[0].code(suggestion.get("before") or "（空）", language=None)
    cols[1].caption("改动后")
    cols[1].code(suggestion.get("after") or "", language=None)
    if suggestion.get("why"):
        st.caption(f"理由：{suggestion['why']}")
    if suggestion.get("claim_ids"):
        refs = "、".join(
            display_text.page_label(claim_id, pages) for claim_id in suggestion["claim_ids"][:6]
        )
        st.caption("引用依据：" + refs)
    if suggestion.get("dropped_claim_ids"):
        st.caption(f"（有 {len(suggestion['dropped_claim_ids'])} 处引用在本地条目里找不到，已忽略）")

    status = suggestion.get("status")
    if status != "pending":
        st.caption("这条建议已经" + ("采纳" if status == "applied" else "驳回") + "。")
        return
    cols = st.columns([1, 1, 3])
    if isinstance(draft, PlanVersion):
        # 已确认版本不可直接改：派生一份新草稿再应用，原版本保持不变
        if cols[0].button("应用并创建新方案", key=f"fork_apply_{message.id}_{suggestion['id']}"):
            try:
                new_draft = plan_store.create_revision(draft)
                discuss_mod.apply_suggestion(message.id, suggestion["id"], draft=new_draft)
            except Exception as exc:  # noqa: BLE001
                st.error(f"应用失败：{exc}")
            else:
                _select(draft.project_id, f"draft:{new_draft.id}")
                st.rerun()
    elif cols[0].button("应用修改", key=f"apply_{message.id}_{suggestion['id']}"):
        try:
            discuss_mod.apply_suggestion(message.id, suggestion["id"], draft=draft)
        except Exception as exc:  # noqa: BLE001 - 显示真实原因
            st.error(f"应用失败：{exc}")
        else:
            st.rerun()
    if cols[1].button("驳回", key=f"reject_{message.id}_{suggestion['id']}"):
        discuss_mod.reject_suggestion(message.id, suggestion["id"], reason="用户在界面上驳回")
        st.rerun()


def _chat_history(project: Project, draft: PlanDraft | None, series_id: str, pages: dict) -> None:
    messages = discuss_mod.conversation(project.id, series_id=series_id)
    if not messages:
        return
    for message in messages[-20:]:
        if message.role.value == "user":
            st.markdown(f"**你**：{message.content}")
            continue
        label = "　".join(message.labels) or chat_schemas.ASSISTANT_LABEL
        with st.container(border=True):
            st.markdown(f"**助手**　`{label}`")
            if message.error:
                st.error(f"这次没有得到回答：{message.error}")
            if message.content:
                st.write(display_text.strip_claim_refs(message.content))
            for suggestion in message.suggestions:
                _suggestion_card(project, draft, message, suggestion, pages)


def _chat_panel(project: Project, draft, claims: list, series_id: str,
                step_order: int | None, *, collapsed: bool = True) -> None:
    history = discuss_mod.conversation(project.id, series_id=series_id)
    count = f"（{len(history)} 条）" if history else ""
    with st.expander(f"与 AI 讨论这份方案{count}", expanded=not collapsed):
        _render_chat_body(project, draft, claims, series_id, step_order)


def _render_chat_body(project: Project, draft, claims: list, series_id: str,
                      step_order: int | None) -> None:
    """讨论上下文可以是**草稿**也可以是**已确认版本**（只读）。

    P0-A：讨论权限与"修改已确认计划"的权限是两件事。已确认版本不能改，但用户随时可以
    针对它提问；AI 的解释/反驳/建议照常生成，只有"应用修改"才需要一份未确认草稿。
    """
    _chat_history(project, draft, series_id, display_text.page_map(claims))

    if draft is None:
        st.caption("还没有可讨论的方案。先生成或确认一份方案。")
        return
    if not llm_settings.require_connection("与 AI 讨论方案"):
        return

    context_id = getattr(draft, "id", "context")
    role_label = st.radio(
        "对话类型", list(CHAT_ROLES), key=f"chat_role_{context_id}", label_visibility="collapsed"
    )
    question = st.text_area(
        "你的问题或要求", key=f"chat_input_{context_id}", height=80,
        placeholder="例如：第 3 步的依据是哪一条？如果只用一个台站对，结论还站得住吗？",
    )
    if st.button("发送", key=f"chat_send_{context_id}", type="primary"):
        if not question.strip():
            st.warning("先写下要问的内容。")
            return
        with st.spinner("正在结合当前方案与条目作答……"):
            discuss_mod.ask_and_record(
                LLMClient(), project=project, question=question,
                claims=claims, item=draft, step_order=step_order,
                role=CHAT_ROLES[role_label], series_id=series_id,
            )
        st.rerun()


def _decisions_panel(project: Project, series_id: str, pages: dict) -> None:
    items = discuss_mod.decisions(project.id, series_id=series_id)
    if not items:
        return
    with st.expander(f"重要决策与建议（{len(items)}）", expanded=False):
        for decision in items:
            mark = {"accepted": "已采纳", "proposed": "待定", "rejected": "已驳回"}[decision.status.value]
            st.markdown(f"- `{mark}`{decision.text}")
            bits = []
            if decision.evidence_claim_ids:
                bits.append("依据 " + "、".join(
                    display_text.page_label(claim_id, pages)
                    for claim_id in decision.evidence_claim_ids[:4]
                ))
            if decision.plan_version_id:
                version = repo.get(PlanVersion, decision.plan_version_id)
                bits.append("已落成")
            if bits:
                st.caption("　" + "　".join(bits))


# ---------------------------------------------------------------------------
# 草稿 / 版本视图
# ---------------------------------------------------------------------------


def _plan_text_form(project: Project, draft: PlanDraft) -> None:
    # 行内条目编号按证据重新排版：界面上不显示内部编号，出处由步骤里的「PDF · 第 N 页」承担。
    cleaned = {name: display_text.strip_claim_refs(getattr(draft, name) or "")
               for name in ("question", "objective", "data_requirements",
                            "success_criteria", "user_notes")}
    with st.form(key=f"plan_text_{draft.id}_{draft.revision}"):
        question = st.text_area("研究问题", value=cleaned["question"], height=70)
        objective = st.text_area("研究目标", value=cleaned["objective"], height=70)
        data_requirements = st.text_area("数据需求", value=cleaned["data_requirements"], height=80)
        success_criteria = st.text_area("成功判据", value=cleaned["success_criteria"], height=70)
        notes = st.text_area("用户设定", value=cleaned["user_notes"], height=80)
        cols = st.columns([1, 1, 2])
        saved = cols[0].form_submit_button("保存修改")
        confirmed = cols[1].form_submit_button("确认计划", type="primary")
    if saved or confirmed:
        try:
            plan_store.apply_edits(draft, text={
                "question": question, "objective": objective,
                "data_requirements": data_requirements,
                "success_criteria": success_criteria, "user_notes": notes,
            })
            if confirmed:
                version, created = plan_store.confirm_draft(draft)
                if created:
                    _select(project.id, f"version:{version.id}")
        except Exception as exc:  # noqa: BLE001
            log.exception("保存或确认计划失败 %s", draft.id)
            st.error(f"操作失败：{exc}")
        else:
            st.session_state.pop(_outcome_key(project.id), None)
            st.rerun()


def _draft_view(project: Project, draft: PlanDraft, claims: list,
                papers_by_id: dict[str, Paper]) -> None:
    confirmed = draft.status is PlanDraftStatus.CONFIRMED
    series_id = plan_store.series_of(draft)
    pages = display_text.page_map(claims)

    # 头部：方案身份 + 两个分明的动作（继续完善 / 另起新方案）
    version = repo.get(PlanVersion, draft.confirmed_version_id) if draft.confirmed_version_id else None
    title = plan_store.series_title_of(draft)
    status_text = (
        f"已确认版本 v{version.version}" if confirmed and version
        else "草稿" if not confirmed else ""
    )
    theme.page_title(title, status_text)
    theme.markdown(theme.chip_row([
        (plan_schemas.mode_label(draft.mode), ""),
        ("草稿" if not confirmed else "已确认", "compat"),
        (f"依据条目 {draft.claim_count} 条", "evidence" if draft.claim_count else ""),
        ("基于已确认版本继续修订", "ai") if draft.derived_from_version_id else ("新建方案", ""),
    ]))

    if draft.warnings:
        with st.expander(f"需要人工确认的 {len(draft.warnings)} 处", expanded=False):
            for item in draft.warnings[:20]:
                st.markdown(f"- {item}")
    if draft.dropped:
        with st.expander(f"未采用的引用 {len(draft.dropped)} 条", expanded=False):
            for item in draft.dropped[:20]:
                st.caption(f"- {item.get('reason', '未说明原因')}")

    # 状态条：人工进度 + 引擎执行，各自独立
    states = repo.list_step_states(project.id, series_id)
    merged = steps_mod.merge(list(draft.steps or []), states, steps_mod.runs_by_id(project.id))
    stats = steps_mod.progress(merged)
    theme.status_strip([
        (f"人工完成 {stats['done']}/{stats['total']}", "ok" if stats["done"] else ""),
        (f"引擎执行成功 {stats['engine_succeeded']}", "ai" if stats["engine_succeeded"] else ""),
        (f"可执行步骤 {stats['engine_capable']}", ""),
    ], progress=(stats["done"] / stats["total"]) if stats["total"] else 0.0)

    # 主阅读区通栏：目标 → 步骤索引 → 当前步骤。讨论与历史按需展开，不再占一条常驻窄列
    theme.markdown(theme.hidden_compat("研究目标"))
    st.markdown(display_text.strip_claim_refs(draft.objective) or "—")
    if not confirmed:
        with st.expander("编辑方案内容", expanded=False):
            _plan_text_form(project, draft)
    theme.markdown(theme.hidden_compat("步骤索引"))
    _step_panel(project, draft, series_id, editable_text=not confirmed)

    step_order = st.session_state.get(f"step_pick_{draft.id}")
    # 已确认的草稿同样可以作为讨论上下文（只读）；"应用"由 _suggestion_card 决定去向
    _chat_panel(project, draft, claims, series_id, step_order, collapsed=True)
    _decisions_panel(project, series_id, pages)
    if not confirmed:
        _regenerate_block(project, draft, claims, papers_by_id)


def _version_view(project: Project, version: PlanVersion, pages: dict, claims: list) -> None:
    series_id = plan_store.series_of(version)
    states = repo.list_step_states(project.id, series_id)
    merged = steps_mod.merge(list(version.steps or []), states, steps_mod.runs_by_id(project.id))
    stats = steps_mod.progress(merged)

    theme.page_title(
        plan_store.series_title_of(version),
        f"已确认版本 v{version.version}",
    )
    theme.markdown(theme.chip_row([
        (plan_schemas.mode_label(version.mode), ""),
        (f"确认于 {(version.confirmed_at or version.created_at)[:16]}", ""),
        ("内容已定稿", "compat"),
    ]))
    theme.status_strip([
        (f"人工完成 {stats['done']}/{stats['total']}", "ok" if stats["done"] else ""),
        (f"引擎执行成功 {stats['engine_succeeded']}", "ai" if stats["engine_succeeded"] else ""),
        (f"文献依据 {len(version.evidence)} 条", "evidence" if version.evidence else ""),
    ], progress=(stats["done"] / stats["total"]) if stats["total"] else 0.0)

    theme.markdown(theme.hidden_compat("研究目标"))
    st.markdown(display_text.strip_claim_refs(version.objective) or "—")
    if st.button("继续完善当前方案", key=f"revise_{version.id}", type="primary"):
        draft = plan_store.create_revision(version)
        _select(project.id, f"draft:{draft.id}")
        st.rerun()
    theme.markdown(theme.hidden_compat("步骤索引"))
    _step_panel(project, version, series_id, editable_text=False)

    _chat_panel(project, version, claims, series_id, None, collapsed=True)
    _decisions_panel(project, series_id, pages)
    history = plan_store.versions_of_series(project.id, series_id)
    if len(history) > 1:
        with st.expander(f"历史记录（{len(history)}）", expanded=False):
            for item in reversed(history):
                st.caption(f"{item.confirmed_at or item.created_at}")
    with st.expander("方案要点（研究问题 / 数据需求 / 判据 / 指标）", expanded=False):
        theme.markdown(theme.field_rows([
            ("研究问题", theme.esc(display_text.strip_claim_refs(version.question) or "—")),
            ("数据需求", theme.esc(display_text.strip_claim_refs(version.data_requirements) or "—")),
            ("成功判据", theme.esc(display_text.strip_claim_refs(version.success_criteria) or "—")),
            ("指标", theme.esc("；".join(version.metrics) or "—")),
            ("用户设定", theme.esc(display_text.strip_claim_refs(version.notes) or "—")),
        ]))
    with st.expander("全部文献依据与参数", expanded=False):
        if version.parameters:
            _param_lines(version.parameters)
        else:
            st.caption("没有全局参数。")
        for step in plan_store.step_lines(version):
            st.markdown(f"**{step.get('order')}. {step.get('title', '')}**")
            _evidence_lines(step.get("evidence") or [], project.id, f"plan_all_{step.get('order')}")
            _param_lines(step.get("params") or [])


# ---------------------------------------------------------------------------
# 渲染入口
# ---------------------------------------------------------------------------


def _resolve_selection(selection: str, drafts: list[PlanDraft], versions: list[PlanVersion]):
    kind, _, obj_id = str(selection or "").partition(":")
    if kind == "draft":
        return next((d for d in drafts if d.id == obj_id), None), None
    if kind == "version":
        return None, next((v for v in versions if v.id == obj_id), None)
    return None, None


def _detail_pane(
    project: Project, groups: list[dict], papers_by_id: dict[str, Paper], claims: list,
    drafts: list[PlanDraft], versions: list[PlanVersion], selection: str,
) -> None:
    # 生成结果只是一次性提示：渲染一次就清掉，避免确认之后还挂着过期的话
    record = st.session_state.pop(_outcome_key(project.id), None)
    if isinstance(record, dict):
        _render_outcome(record)

    _, mapping = _selection_options(groups)
    draft, version = _resolve_selection(mapping.get(selection, selection), drafts, versions)
    if draft is not None:
        _draft_view(project, draft, claims, papers_by_id)
        st.divider()
    elif version is not None:
        _version_view(project, version, display_text.page_map(claims), claims)
        st.divider()
    else:
        _start_block(project, claims, papers_by_id)

    _new_series_block(project, claims, papers_by_id)


def _start_block(project: Project, claims: list, papers_by_id: dict[str, Paper]) -> None:
    """还没有任何方案时：先起一个主题，再用勾选的条目生成。"""
    st.subheader("开始第一个方案")
    labels = [text for _, text in MODE_CHOICES]
    choice = st.radio("方案类型", labels, key=f"start_mode_{project.id}")
    title = st.text_input(
        "方案主题", key=f"start_title_{project.id}",
        placeholder="例如：Bensen 2007 单日互相关链路复现",
    )
    user_notes = st.text_area("补充要求（用户设定）", key=f"plan_user_notes_{project.id}", height=70)
    selected = _claim_picker(project, claims, papers_by_id)
    if not selected:
        return
    if not llm_settings.require_connection("生成复现方案"):
        return
    if st.button("生成计划草稿", type="primary", key=f"plan_generate_{project.id}"):
        mode = MODE_CHOICES[labels.index(choice)][0]
        draft = plan_store.start_new_series(project, mode, title)
        _run_generate(project, mode, selected, papers_by_id, user_notes, into=draft)


def render(project: Project) -> None:
    papers = repo.list_all(Paper, project.id)
    papers_by_id = {paper.id: paper for paper in papers}
    claims = repo.claims_of_project(project.id)
    drafts = plan_store.list_drafts(project.id)
    versions = plan_store.list_versions(project.id)
    groups = plan_store.list_series(project.id)
    pending_draft_id = str(
        st.session_state.pop("_graph_open_draft", "") or ""
    )
    if pending_draft_id:
        options, mapping = _selection_options(groups)
        wanted = mapping.get(pending_draft_id, f"draft:{pending_draft_id}")
        match = next(
            (
                option for option in options
                if mapping.get(option) == wanted
            ),
            "",
        )
        if match:
            st.session_state[_pick_key(project.id)] = match

    runs = steps_mod.runs_by_id(project.id)
    progress_by_series: dict[str, dict] = {}
    for group in groups:
        item = group["latest_version"] or (group["drafts"][0] if group["drafts"] else None)
        if item is None:
            continue
        serial = repo.list_step_states(project.id, group["series_id"])
        merged = steps_mod.merge(list(getattr(item, "steps", []) or []), serial, runs)
        progress_by_series[group["series_id"]] = steps_mod.progress(merged)

    def _content() -> None:
        options, _ = _selection_options(groups)
        # 渲染前先归一化选中项：索引在右侧时 detail 先于 index 渲染，
        # 若不提前写回，detail 会读到被清空的选中值而落到空块。
        if options:
            key = _pick_key(project.id)
            if st.session_state.get(key) not in options:
                st.session_state[key] = options[0]
        if pdf_workspace.visible(project.id):
            st.selectbox("方案与版本", options, key=_pick_key(project.id),
                         help="双栏时索引收成一行")
            _detail_pane(
                project, groups, papers_by_id, claims, drafts, versions,
                st.session_state.get(_pick_key(project.id), ""),
            )
            return
        split_layout.render(
            workbench="planner",
            index_title="方案与版本",
            index_render=lambda: _index_pane(project, groups, progress_by_series),
            detail_render=lambda: _detail_pane(
                project, groups, papers_by_id, claims, drafts, versions,
                st.session_state.get(_pick_key(project.id), ""),
            ),
            index_height=INDEX_HEIGHT,
            index_side="right",
            index_controls_label="索引",
            collapsed_rail_render=lambda: split_layout.collapsed_rail_button(
                "planner", project.id
            ),
        )

    pdf_workspace.render(project, _content)

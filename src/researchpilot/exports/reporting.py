"""成果报告生成（M3 最小实现）。

报告只写**库里真实存在的东西**：

- 文献证据来自 ``PaperClaim``（带逐字引文与页码），不是模型复述；
- 目标与方法来自已确认的 ``PlanVersion``；
- 实验参数、结果与图来自 ``ExperimentRun`` + 真实产物；
- 缺失的部分写「未执行 / 尚无证据」，**不编结论、不补示意图**。

AI 辅助内容（对话建议、AI 推断步骤）单独标注来源，不与已验证事实混排。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import display_text
from ..agent import plan_schemas
from ..planning import draft as plan_store
from ..planning import steps as steps_mod
from ..storage import repository as repo
from ..storage.models import (
    Artifact,
    Certainty,
    DecisionStatus,
    ExperimentRun,
    Goal,
    GoalAssessment,
    Paper,
    RunStatus,
)

NO_EVIDENCE = "尚无证据"
NOT_RUN = "未执行"

_CERTAINTY_MARK: dict[Certainty, str] = {
    Certainty.STATED: "原文事实（逐字命中）",
    Certainty.IMPLIED: "疑似转述（待人工核查）",
    Certainty.INFERRED: "AI 推断（未核实）",
}


@dataclass
class ReportSection:
    key: str
    title: str
    lines: list[str] = field(default_factory=list)
    empty_reason: str = ""
    # 界面用的结构化块：导出仍走 lines，界面按块渲染（徽标 + 折叠引文）
    blocks: list[dict] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool([line for line in self.lines if line.strip()])


@dataclass
class ReportDoc:
    project_id: str
    project_title: str
    generated_at: str
    sections: list[ReportSection]

    @property
    def gaps(self) -> list[str]:
        return [
            f"{item.title}：{item.empty_reason}"
            for item in self.sections
            if not item.has_content
        ]

    @property
    def is_empty(self) -> bool:
        """有没有任何**真实内容**。

        "证据来源说明"那一节总会用几句话描述统计口径，所以判断空不空要看它以外的章节：
        否则一个什么都没有的项目也会被当成"有报告"。
        """
        return not any(
            item.has_content for item in self.sections if item.key != "provenance"
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _paper_title(papers: dict[str, Paper], paper_id: str) -> str:
    paper = papers.get(paper_id)
    return getattr(paper, "title", "") or "未命名文献"


def _literature_section(project_id: str) -> ReportSection:
    section = ReportSection(key="literature", title="一、研究背景与文献证据")
    papers = {paper.id: paper for paper in repo.list_all(Paper, project_id)}
    claims = repo.claims_of_project(project_id)
    if not claims:
        section.empty_reason = f"{NO_EVIDENCE}（还没有通过原文校验的文献条目）"
        return section

    by_paper: dict[str, list] = {}
    for claim in claims:
        by_paper.setdefault(claim.paper_id, []).append(claim)

    stated = sum(1 for claim in claims if claim.certainty is Certainty.STATED)
    implied = sum(1 for claim in claims if claim.certainty is Certainty.IMPLIED)
    section.lines.append(
        f"共 {len(claims)} 条已分析条目：原文事实 {stated} 条、疑似转述 {implied} 条。"
        "疑似转述与 AI 推断只作线索，不作为已证实结论。"
    )
    section.lines.append("")
    for paper_id, items in by_paper.items():
        title = _paper_title(papers, paper_id)
        section.lines.append(f"**《{title}》**")
        section.blocks.append({"kind": "paper", "title": title})
        for claim in items:
            page = f"PDF · 第 {claim.page} 页" if claim.page else (claim.locator or "出处未定位")
            mark = _CERTAINTY_MARK.get(claim.certainty, "未核实")
            section.lines.append(f"- {claim.claim}（{mark}，{page}）")
            if claim.quote:
                section.lines.append(f"  - 原文引文：{claim.quote}")
            section.blocks.append({
                "kind": "claim",
                "claim_id": claim.id,
                "paper_id": claim.paper_id,
                "text": claim.claim,
                "quote": claim.quote,
                "page_label": page,
                "certainty_label": mark,
                "verified": claim.certainty is Certainty.STATED,
                "paper": title,
            })
        section.lines.append("")
    return section


def _plan_section(project_id: str) -> ReportSection:
    section = ReportSection(key="plan", title="二、已确认的研究目标与方法")
    versions = plan_store.list_versions(project_id)
    goals = repo.list_all(Goal, project_id)
    if not versions and not goals:
        section.empty_reason = f"{NOT_RUN}（还没有确认过任何方案）"
        return section

    pages = display_text.page_map(repo.claims_of_project(project_id))
    for goal in goals:
        section.lines.append(
            f"**研究目标**：{display_text.humanize_claim_refs(goal.statement, pages)}"
        )
        details = []
        if goal.metric:
            details.append(f"指标：{display_text.strip_claim_refs(goal.metric)}")
        if goal.acceptance:
            details.append(f"成功判据：{display_text.strip_claim_refs(goal.acceptance)}")
        if details:
            section.lines.append("　".join(details))
        section.lines.append("")

    for version in sorted(versions, key=lambda item: item.version):
        section.lines.append(
            f"**版本 v{version.version}**（{plan_schemas.mode_label(version.mode)}，"
            f"确认于 {version.confirmed_at or version.created_at}）"
        )
        if version.question:
            section.lines.append(f"- 研究问题：{display_text.humanize_claim_refs(version.question, pages)}")
        if version.data_requirements:
            section.lines.append(f"- 数据需求：{display_text.humanize_claim_refs(version.data_requirements, pages)}")
        if version.metrics:
            section.lines.append(
                f"- 指标：{'；'.join(display_text.strip_claim_refs(m) for m in version.metrics)}"
            )
        for step in plan_store.step_lines(version):
            label = plan_schemas.origin_label(step.get("origin", "ai"))
            section.lines.append(
                f"  {step.get('order')}. "
                f"{display_text.strip_claim_refs(step.get('title', ''))}（来源：{label}）"
            )
            if step.get("method"):
                section.lines.append(
                    f"     - 做法：{display_text.humanize_claim_refs(step['method'], pages)}"
                )
            if step.get("expected_metric"):
                section.lines.append(
                    f"     - 预期指标：{display_text.humanize_claim_refs(step['expected_metric'], pages)}"
                )
            for evidence in step.get("evidence") or []:
                page = f"第 {evidence['page']} 页" if evidence.get("page") else "出处未定位"
                section.lines.append(f"     - 依据：{evidence.get('claim', '')}（{page}）")
            for param in step.get("params") or []:
                section.lines.append(
                    f"     - 建议参数：{param.get('name', '')} = "
                    f"{param.get('suggested_value', '')}{param.get('unit', '')}"
                    f"（{plan_schemas.param_origin_label(param.get('origin', 'suggested'))}）"
                )
        section.lines.append("")

    accepted = repo.list_decisions(project_id, status=DecisionStatus.ACCEPTED)
    if accepted:
        section.lines.append("**重要科研决策（已采纳）**")
        for decision in accepted:
            where = f"，落成版本 {decision.plan_version_id}" if decision.plan_version_id else ""
            section.lines.append(f"- {decision.text}（{decision.kind.value}{where}）")
        section.lines.append("")
    return section


def _method_outline_section(project_id: str) -> ReportSection:
    section = ReportSection(
        key="method_outline",
        title="方法演进大纲（引用图谱导出）",
    )
    artifacts = sorted(
        (
            item for item in repo.list_all(Artifact, project_id)
            if item.type == "report"
            and Path(item.path).name.startswith("method-evolution-outline-")
        ),
        key=lambda item: item.created_at,
        reverse=True,
    )
    if not artifacts:
        section.empty_reason = "尚未从引用关系图谱导出方法演进大纲"
        return section
    artifact = artifacts[0]
    try:
        section.lines.extend(
            Path(artifact.path).read_text(encoding="utf-8").splitlines()
        )
    except OSError as exc:
        section.empty_reason = f"大纲文件无法读取：{type(exc).__name__}"
    return section


def _run_section(project_id: str) -> ReportSection:
    section = ReportSection(key="runs", title="三、真实实验参数与处理流程")
    runs = repo.list_all(ExperimentRun, project_id)
    if not runs:
        section.empty_reason = f"{NOT_RUN}（本项目还没有实验记录）"
        return section
    for index, run in enumerate(sorted(runs, key=lambda item: item.created_at), start=1):
        section.lines.append(
            f"**运行 {index}**（{run.time_window or '时间窗未记录'}）　状态：{run.status.value}　"
            f"台站：{'、'.join(run.stations) or '未记录'}　引擎：{run.engine or '未记录'}"
        )
        if run.duration_s:
            section.lines.append(f"- 耗时：{run.duration_s:.1f}s　退出码：{run.exit_code}")
        parameters = _run_parameters(run)
        if parameters:
            readable = "；".join(f"{key}={value}" for key, value in parameters.items())
            section.lines.append(f"- 实际参数（来自该次运行的 run.json）：{readable[:400]}")
        actual = (run.summary or {}).get("preprocess", {})
        if isinstance(actual, dict) and actual.get("actual_steps"):
            section.lines.append("- 实际处理步骤（来自运行产物）：")
            for step in actual["actual_steps"]:
                section.lines.append(f"    - {step}")
        if run.errors:
            section.lines.append(f"- 错误：{'；'.join(run.errors[:3])}")
        section.lines.append("")
    return section


def _run_parameters(run: ExperimentRun) -> dict:
    """从该次运行的 run.json 里读回真实参数（只读；读不到就返回空，不猜）。"""
    import json

    work_dir = Path(run.work_dir or "")
    if not work_dir:
        return {}
    path = work_dir / "run.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return {}
    return {
        key: value for key, value in parameters.items()
        if isinstance(value, (int, float, str, list)) and value not in ("", None)
    }


def _result_section(project_id: str) -> ReportSection:
    section = ReportSection(key="results", title="四、已执行实验的结果、图与日志")
    runs = repo.list_all(ExperimentRun, project_id)
    succeeded = [run for run in runs if run.status is RunStatus.SUCCEEDED]
    if not succeeded:
        section.empty_reason = f"{NOT_RUN}（没有执行成功的实验；未成功的运行不作为结果呈现）"
        return section
    artifacts = repo.list_all(Artifact, project_id)
    for index, run in enumerate(succeeded, start=1):
        summary = run.summary or {}
        section.lines.append(f"**运行 {index}**（{run.time_window or '时间窗未记录'}）")
        for stage in ("qc", "preprocess", "xcorr", "stack"):
            payload = summary.get(stage)
            if isinstance(payload, dict) and payload:
                metrics = "；".join(
                    f"{key}={value}"
                    for key, value in payload.items()
                    if isinstance(value, (int, float, str)) and key != "stations"
                )
                if metrics:
                    section.lines.append(f"- {stage}：{metrics[:400]}")
                # 台站对 / 叠加这类结果是"列表里的一行"，逐项把标量字段写清楚
                for key in ("pairs", "stacks"):
                    rows = payload.get(key)
                    if not (isinstance(rows, list) and rows and isinstance(rows[0], dict)):
                        continue
                    row = rows[0]
                    detail = "；".join(
                        f"{item}={value}"
                        for item, value in row.items()
                        if isinstance(value, (int, float, str)) and value not in ("", None)
                    )
                    if detail:
                        section.lines.append(f"- {stage}（{key[:-1]}）：{detail[:400]}")
        mine = [item for item in artifacts if item.run_id == run.id]
        if mine:
            section.lines.append(f"- 产物 {len(mine)} 个（图 / 数据 / 日志，按真实文件登记）：")
            # 图优先且不截断：报告要能直接看到结果图；其余类型最多列 20 条。
            ordered = ([item for item in mine if item.type == "figure"]
                       + [item for item in mine if item.type != "figure"])
            listed = 0
            for artifact in ordered:
                if artifact.type != "figure" and listed >= 20:
                    continue
                listed += 1
                section.lines.append(
                    f"    - [{artifact.type}] {artifact.path}"
                    + (f"（sha256 {artifact.sha256[:12]}…）" if artifact.sha256 else "")
                )
                # 图要能在报告里直接看到：路径相对**仓库根目录**，链接指向真实产物文件。
                # 只有真登记成 figure 的产物才画，不补任何示意图。
                if artifact.type == "figure" and artifact.path:
                    relative = f"outputs/runs/{run.id}/{artifact.path}"
                    section.lines.append(
                        f"      ![运行 {run.id} 的图：{artifact.path}]({relative})"
                    )
        section.lines.append("")
    return section


def _assessment_section(project_id: str) -> ReportSection:
    section = ReportSection(key="assessment", title="五、目标达成情况与尚未解决的问题")
    goals = repo.list_all(Goal, project_id)
    assessments = repo.list_all(GoalAssessment)
    # 报告是给用户看的：状态写中文，不把内部枚举值端上去
    status_label = {
        "done": "达成",
        "partial": "部分达成",
        "not_done": "未达成",
        "failed": "失败",
    }
    if not goals:
        section.empty_reason = f"{NOT_RUN}（还没有确认的研究目标）"
        return section
    for goal in goals:
        mine = [item for item in assessments if item.goal_id == goal.id]
        if mine:
            latest = sorted(mine, key=lambda item: item.created_at)[-1]
            label = status_label.get(latest.status.value, latest.status.value)
            section.lines.append(
                f"- {display_text.strip_claim_refs(goal.statement)}：{label}"
                f"（{latest.reason or '未记录理由'}）"
            )
            if latest.evidence_ids:
                section.lines.append(
                    f"    - 依据：{', '.join(latest.evidence_ids)}"
                    + (f"；下一步：{latest.next_step}" if latest.next_step else "")
                )
        else:
            section.lines.append(f"- {goal.statement}：未评估（还没有做目标对照）")

    pending: list[str] = []
    for group in plan_store.list_series(project_id):
        version = group.get("latest_version")
        if version is None:
            continue
        states = repo.list_step_states(project_id, group["series_id"])
        merged = steps_mod.merge(version.steps, states, steps_mod.runs_by_id(project_id))
        for step in merged:
            if step["status"].value not in {"done", "skipped"}:
                pending.append(f"{group['title']} 第 {step['order']} 步（{step['status_label']}）")
    if pending:
        section.lines.append("")
        section.lines.append("**尚未完成或未记录的步骤**")
        section.lines.append(
            "- 步骤的完成状态**只来自人工勾选**；真实实验执行成功不等于某一步已被确认完成，"
            "两者在界面上分开记录（真实运行见第四节）。"
        )
        for item in pending:
            section.lines.append(f"- {item}")
    section.lines.append("")
    return section


def _provenance_section(project_id: str) -> ReportSection:
    section = ReportSection(key="provenance", title="六、证据来源与 AI 辅助内容说明")
    claims = repo.claims_of_project(project_id)
    messages = repo.list_messages(project_id)
    versions = plan_store.list_versions(project_id)
    runs = repo.list_all(ExperimentRun, project_id)
    succeeded = [run for run in runs if run.status is RunStatus.SUCCEEDED]
    section.lines.append(
        f"- 文献证据：{len(claims)} 条，全部来自原文逐字引文定位（见第一节）。"
    )
    section.lines.append(
        f"- 已确认方案：{len(versions)} 个版本；版本中标为「AI 推断」的步骤未经文献验证。"
    )
    section.lines.append(
        f"- AI 讨论记录：{len(messages)} 条。**对话内容属于讨论，不是已验证的科研事实**，"
        "也不作为本报告的结论依据。"
    )
    section.lines.append(
        f"- 实验数据：{len(runs)} 次运行，其中成功 {len(succeeded)} 次；"
        "报告中的实验数字与图只取自这些真实运行。"
    )
    if not succeeded:
        section.lines.append(f"- 尚未执行的实验：{NOT_RUN}。")
    return section


def build(project) -> ReportDoc:
    """按项目生成报告。传入 Project 对象（调用方已经查过，不重复查库）。"""
    sections = [
        _literature_section(project.id),
        _method_outline_section(project.id),
        _plan_section(project.id),
        _run_section(project.id),
        _result_section(project.id),
        _assessment_section(project.id),
        _provenance_section(project.id),
    ]
    return ReportDoc(
        project_id=project.id,
        project_title=project.title,
        generated_at=_now(),
        sections=sections,
    )


def to_markdown(doc: ReportDoc) -> str:
    """导出 Markdown。没有内容的章节写「未执行 / 尚无证据」，不留空话。"""
    lines = [
        f"# {doc.project_title} —— 研究成果报告",
        "",
        f"- 生成时间：{doc.generated_at}",
        "",
        "> 本报告只收录库中真实存在的内容：文献证据带逐字引文与页码，"
        "实验数字只来自执行成功的运行记录。**没有真实数据的部分写「未执行 / 尚无证据」。**",
        "> 图片链接相对**仓库根目录**（`outputs/runs/<运行>/…`），"
        "在仓库目录下打开本报告即可看到真实结果图。",
        "",
    ]
    for section in doc.sections:
        lines.append(f"## {section.title}")
        lines.append("")
        if section.has_content:
            lines.extend(section.lines)
        else:
            lines.append(f"**{section.empty_reason or NO_EVIDENCE}**")
        lines.append("")
    if doc.gaps:
        lines.append("## 尚未完成的部分")
        lines.append("")
        for gap in doc.gaps:
            lines.append(f"- {gap}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"

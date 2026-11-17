"""草稿 → 确认 → 正式版本：计划生成闭环的落库一侧（P0-1）。

三条不可破的规则在这里落地，不靠界面自觉：

1. **确认才生效**：``PlanVersion`` 与 ``Goal`` 只在 ``confirm_draft`` 里产生。
   草稿单独存在 ``plan_drafts`` 表，既不计入"已定目标"，也不进项目导出
   （导出排除规则见 ``storage/repository.py``）。
2. **版本号自增且留历史**：同一项目 + 同一模式的版本号从 1 开始递增，
   确认过的版本不覆盖、不删除；重复确认同一份草稿返回已有版本，不新增。
3. **确认后不可再改**：已确认的草稿要改，就再生成一份新草稿，别去动历史版本。
"""

from __future__ import annotations

from datetime import datetime

from ..agent import plan_schemas
from ..storage import repository as repo
from ..storage.models import (
    DecisionStatus,
    Goal,
    PlanDraft,
    PlanDraftStatus,
    PlanMode,
    PlanVersion,
)

# 用户在界面上可以改的文本字段（改完仍是草稿，要重新确认）
EDITABLE_TEXT_FIELDS: tuple[str, ...] = (
    "question",
    "objective",
    "data_requirements",
    "success_criteria",
    "user_notes",
)
EDITABLE_STEP_FIELDS: tuple[str, ...] = (
    "title",
    "purpose",
    "method",
    "expected_metric",
    "risk",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_drafts(project_id: str, status: PlanDraftStatus | None = None) -> list[PlanDraft]:
    return repo.list_drafts(project_id, status)


def list_versions(project_id: str, mode: PlanMode | None = None) -> list[PlanVersion]:
    """已确认的版本，版本号大的在前。"""
    versions = repo.list_all(PlanVersion, project_id)
    if mode is not None:
        versions = [v for v in versions if v.mode is mode]
    return sorted(versions, key=lambda v: (v.version, v.confirmed_at or v.created_at), reverse=True)


def series_of(item) -> str:
    """一项计划属于哪个系列。

    v1.7 之前建的草稿与版本没有 ``series_id``，这里按模式归到一个"历史系列"，
    于是它们的版本号继续接着排（不会因为这次增量而跳号或重置）。
    """
    series = getattr(item, "series_id", "") or ""
    if series:
        return series
    mode = getattr(item, "mode", PlanMode.REPRODUCE)
    value = getattr(mode, "value", mode)
    return f"legacy:{value}"


def series_title_of(item) -> str:
    """系列显示名：显式标题 → 研究目标前 24 字 → 模式名。

    P0-3：旧记录没有 series_title，之前一律显示"未命名方案"，同一个系列下的草稿与版本
    看起来就像两个不同的方案。用目标文字兜底后，同一系列的条目会显示同一个名字。
    """
    title = (getattr(item, "series_title", "") or "").strip()
    if title:
        return title
    objective = (getattr(item, "objective", "") or "").strip()
    if item is not None and not objective and isinstance(item, PlanDraft):
        objective = (item.question or "").strip()
    if objective:
        return objective[:24] + ("…" if len(objective) > 24 else "")
    return plan_schemas.mode_label(getattr(item, "mode", PlanMode.REPRODUCE))


def new_series_id() -> str:
    return repo.new_id("series")


def next_version_number(project_id: str, series_id: str) -> int:
    """版本号在**系列内**自增：另起新方案的版本重新从 1 开始。"""
    versions = [
        v for v in repo.list_all(PlanVersion, project_id)
        if series_of(v) == series_id
    ]
    return max((v.version for v in versions), default=0) + 1


def versions_of_series(project_id: str, series_id: str) -> list[PlanVersion]:
    versions = [
        v for v in repo.list_all(PlanVersion, project_id) if series_of(v) == series_id
    ]
    return sorted(versions, key=lambda v: (v.version, v.confirmed_at or v.created_at))


def drafts_of_series(project_id: str, series_id: str) -> list[PlanDraft]:
    drafts = [
        d for d in repo.list_all(PlanDraft, project_id) if series_of(d) == series_id
    ]
    return sorted(drafts, key=lambda d: (d.created_at, d.id), reverse=True)


def list_series(project_id: str) -> list[dict]:
    """项目下的全部计划系列：一个研究主题一条，含历代版本与在写的草稿。"""
    series_ids: list[str] = []
    for item in [*repo.list_all(PlanVersion, project_id), *repo.list_all(PlanDraft, project_id)]:
        key = series_of(item)
        if key not in series_ids:
            series_ids.append(key)

    groups: list[dict] = []
    for key in series_ids:
        versions = versions_of_series(project_id, key)
        drafts = drafts_of_series(project_id, key)
        sample = (versions[-1] if versions else (drafts[0] if drafts else None))
        groups.append({
            "series_id": key,
            "title": series_title_of(sample) if sample is not None else key,
            "mode": sample.mode if sample is not None else PlanMode.REPRODUCE,
            "versions": versions,
            "drafts": drafts,
            "latest_version": versions[-1] if versions else None,
        })
    groups.sort(
        key=lambda group: (
            group["latest_version"].confirmed_at if group["latest_version"] else "",
            group["series_id"],
        ),
        reverse=True,
    )
    return groups


def start_new_series(project: Project, mode: PlanMode, title: str = "") -> PlanDraft:
    """另起一个新方案：新主题 = 新系列，版本历史从 1 开始，不与旧主题混排。"""
    now = _now()
    draft = PlanDraft(
        id=repo.new_id("draft"),
        project_id=project.id,
        mode=mode,
        status=PlanDraftStatus.DRAFT,
        series_id=new_series_id(),
        series_title=(title or "").strip() or plan_schemas.mode_label(mode),
        objective=(title or "").strip(),
        origin="user",
        generated_at="",
        created_at=now,
        updated_at=now,
    )
    return repo.save(draft)


def create_revision(source: PlanVersion | PlanDraft, *, note: str = "") -> PlanDraft:
    """基于已确认版本（或一份草稿）派生一份**可修改草稿**，继续完善当前方案。

    原版本一个字段都不动；新草稿确认后成为同一系列的下一版。
    """
    if isinstance(source, PlanVersion):
        origin_version_id = source.id
    else:
        origin_version_id = source.confirmed_version_id or source.derived_from_version_id

    now = _now()
    draft = PlanDraft(
        id=repo.new_id("draft"),
        project_id=source.project_id,
        mode=source.mode,
        status=PlanDraftStatus.DRAFT,
        question=source.question,
        objective=source.objective,
        data_requirements=source.data_requirements,
        steps=[dict(step) for step in source.steps],
        parameters=[dict(item) for item in getattr(source, "parameters", [])],
        metrics=list(getattr(source, "metrics", []) or []),
        success_criteria=source.success_criteria,
        expected_outputs=list(getattr(source, "expected_outputs", []) or []),
        risks=list(getattr(source, "risks", []) or []),
        evidence=[dict(item) for item in getattr(source, "evidence", [])],
        user_notes=source.notes or getattr(source, "user_notes", ""),
        warnings=list(getattr(source, "warnings", []) or []),
        claim_count=len(getattr(source, "evidence", []) or []),
        evidence_count=sum(
            1 for step in source.steps
            if isinstance(step, dict) and step.get("origin") == "evidence"
        ),
        ai_step_count=sum(
            1 for step in source.steps
            if not isinstance(step, dict) or step.get("origin") != "evidence"
        ),
        origin="user",
        series_id=source.series_id,
        series_title=source.series_title,
        derived_from_version_id=origin_version_id,
        generated_at="",
        created_at=now,
        updated_at=now,
    )
    if note.strip():
        draft.user_notes = (draft.user_notes + "\n" + note.strip()).strip()
    return repo.save(draft)


def version_of_draft(project_id: str, draft_id: str) -> PlanVersion | None:
    """某份草稿确认出来的版本（用于判断"是不是已经确认过了"）。"""
    for version in list_versions(project_id):
        if version.source_draft_id == draft_id:
            return version
    return None


def confirm_draft(
    draft: PlanDraft,
    *,
    statement: str = "",
    user_notes: str | None = None,
) -> tuple[PlanVersion, bool]:
    """把草稿确认为正式版本。

    返回 ``(版本, 是否本次新建)``。重复确认同一份草稿返回已有版本，
    第二个值为 False——**不会**因为多点一次按钮就多出一个版本。
    """
    if draft.status is PlanDraftStatus.CONFIRMED and draft.confirmed_version_id:
        existing = repo.get(PlanVersion, draft.confirmed_version_id)
        if existing is not None:
            return existing, False
        # 版本记录被删过：重新落一版，避免出现"已确认却没有版本"的悬空状态

    now = _now()
    notes = draft.user_notes if user_notes is None else user_notes
    goal_statement = (statement or draft.objective or draft.question or "").strip()
    if not goal_statement:
        goal_statement = "未命名研究目标"

    # 目标复用规则：同一项目下表述完全相同的目标只保留一条。
    # 否则同一研究目标确认两次就变成两个"已定目标"，统计数字会失真。
    goal = next(
        (
            item for item in repo.list_all(Goal, draft.project_id)
            if (item.statement or "").strip() == goal_statement
        ),
        None,
    )
    if goal is None:
        goal = Goal(
            id=repo.new_id("goal"),
            project_id=draft.project_id,
            statement=goal_statement,
            metric="；".join(draft.metrics),
            acceptance=draft.success_criteria,
            initial_status="未开始",
            created_at=now,
        )
        repo.save(goal)

    series_id = series_of(draft)
    series_title = series_title_of(draft)
    # 系列标题以草稿为准回写：修订草稿可能改了标题，版本与草稿要一致。
    if draft.series_title != series_title:
        draft.series_title = series_title

    version = PlanVersion(
        id=repo.new_id("plan"),
        project_id=draft.project_id,
        mode=draft.mode,
        version=next_version_number(draft.project_id, series_id),
        steps=[dict(step) for step in draft.steps],
        goal_ids=[goal.id],
        data_requirements=draft.data_requirements,
        risks=list(draft.risks),
        metrics=list(draft.metrics),
        notes=notes,
        question=draft.question,
        objective=draft.objective,
        success_criteria=draft.success_criteria,
        expected_outputs=list(draft.expected_outputs),
        evidence=[dict(item) for item in draft.evidence],
        parameters=[dict(item) for item in draft.parameters],
        created_by="llm",
        source_draft_id=draft.id,
        confirmed_at=now,
        series_id=draft.series_id,
        series_title=draft.series_title,
        derived_from_version_id=draft.derived_from_version_id,
        created_at=now,
    )
    repo.save(version)

    draft.status = PlanDraftStatus.CONFIRMED
    draft.user_notes = notes
    draft.confirmed_at = now
    draft.confirmed_version_id = version.id
    draft.updated_at = now
    repo.save(draft)

    # 已采纳的决策挂上这一版：成果报告要说清"哪条决策落成了哪个版本"。
    for decision in repo.list_decisions(draft.project_id, status=DecisionStatus.ACCEPTED):
        if decision.draft_id == draft.id and decision.plan_version_id != version.id:
            decision.plan_version_id = version.id
            decision.updated_at = now
            repo.save(decision)
    return version, True


def apply_edits(
    draft: PlanDraft,
    *,
    text: dict[str, str] | None = None,
    steps: dict[int, dict[str, str]] | None = None,
    extra_params: list[dict] | None = None,
) -> PlanDraft:
    """把界面上的修改写回草稿。

    - 文本字段与步骤文本直接覆盖，被改过的步骤标 ``edited_by_user``；
    - 用户自己加的参数 ``origin="user"``，与模型的"建议参数"分开；
    - 已确认的草稿不许改——要改就再生成一份草稿，要留版本历史。
    """
    if draft.status is PlanDraftStatus.CONFIRMED:
        raise ValueError("已确认的版本不可修改；如需调整，请基于文献条目重新生成一份草稿。")

    for key, value in (text or {}).items():
        if key not in EDITABLE_TEXT_FIELDS:
            continue
        setattr(draft, key, str(value))

    for index, changes in (steps or {}).items():
        if not (0 <= index < len(draft.steps)):
            continue
        step = dict(draft.steps[index])
        touched = False
        for key, value in changes.items():
            if key not in EDITABLE_STEP_FIELDS:
                continue
            if str(step.get(key, "")) != str(value):
                touched = True
            step[key] = str(value)
        if touched:
            step["edited_by_user"] = True
        draft.steps[index] = step

    for item in extra_params or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        draft.parameters.append(plan_schemas.DraftParam(
            name=name,
            suggested_value=str(item.get("suggested_value") or ""),
            unit=str(item.get("unit") or ""),
            reason=str(item.get("reason") or ""),
            origin="user",
        ).model_dump())

    draft.revision += 1
    draft.updated_at = _now()
    return repo.save(draft)


def step_lines(version: PlanVersion) -> list[dict]:
    """把版本里的步骤统一成字典形式。

    旧版本可能存的是字符串列表（"一句话一步"），新版本存的是结构化字典。
    界面与导出都走这个函数，就不会因为两种历史格式而漏渲染或报错。
    """
    lines: list[dict] = []
    for index, step in enumerate(version.steps, start=1):
        if isinstance(step, dict):
            lines.append({**step, "order": step.get("order") or index})
        else:
            lines.append({"order": index, "title": str(step), "origin": "ai", "evidence": []})
    return lines


def counts(project_id: str) -> dict[str, int]:
    """规划页顶部用的一组计数。草稿与已确认分开数，不混在一起。"""
    drafts = list_drafts(project_id)
    versions = list_versions(project_id)
    return {
        "drafts": sum(1 for d in drafts if d.status is PlanDraftStatus.DRAFT),
        "confirmed": sum(1 for d in drafts if d.status is PlanDraftStatus.CONFIRMED),
        "versions": len(versions),
        "goals": len(repo.list_all(Goal, project_id)),
        "series": len(list_series(project_id)),
    }

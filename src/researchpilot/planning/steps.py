"""步骤进度、待办与备忘（P0-C）。

三条纪律：

1. **人工状态与引擎执行分开**：``PlanStepState.status`` 是用户自己勾的；
   "这一步引擎真的跑成功了吗"只从 ``ExperimentRun.status`` 读，两者不互相推导。
2. **没有引擎就不给运行入口**：``engine_stage`` 只允许填真实存在的计算阶段，
   频散测量这类现有引擎做不了的步骤，界面上只显示"需人工执行并记录"。
3. 状态按 ``(项目, 系列, 步骤序号)`` 存：同一主题的历次修订共用进度，
   换项目或另起新方案都不会串。
"""

from __future__ import annotations

from datetime import datetime

from ..storage import repository as repo
from ..storage.models import ExperimentRun, PlanStepState, RunStatus, StepStatus

# 只有这里列出的阶段才是引擎真能跑的（与 engine/pipeline.STAGE_ORDER 一致）
ENGINE_STAGES: dict[str, str] = {
    "qc": "质控",
    "preprocess": "预处理",
    "xcorr": "互相关",
    "stack": "叠加",
}

STATUS_LABELS: dict[StepStatus, str] = {
    StepStatus.TODO: "未开始",
    StepStatus.DOING: "进行中",
    StepStatus.DONE: "已完成",
    StepStatus.SKIPPED: "跳过",
}

STATUS_MARKS: dict[StepStatus, str] = {
    StepStatus.TODO: "○",
    StepStatus.DOING: "◐",
    StepStatus.DONE: "●",
    StepStatus.SKIPPED: "－",
}

RUN_STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.PENDING: "已排队",
    RunStatus.RUNNING: "运行中",
    RunStatus.SUCCEEDED: "执行成功",
    RunStatus.FAILED: "执行失败",
    RunStatus.CANCELLED: "已取消",
    RunStatus.SKIPPED: "未执行",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stage_label(stage: str) -> str:
    return ENGINE_STAGES.get(stage, "")


def has_engine(stage: str) -> bool:
    """这一步有没有可用的计算引擎。空值或未知阶段一律当作没有。"""
    return (stage or "") in ENGINE_STAGES


def state_for(project_id: str, series_id: str, order: int) -> PlanStepState:
    """取某一步的状态；还没有就返回一条**未落库**的默认状态。

    不自动落库：用户没动过这一步，库里就不该凭空多出一行。
    """
    existing = repo.find_step_state(project_id, series_id, order)
    if existing is not None:
        return existing
    return PlanStepState(
        id=repo.new_id("step"),
        project_id=project_id,
        series_id=series_id,
        order=order,
    )


def update_state(
    state: PlanStepState,
    *,
    status: StepStatus | None = None,
    todo: str | None = None,
    note: str | None = None,
    engine_stage: str | None = None,
    run_id: str | None = None,
) -> PlanStepState:
    """保存一步的状态。``engine_stage`` 不接受引擎做不了的阶段。"""
    if status is not None:
        state.status = status
    if todo is not None:
        state.todo = str(todo)
    if note is not None:
        state.note = str(note)
    if engine_stage is not None:
        stage = (engine_stage or "").strip()
        if stage and stage not in ENGINE_STAGES:
            raise ValueError(f"未知的计算阶段：{stage!r}；没有对应引擎的步骤不能标成可执行")
        state.engine_stage = stage
    if run_id is not None:
        state.run_id = str(run_id)
    state.updated_at = _now()
    return repo.save(state)


def runs_by_id(project_id: str) -> dict[str, ExperimentRun]:
    return {run.id: run for run in repo.list_all(ExperimentRun, project_id)}


def merge(
    steps: list,
    states: list[PlanStepState],
    runs: dict[str, ExperimentRun],
) -> list[dict]:
    """把步骤内容、人工状态、真实运行记录合成一份可渲染的列表。"""
    by_order = {state.order: state for state in states}
    merged: list[dict] = []
    for index, step in enumerate(steps, start=1):
        payload = dict(step) if isinstance(step, dict) else {"title": str(step), "origin": "ai"}
        order = payload.get("order") or index
        state = by_order.get(order) or by_order.get(index) or PlanStepState(
            id="", project_id="", series_id="", order=order
        )
        run = runs.get(state.run_id) if state.run_id else None
        merged.append({
            **payload,
            "order": order,
            "status": state.status,
            "status_label": STATUS_LABELS[state.status],
            "status_mark": STATUS_MARKS[state.status],
            "todo": state.todo,
            "note": state.note,
            "engine_stage": state.engine_stage,
            "has_engine": has_engine(state.engine_stage),
            "run_id": state.run_id,
            "run": run,
            "run_status": run.status if run else None,
            "run_label": RUN_STATUS_LABELS.get(run.status, "") if run else "",
            "state_id": state.id,
        })
    return merged


def progress(steps: list[dict]) -> dict:
    """顶部进度条用的一组计数。人工状态与引擎结果分别统计。"""
    total = len(steps)
    done = sum(1 for step in steps if step.get("status") is StepStatus.DONE)
    doing = sum(1 for step in steps if step.get("status") is StepStatus.DOING)
    run_ready = sum(1 for step in steps if step.get("has_engine"))
    run_succeeded = sum(
        1 for step in steps if step.get("run_status") is RunStatus.SUCCEEDED
    )
    run_failed = sum(
        1 for step in steps if step.get("run_status") is RunStatus.FAILED
    )
    return {
        "total": total,
        "done": done,
        "doing": doing,
        "engine_capable": run_ready,
        "engine_succeeded": run_succeeded,
        "engine_failed": run_failed,
    }

"""ExperimentRun 生命周期与产物登记。

设计要点（对应 M2-02B 的要求）：

1. **记录在数据库里，不在页面会话里**。页面刷新丢的是会话，不是运行记录；
   后台工作进程独立于任何浏览器连接。
2. **同一项目同时只允许一个运行中的任务**，重复提交会被拒绝，
   避免网页重复点击拉起一堆进程。
3. **取消要真的杀进程**：先落 `cancel_requested` 标志，再向进程组发 SIGTERM，
   工作进程收到信号后走正常收尾（把状态写成 cancelled）。
4. **孤儿运行要能自愈**：状态是 running 但进程已经不在时，下一次读取会把它
   标成失败并写明原因，不会永远挂在"运行中"。
"""

from __future__ import annotations

import os
import signal
from datetime import datetime
from pathlib import Path

from ..storage import repository as repo
from ..storage.models import Artifact, ExperimentRun, RunStatus
from . import results as results_mod

ACTIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_run(
    project_id: str,
    *,
    stations: list[str],
    time_window: str,
    work_dir: Path,
    plan_version_id: str = "",
    engine: str = "legacy_bensen",
    engine_version: str = "",
    interpreter: str = "",
    dependencies: dict | None = None,
    script_hashes: dict | None = None,
    inputs: list[str] | None = None,
) -> ExperimentRun:
    """建一条 pending 记录。此时还没有执行任何东西。"""
    run = ExperimentRun(
        id=repo.new_id("run"),
        project_id=project_id,
        plan_version_id=plan_version_id,
        engine=engine,
        engine_version=engine_version,
        stations=list(stations),
        time_window=time_window,
        work_dir=str(work_dir),
        interpreter=interpreter,
        dependencies=dict(dependencies or {}),
        script_hashes=dict(script_hashes or {}),
        inputs=list(inputs or []),
        status=RunStatus.PENDING,
    )
    return repo.save(run)


def get_run(run_id: str) -> ExperimentRun | None:
    return repo.get(ExperimentRun, run_id)


def list_runs(project_id: str) -> list[ExperimentRun]:
    return repo.list_all(ExperimentRun, project_id)


def active_runs(project_id: str) -> list[ExperimentRun]:
    return [run for run in list_runs(project_id) if run.status in ACTIVE_STATUSES]


def ensure_no_active_run(project_id: str) -> None:
    """同一项目只允许一个进行中的运行。"""
    running = active_runs(project_id)
    if running:
        raise RuntimeError(
            f"项目已有进行中的实验（{running[0].id}，状态 {running[0].status.value}）；"
            "请等它结束或先取消"
        )


def mark_running(run: ExperimentRun, *, command: str, pid: int, pgid: int) -> ExperimentRun:
    """标记为运行中。

    先重新读一次库里的状态：如果工作进程已经飞快地跑完并把状态写成终态，
    这里就不能用 RUNNING 覆盖它——否则一次已经结束的运行会永远显示"运行中"。
    """
    fresh = repo.get(ExperimentRun, run.id) or run
    if fresh.status is not RunStatus.PENDING:
        return fresh
    run = fresh
    run.status = RunStatus.RUNNING
    run.command = command
    run.pid = int(pid)
    run.pgid = int(pgid)
    run.started_at = run.started_at or _now()
    return repo.save(run)


def finish_run(run: ExperimentRun, *, status: RunStatus, exit_code: int | None,
               errors: list[str] | None = None, warnings: list[str] | None = None,
               summary: dict | None = None, duration_s: float | None = None,
               command: str | None = None) -> ExperimentRun:
    """收尾：状态、退出码、错误、摘要一次写清。失败也照样写，不伪装。

    先重新读一次库里的最新记录再合并：工作进程读到自己那份记录之后，
    用户可能已经点了取消（`cancel_requested=True`）。如果直接把自己那份旧对象
    写回去，就会把取消标志、pid、输入清单这些被别处更新的字段覆盖掉。
    """
    run = repo.get(ExperimentRun, run.id) or run
    run.status = status
    run.exit_code = exit_code
    run.ended_at = _now()
    if command is not None:
        run.command = command
    if errors:
        run.errors = list(run.errors) + [str(item) for item in errors]
    if warnings:
        run.warnings = list(run.warnings) + [str(item) for item in warnings]
    if summary is not None:
        run.summary = summary
    if duration_s is not None:
        run.duration_s = float(duration_s)
    return repo.save(run)


def register_artifacts(run: ExperimentRun, records: list[results_mod.ArtifactRecord]) -> int:
    """把产物登记成 Artifact 行（含 sha256），供界面与报告引用。"""
    existing = {item.path for item in repo.list_all(Artifact, run.project_id) if item.run_id == run.id}
    created = 0
    for record in records:
        if record.relative_path in existing:
            continue
        repo.save(
            Artifact(
                id=repo.new_id("art"),
                project_id=run.project_id,
                run_id=run.id,
                type=record.kind,
                path=record.relative_path,
                sha256=record.sha256,
            )
        )
        created += 1
    return created


def set_inputs(run: ExperimentRun, names: list[str]) -> ExperimentRun:
    """记录本次运行实际读到的输入文件名（只记名字，不复制数据）。"""
    run.inputs = sorted(names)
    return repo.save(run)


def input_names(sac_dir: Path, date: str, stations: list[str] | tuple[str, ...]) -> list[str]:
    """本次运行会读到的 SAC 文件名清单。只列名字，不读内容、不复制。"""
    wanted = {f"{station}_{date}" for station in stations}
    root = Path(sac_dir)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.glob("*.SAC") if path.stem in wanted)


def artifacts_of(run: ExperimentRun) -> list[Artifact]:
    return [item for item in repo.list_all(Artifact, run.project_id) if item.run_id == run.id]


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def request_cancel(run: ExperimentRun, *, grace_signal: int = signal.SIGTERM) -> bool:
    """请求取消：先落标志，再向进程组发信号。返回是否真的发出了信号。"""
    run.cancel_requested = True
    repo.save(run)
    if not run.pgid:
        return False
    try:
        os.killpg(run.pgid, grace_signal)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def recover_stale_runs(project_id: str | None = None) -> list[str]:
    """把"状态是运行中、但进程已不在"的记录标成失败。

    覆盖两种现实情况：工作进程被强杀（SIGKILL 收不到收尾逻辑），
    以及机器/容器重启。返回被处理的 run_id 列表。
    """
    runs = list_runs(project_id) if project_id else repo.list_all(ExperimentRun)
    recovered: list[str] = []
    for run in runs:
        if run.status is not RunStatus.RUNNING:
            continue
        if pid_alive(run.pid):
            continue
        reason = (
            "工作进程已不在（可能被强制终止或机器重启），运行状态自动标记为失败"
        )
        finish_run(
            run,
            status=RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED,
            exit_code=run.exit_code,
            errors=[reason],
        )
        recovered.append(run.id)
    return recovered

"""把一次实验提交给独立后台进程。命令行与界面共用这一条路径。

为什么不在这里直接跑：Streamlit 的会话跟浏览器绑定，页面刷新会重跑脚本；
把执行放会话里必然丢记录，也杀不干净。所以提交方只做三件事：
建记录 → 写请求文件 → 起独立进程，然后立刻返回。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..storage import repository as repo
from ..storage.models import ExperimentRun
from . import results as results_mod
from . import runs as runs_mod
from .env import EngineEnvironment
from .pipeline import PipelineConfig

WORKER = Path(__file__).resolve().parents[3] / "scripts" / "run_experiment_worker.py"


def build_request(config: PipelineConfig, environment: EngineEnvironment) -> dict:
    """请求文件的内容：worker 只靠它就能重建一次运行。"""
    return {
        "project_id": config.project_id,
        "date": config.date,
        "stations": list(config.stations),
        "sac_dir": str(config.sac_dir),
        "allowed_sac_root": str(config.allowed_sac_root),
        "runs_root": str(config.runs_root),
        "python": str(environment.python),
        "code_dir": str(environment.code_dir),
        "period": list(config.period),
        "resample": config.resample,
        "max_lag": config.max_lag,
        "stack_days": list(config.stack_days),
        "timeouts": dict(config.timeouts),
        "qc_limit": config.qc_limit,
    }


def launch(
    config: PipelineConfig,
    environment: EngineEnvironment,
    *,
    db_path: Path | str | None = None,
    worker_python: str | None = None,
) -> ExperimentRun:
    """建记录 + 起后台进程。返回已标记为 running 的记录。"""
    runs_mod.recover_stale_runs(config.project_id)
    runs_mod.ensure_no_active_run(config.project_id)

    run = runs_mod.create_run(
        config.project_id,
        stations=list(config.stations),
        time_window=config.date,
        work_dir=config.runs_root / "pending",
        interpreter=str(environment.python),
        dependencies=environment.versions,
        script_hashes=environment.script_hashes,
        engine_version=str(environment.versions.get("obspy") or ""),
    )
    run_dir = Path(config.runs_root) / run.id
    run_dir.mkdir(parents=True, exist_ok=True)
    run.work_dir = str(run_dir)
    repo.save(run)
    runs_mod.set_inputs(
        run, runs_mod.input_names(Path(config.sac_dir), config.date, list(config.stations))
    )
    results_mod.write_json(run_dir / "request.json", build_request(config, environment))

    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    worker_cmd = [
        worker_python or sys.executable, "-B", str(WORKER),
        "--run-id", run.id,
        "--request", str(run_dir / "request.json"),
        "--db", str(db_path) if db_path else "",
    ]
    proc = subprocess.Popen(  # noqa: S603 - 命令由本模块拼装，无用户输入
        worker_cmd,
        cwd=str(WORKER.parents[1]),
        stdout=(log_dir / "worker.stdout.log").open("ab"),
        stderr=(log_dir / "worker.stderr.log").open("ab"),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
    )
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    return runs_mod.mark_running(
        run, command=f"后台工作进程 pid={proc.pid}", pid=proc.pid, pgid=pgid
    )

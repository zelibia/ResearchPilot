"""单日实验链路编排：QC → 预处理 → 互相关 → 叠加。

纪律（写在代码里，也写在 run.json 里）：

1. **单日就是单日**。叠加只允许 N=1；要更多天数，必须上游真的有那么多天
   daily CCF，由 `ensure_days_available` 在起进程前拒绝。
2. **QC 只看本次实验的数据范围**。日期与台站过滤由 QcSpec 带下去；
   跑完还核对"扫描文件数"是否与预期范围吻合，防止某天参数失效变成全量扫描。
3. **先写计划快照，再执行，最后写实际结果**。配置里的顺序不算数，
   实际执行了哪几步以预处理产出的 `*_steps.csv` 为准。
4. 任一段失败立即停止，且**失败记录照样落盘**（阶段结果、退出码、stderr 路径）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths as path_safety
from . import results as results_mod
from . import specs as specs_mod
from .env import EngineEnvironment
from .runner import ControlledRunner, StageResult


class PipelineError(RuntimeError):
    """编排层面的错误（校验不通过、前置条件不满足）。"""


STAGE_ORDER = ("qc", "preprocess", "xcorr", "stack")

# 每段跑完之后必须真的产出这些文件，否则视为失败。
# 只靠退出码不够：脚本遇到"没有输入"时可能安静地跑完并返回 0，
# 让一次空跑看起来像成功。
EXPECTED_OUTPUTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "preprocess": ("prep", ("*_prep.SAC",)),
    "xcorr": ("cc", ("*_cc.SAC",)),
    "stack": ("stack", ("*.SAC",)),
}


@dataclass
class PipelineConfig:
    """一次实验的全部输入。所有字段由调用方显式给出，界面上来的值必须先过这里。"""

    project_id: str
    run_id: str
    date: str
    stations: tuple[str, ...]
    sac_dir: Path
    allowed_sac_root: Path
    runs_root: Path
    period: tuple[float, float] = (7.0, 150.0)
    resample: float = 1.0
    norm: str = "ram"
    max_lag: float = 5000.0
    stack_days: tuple[int, ...] = (1,)
    timeouts: dict[str, float] = field(default_factory=lambda: {
        "qc": 300.0, "preprocess": 900.0, "xcorr": 600.0, "stack": 600.0,
    })
    qc_limit: int = 0


@dataclass
class PipelineOutcome:
    run_id: str
    run_dir: Path
    status: str                       # succeeded | failed | cancelled
    stages: list[StageResult] = field(default_factory=list)
    snapshot: dict[str, object] = field(default_factory=dict)
    summary: dict[str, object] = field(default_factory=dict)
    error: str = ""

    @property
    def failed_stage(self) -> StageResult | None:
        return next((item for item in self.stages if not item.ok), None)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _declared_vs_actual_note() -> dict[str, object]:
    """配置里声明的预处理顺序 vs 代码实际执行的顺序。

    `configs/default.yaml` 声明了 8 步（含谱白化），而既有代码实际跑 5 步且关闭白化。
    本轮不为了"配置好看"去改算法顺序，只把差异如实记进快照。
    """
    return {
        "declared_in_config": [
            "demean", "detrend", "taper", "remove_response",
            "resample", "bandpass", "temporal_normalize", "spectral_whitening",
        ],
        "actual_reported_by_script": [
            "detrend/taper", "remove_response", "resample", "band-pass", "temporal norm",
        ],
        "whitening": False,
        "note": "以脚本产物 *_steps.csv 为准；未确认前不改变算法执行顺序",
    }


def run_pipeline(
    config: PipelineConfig,
    environment: EngineEnvironment,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[str, StageResult], None] | None = None,
) -> PipelineOutcome:
    """跑完四段。失败不抛异常，而是把失败状态放进返回值——失败也要留记录。"""

    run_dir = path_safety.run_dir(config.runs_root, config.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    def out_dir(stage: str) -> Path:
        return path_safety.ensure_output_dir(run_dir / stage, config.runs_root)

    outcome = PipelineOutcome(
        run_id=config.run_id,
        run_dir=run_dir,
        status="failed",
        snapshot={
            "run_id": config.run_id,
            "project_id": config.project_id,
            "date": config.date,
            "stations": list(config.stations),
            "data_scope": {
                "sac_dir": str(config.sac_dir),
                "dates": [config.date, config.date],
                "allowed_root": str(config.allowed_sac_root),
            },
            "parameters": {
                "period_s": list(config.period),
                "resample_hz": config.resample,
                "norm": config.norm,
                "max_lag_s": config.max_lag,
                "stack_days": list(config.stack_days),
            },
            "timeouts_s": dict(config.timeouts),
            "environment": environment.snapshot(),
            "preprocess_order": _declared_vs_actual_note(),
            "planned_at": _now(),
            "stages": [],
            "warning": "单日实验：叠加只允许 N=1，不做 3/7 天叠加",
        },
    )
    snapshot_path = run_dir / "run.json"

    # 输入安全：存在、在允许根之下、当前进程只读
    sac_dir = path_safety.resolve_input_dir(config.sac_dir, config.allowed_sac_root)
    path_safety.ensure_disjoint(config.runs_root, sac_dir)
    path_safety.ensure_disjoint(config.runs_root, environment.code_dir)
    if sac_dir != config.sac_dir:
        outcome.snapshot["data_scope"]["sac_dir_resolved"] = str(sac_dir)  # type: ignore[index]

    # 运行前快照：原始数据与脚本目录都不许被改动（含不留 __pycache__）
    before_inputs = path_safety.snapshot_tree(sac_dir)
    before_code = path_safety.snapshot_tree(environment.code_dir)

    # 先写"计划快照"：万一中途崩了，也能知道本来打算跑什么
    results_mod.write_json(snapshot_path, outcome.snapshot)

    runner = ControlledRunner(
        python=environment.python,
        code_dir=environment.code_dir,
        log_dir=run_dir / "logs",
        default_timeout=max(config.timeouts.values()),
    )

    date = specs_mod.normalize_date(config.date)
    stations = specs_mod.check_stations(config.stations)
    run_stages: list[tuple[str, specs_mod.QcSpec | specs_mod.PreprocessSpec
                          | specs_mod.XcorrSpec | specs_mod.StackSpec]] = [
        ("qc", specs_mod.QcSpec(
            sac_dir=sac_dir, out_dir=out_dir("qc"),
            dates=(date, date), stations=stations, limit=config.qc_limit,
        )),
        ("preprocess", specs_mod.PreprocessSpec(
            sac_dir=sac_dir, date=date, stations=stations,
            period=config.period, resample=config.resample, norm=config.norm,
            out_dir=out_dir("prep"),
        )),
        ("xcorr", specs_mod.XcorrSpec(
            stations=stations, date=date, prep_dir=out_dir("prep"),
            max_lag=config.max_lag, out_dir=out_dir("cc"),
        )),
        ("stack", specs_mod.StackSpec(
            stations=stations, cc_dir=out_dir("cc"),
            days=config.stack_days, out_dir=out_dir("stack"),
        )),
    ]

    recorded: list[dict[str, object]] = []
    for stage, spec in run_stages:
        argv = spec.argv(environment.code_dir)
        stage_result = runner.run(
            argv,
            stage=stage,
            timeout=config.timeouts.get(stage),
            cancel_event=cancel_event,
        )
        outcome.stages.append(stage_result)
        recorded.append({
            "stage": stage,
            "status": stage_result.status,
            "returncode": stage_result.returncode,
            "duration_s": stage_result.duration_s,
            "command": stage_result.command,
            "stdout": stage_result.stdout_path,
            "stderr": stage_result.stderr_path,
            "error": stage_result.error,
        })
        if progress is not None:
            progress(stage, stage_result)

        if stage == "qc" and stage_result.ok:
            _verify_qc_scope(out_dir("qc"), stations, config.qc_limit, outcome)

        if stage_result.ok:
            missing = _missing_outputs(stage, run_dir)
            if missing:
                stage_result.status = "failed"
                stage_result.error = missing
                recorded[-1]["status"] = "failed"
                recorded[-1]["error"] = missing

        if not stage_result.ok:
            outcome.status = "cancelled" if stage_result.status == "cancelled" else "failed"
            outcome.error = f"{stage} 阶段 {stage_result.status}：{stage_result.error}"
            break

    if not outcome.error and all(item.ok for item in outcome.stages):
        outcome.status = "succeeded"

    # 运行后核对：输入树与脚本目录零改动
    input_changes = path_safety.diff_snapshots(before_inputs, path_safety.snapshot_tree(sac_dir))
    code_changes = path_safety.diff_snapshots(before_code, path_safety.snapshot_tree(environment.code_dir))
    outcome.snapshot["input_integrity"] = {
        "sac_dir": str(sac_dir),
        "sac_dir_changes": input_changes,
        "code_dir": str(environment.code_dir),
        "code_dir_changes": code_changes,
    }
    if (input_changes or code_changes) and outcome.status == "succeeded":
        outcome.status = "failed"
        outcome.error = (
            "运行后检测到只读区域被改动："
            f"原始数据 {input_changes[:3]}；脚本目录 {code_changes[:3]}"
        )

    outcome.snapshot["stages"] = recorded
    outcome.snapshot["status"] = outcome.status
    outcome.snapshot["error"] = outcome.error
    outcome.snapshot["finished_at"] = _now()
    outcome.summary = results_mod.summarize_run(run_dir)
    _attach_zero_lag_index(outcome, run_dir)
    outcome.snapshot["summary"] = outcome.summary
    results_mod.write_json(snapshot_path, outcome.snapshot)
    return outcome


def _attach_zero_lag_index(outcome: PipelineOutcome, run_dir: Path) -> None:
    """把互相关零 lag 的数组下标补进结果（CSV 里没有，只在标准输出里）。"""
    pairs = (outcome.summary.get("xcorr") or {}).get("pairs") or []
    if not pairs:
        return
    index = results_mod.zero_lag_index_from_log(run_dir / "logs" / "xcorr.stdout.log")
    if index is None:
        return
    for row in pairs:
        row["zero_lag_index"] = index


def _missing_outputs(stage: str, run_dir: Path) -> str:
    """退出码为 0 但没产出文件时，返回一句明确的失败原因。"""
    expectation = EXPECTED_OUTPUTS.get(stage)
    if not expectation:
        return ""
    subdir, patterns = expectation
    target = run_dir / subdir
    found = [path for pattern in patterns for path in target.glob(pattern)]
    if found:
        return ""
    return (
        f"{stage} 退出码为 0，但 {subdir}/ 下没有产出 {patterns} —— "
        "通常意味着上游没有可用输入（例如该日期没有数据）"
    )


def _verify_qc_scope(
    qc_dir: Path, stations: tuple[str, ...], limit: int, outcome: PipelineOutcome
) -> None:
    """核对 QC 真的只扫了本次范围，而不是把整个数据目录扫了。"""
    parsed = results_mod.parse_qc(qc_dir)
    scanned = int(parsed.get("files_scanned") or 0)
    if scanned == 0:
        outcome.snapshot["qc_scope_check"] = "警告：QC 没有扫描到任何文件"
        return
    expected = len(stations) if not limit else min(limit, len(stations))
    if scanned > expected:
        outcome.snapshot["qc_scope_check"] = (
            f"警告：QC 扫描了 {scanned} 个文件，超过本次范围的 {expected} 个"
        )
    else:
        outcome.snapshot["qc_scope_check"] = f"QC 扫描 {scanned} 个文件，与本次范围一致"

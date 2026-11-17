"""实验记录的后端渲染模块（M2-02C）。

网站主导航已移除「实验工作台」入口；该模块仅保留给内部回归与已有实验记录，
不再作为用户可访问的工作台。

只提供**已经真实跑通**的操作：提交单日 QC → 预处理 → 互相关 → 叠加，
查看状态、步骤、结果图与日志，取消进行中的运行。

三条纪律：

1. 界面上的数字与图全部来自真实运行记录与产物文件，不放任何示意数据；
2. 没有记录时只显示空状态与提交入口；
3. 开发者信息（引擎路径、脚本校验值等）只在开发者诊断模式里看。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import streamlit as st

from ..config import ALLOWED_SAC_ROOT, ENGINE_CODE_DIR, ENGINE_PYTHON, RUNS_DIR, STAGE_TIMEOUTS
from ..engine import env as env_mod
from ..engine import launch as launch_mod
from ..engine import pipeline as pipeline_mod
from ..engine import runs as runs_mod
from ..engine.env import EngineEnvironment, EngineEnvironmentError
from ..storage import repository as repo
from ..storage.models import Artifact, ExperimentRun, Project, RunStatus
from . import developer, split_layout, theme

INDEX_HEIGHT = 560

DEFAULT_DATE = "2022-11-07"
STATION_PAIRS: dict[str, tuple[str, str]] = {
    "BJT × SSE（默认）": ("IC_BJT_BHZ", "IC_SSE_BHZ"),
}

_STATUS_MARK = {
    RunStatus.PENDING: "○",
    RunStatus.RUNNING: "⏳",
    RunStatus.SUCCEEDED: "✅",
    RunStatus.FAILED: "❌",
    RunStatus.CANCELLED: "⛔",
    RunStatus.SKIPPED: "－",
}

_RUN_STATUS_LABEL = {
    RunStatus.PENDING: "已排队",
    RunStatus.RUNNING: "运行中",
    RunStatus.SUCCEEDED: "执行成功",
    RunStatus.FAILED: "执行失败",
    RunStatus.CANCELLED: "已取消",
    RunStatus.SKIPPED: "未执行",
}

# 引擎真实覆盖的四段（与 engine/pipeline.STAGE_ORDER 一致）
_STAGE_ORDER = ("qc", "preprocess", "xcorr", "stack")
_STAGE_LABEL = {"qc": "质控", "preprocess": "预处理", "xcorr": "互相关", "stack": "叠加"}


def _station_names(run: ExperimentRun) -> list[str]:
    """台站显示成短名（IC_BJT_BHZ → BJT），与目录里的写法一致。"""
    return [item.split("_")[1] if item.count("_") >= 2 else item for item in (run.stations or [])]


def _stage_states(snapshot: dict) -> list[str]:
    """阶段进度条：只按运行记录里的阶段结果着色，没有记录就是未跑（不猜）。"""
    by_stage = {
        str(item.get("stage")): str(item.get("status"))
        for item in (snapshot.get("stages") or [])
        if isinstance(item, dict)
    }
    states = []
    for stage in _STAGE_ORDER:
        status = by_stage.get(stage, "")
        if status in {"ok", "succeeded"}:
            states.append("done")
        elif status in {"running", "pending"}:
            states.append("run")
        else:
            states.append("")
    return states

log = logging.getLogger(__name__)


def _run_dir(run: ExperimentRun) -> Path:
    return Path(run.work_dir) if run.work_dir else Path(RUNS_DIR) / run.id


def _read_snapshot(run: ExperimentRun) -> dict:
    path = _run_dir(run) / "run.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _index_pane(project: Project, runs: list[ExperimentRun]) -> None:
    if not runs:
        theme.empty_state("暂无实验记录")
        return
    for run in runs:
        selected = st.session_state.get(f"exp_run_{project.id}") == run.id
        label = f"{_STATUS_MARK.get(run.status, '?')} {run.time_window or '未注明日期'}　{run.id[-6:]}"
        if st.button(label, key=f"run_pick_{run.id}", type="tertiary",
                     width="stretch"):
            st.session_state[f"exp_run_{project.id}"] = run.id
            st.rerun()
        bits = []
        if run.duration_s:
            bits.append(f"{run.duration_s:.1f}s")
        if bits:
            st.caption("　".join(bits))


def _submit_block(project: Project) -> None:
    active = runs_mod.active_runs(project.id)
    with st.expander("新建实验（单日链路）", expanded=not runs_mod.list_runs(project.id)):
        if active:
            st.info(f"已有进行中的实验 {active[0].id}，等它结束或先取消。")
            return
        pair_label = st.selectbox("台站对", list(STATION_PAIRS), key=f"exp_pair_{project.id}")
        date = st.text_input("日期", value=DEFAULT_DATE, key=f"exp_date_{project.id}",
                             help="形如 2022-11-07；只处理这一天的数据")
        if st.button("开始实验", type="primary", key=f"exp_start_{project.id}"):
            _start(project, STATION_PAIRS[pair_label], date)


def _start(project: Project, stations: tuple[str, str], date: str) -> None:
    try:
        environment = EngineEnvironment.probe(ENGINE_PYTHON, ENGINE_CODE_DIR)
    except EngineEnvironmentError:
        # 异常里带着绝对路径，界面不展示它；用自检给的"面向排错"的说明
        _ok, detail = _engine_health()
        st.error(f"计算环境不可用：{detail}")
        return

    config = pipeline_mod.PipelineConfig(
        project_id=project.id,
        run_id="pending",              # 真正的 run_id 由 launch 生成
        date=date,
        stations=stations,
        sac_dir=Path(ALLOWED_SAC_ROOT),
        allowed_sac_root=Path(ALLOWED_SAC_ROOT),
        runs_root=Path(RUNS_DIR),
        stack_days=(1,),
        timeouts=dict(STAGE_TIMEOUTS),
    )
    try:
        run = launch_mod.launch(config, environment)
    except Exception as exc:  # noqa: BLE001 - 提交失败要显示真实原因
        st.error(f"提交失败：{exc}")
        return
    st.session_state[f"exp_run_{project.id}"] = run.id
    st.success(f"已提交：{run.id}（后台执行，页面刷新不影响）")
    st.rerun()


def _detail_pane(project: Project, runs: list[ExperimentRun]) -> None:
    current = st.session_state.get(f"exp_run_{project.id}")
    run = next((item for item in runs if item.id == current), None)
    if run is None:
        if runs:
            theme.empty_state("从左侧选择一次运行")
            return
        # 空状态只在左栏说一次；右栏给"下一步做什么"，不再重复同一句话
        theme.empty_state("提交第一次实验")
        ok, detail = _engine_health()
        if not ok:
            st.warning(f"当前环境不能提交实验：{detail}")
        return

    snapshot = _read_snapshot(run)
    artifacts = runs_mod.artifacts_of(run)

    # 界面上不出现运行内部编号；用时间窗 + 状态徽标 + 阶段条表达
    kind = {"succeeded": "ok", "failed": "danger", "running": "ai",
            "cancelled": "warn"}.get(run.status.value, "")
    theme.markdown(
        f'<div style="font-size:var(--rp-text-lg);font-weight:650">'
        f'{theme.esc(run.time_window or "实验")}</div>'
    )
    theme.markdown(theme.chip_row([
        (_STATUS_MARK.get(run.status, "?") + " " + _RUN_STATUS_LABEL.get(run.status, run.status.value), kind),
        ("耗时 " + (f"{run.duration_s:.1f} 秒" if run.duration_s else "未完成"), ""),
        ("台站 " + "、".join(_station_names(run) or ["未记录"]), ""),
    ]))
    theme.markdown(theme.stage_bar(_stage_states(snapshot)))
    theme.markdown(theme.chip_row([
        (f"{_STAGE_LABEL[stage]}{'已跑' if state == 'done' else ('运行中' if state == 'run' else '未跑')}", 
         "ok" if state == "done" else ("ai" if state == "run" else ""))
        for stage, state in zip(_STAGE_ORDER, _stage_states(snapshot))
    ]))
    head = st.columns([3, 1])
    if run.status is RunStatus.RUNNING:
        if head[1].button("取消", key=f"exp_cancel_{run.id}"):
            signalled = runs_mod.request_cancel(runs_mod.get_run(run.id))
            st.warning("已请求取消" if signalled else "进程已不在，稍后会标记为失败")
            st.rerun()

    if run.errors:
        st.error("；".join(run.errors))

    tab_result, tab_steps, tab_log, tab_raw = st.tabs(["结果", "步骤", "日志", "原始记录"])

    with tab_result:
        _result_tab(run, snapshot, artifacts)
    with tab_steps:
        _steps_tab(snapshot)
    with tab_log:
        _log_tab(_run_dir(run), artifacts)
    with tab_raw:
        _raw_tab(run, snapshot)


def _result_tab(run: ExperimentRun, snapshot: dict, artifacts: list[Artifact]) -> None:
    summary = run.summary or {}
    pairs = (summary.get("xcorr") or {}).get("pairs") or []
    for row in pairs:
        cols = st.columns(4)
        cols[0].metric("台间距", f"{row.get('distance_km')} km")
        cols[1].metric("全局峰 lag", f"{row.get('global_peak_lag_s')} s")
        cols[2].metric("视速度", f"{row.get('global_peak_velocity_kmps')} km/s")
        cols[3].metric("面波 SNR", f"{row.get('surface_window_snr')}")

    stacks = (summary.get("stack") or {}).get("stacks") or []
    for row in stacks:
        st.caption(
            f"叠加 N={row.get('days')}　实际 {row.get('actual_days')} 天"
            f"　面波/全局 {row.get('surface_over_global')}"
            f"　日期范围 {row.get('date_range')}"
        )

    figures = [item for item in artifacts if item.type == "figure"]
    if not figures:
        st.caption("这次运行没有生成图。")
    root = _run_dir(run)
    for item in figures:
        path = root / item.path
        if path.is_file():
            st.image(str(path), caption=item.path)


def _steps_tab(snapshot: dict) -> None:
    stages = snapshot.get("stages") or []
    if stages:
        st.dataframe(
            [
                {
                    "阶段": item.get("stage"),
                    "状态": item.get("status"),
                    "耗时(s)": item.get("duration_s"),
                    "退出码": item.get("returncode"),
                }
                for item in stages
            ],
            width="stretch", hide_index=True,
        )
    steps = ((snapshot.get("summary") or {}).get("preprocess") or {}).get("actual_steps") or []
    if steps:
        st.caption("预处理实际执行步骤（按产物记录）：" + " → ".join(steps))
    scope = snapshot.get("qc_scope_check")
    if scope:
        st.caption(f"数据范围核对：{scope}")


def _log_tab(run_dir: Path, artifacts: list[Artifact]) -> None:
    logs = [item for item in artifacts if item.type == "log"]
    if not logs:
        st.caption("没有日志文件。")
        return
    names = [item.path for item in logs]
    chosen = st.selectbox("选择日志", names, key=f"exp_log_{run_dir.name}")
    path = run_dir / chosen
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        st.code(text[-4000:] or "（空）", language="text")


def _raw_tab(run: ExperimentRun, snapshot: dict) -> None:
    st.markdown(
        f"- **输入文件**：{'、'.join(run.inputs) or '—'}\n"
        f"- **产出条目**：{run.summary.get('artifact_count', 0) if run.summary else 0} 个\n"
        f"- **开始 / 结束**：{run.started_at or '—'} → {run.ended_at or '—'}"
    )
    with st.expander("参数快照", expanded=False):
        st.json(snapshot.get("parameters") or {})
    with st.expander("产物清单（含校验值）", expanded=False):
        st.dataframe(
            [
                {"文件": item["path"], "类型": item["kind"], "大小": item["size"],
                 "sha256": item["sha256"][:16] + "…"}
                for item in (run.summary or {}).get("artifacts", [])
            ],
            width="stretch", hide_index=True,
        )

    if developer.enabled():
        st.divider()
        st.caption("开发者信息（仅诊断模式可见）")
        st.write("解释器：", run.interpreter)
        st.write("依赖：", run.dependencies)
        st.write("脚本 sha256：", run.script_hashes)
        st.write("工作目录：", run.work_dir)
        st.write("命令：", run.command)
        st.write("引擎脚本目录：", str(ENGINE_CODE_DIR))


def render(project: Project) -> None:
    runs_mod.recover_stale_runs(project.id)
    runs = runs_mod.list_runs(project.id)
    done = sum(1 for run in runs if run.status is RunStatus.SUCCEEDED)
    failed = sum(1 for run in runs if run.status in {RunStatus.FAILED, RunStatus.CANCELLED})
    theme.status_strip([
        (f"运行总数 {len(runs)}", ""),
        (f"成功 {done}", "ok" if done else ""),
        (f"失败 / 取消 {failed}", "danger" if failed else ""),
    ])

    # 引擎自检：先说清这台机器能不能算，避免用户点完才发现跑不起来（P0-2）
    ok, detail = _engine_health()
    # 状态只说一次：徽标给结论，版本与脚本数进"环境详情"，不再两行长句重复一遍。
    if not ok:
        cols = st.columns([4, 1, 1])
        with cols[0]:
            theme.markdown(theme.chip_row([("计算环境不可用", "danger")]))
        with cols[1].popover("环境详情", width="stretch"):
            st.caption(detail)
        if cols[2].button("重新检测", key=f"engine_recheck_{project.id}"):
            _engine_health.clear()
            st.rerun()
    else:
        theme.markdown(theme.hidden_compat("计算环境可用 · 原始数据只读"))

    split_layout.render(
        workbench="experiment",
        index_title="",
        index_render=lambda: _index_pane(project, runs),
        detail_render=lambda: _detail_pane(project, runs),
        index_height=INDEX_HEIGHT,
        index_side="right",
        index_controls_label="索引",
        collapsed_rail_render=lambda: split_layout.collapsed_rail_button(
            "experiment", project.id
        ),
    )

    st.divider()
    _submit_block(project)


@st.cache_data(ttl=300, show_spinner=False)
def _engine_health() -> tuple[bool, str]:
    """计算环境自检结果缓存 5 分钟：探测要起子进程，不宜每次重跑都做。"""
    return env_mod.engine_health(ENGINE_PYTHON, ENGINE_CODE_DIR)

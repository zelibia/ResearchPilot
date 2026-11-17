"""运行结果解析：把四段产物变成结构化指标与产物清单。

两个用途：

1. 写进 run.json 的"实际结果"，供界面展示与事后核对；
2. **核对实际执行了什么**。预处理每一步都会写进 `*_steps.csv`，
   所以"实际执行的步骤与参数"是从产物里读出来的，而不是照抄配置里的计划顺序。
   这两者一旦不一致，以产物为准。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .env import file_sha256


# 产物里天然不确定的列：耗时随机器负载变化，不参与一致性比对
VOLATILE_COLUMNS = {"耗时(s)"}

# xcorr 把零 lag 所在的下标打在标准输出里（CSV 只存了该点的 lag 值）
ZERO_LAG_INDEX_RE = re.compile(r"零 lag：索引\s*(\d+)")


def zero_lag_index_from_log(path: Path) -> int | None:
    """从互相关标准输出里取零 lag 的数组下标。取不到返回 None。"""
    if not Path(path).is_file():
        return None
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    match = ZERO_LAG_INDEX_RE.search(text)
    return int(match.group(1)) if match else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    kind: str
    size: int
    sha256: str


KIND_BY_SUFFIX = {
    ".png": "figure",
    ".jpg": "figure",
    ".svg": "figure",
    ".pdf": "report",
    ".md": "report",
    ".csv": "data",
    ".sac": "data",
    ".log": "log",
    ".json": "report",
}


def collect_artifacts(root: Path, *, skip: tuple[str, ...] = ()) -> list[ArtifactRecord]:
    """列出输出目录下的全部产物（含大小与 sha256）。"""
    records: list[ArtifactRecord] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in skip):
            continue
        records.append(
            ArtifactRecord(
                relative_path=relative,
                kind=KIND_BY_SUFFIX.get(path.suffix.lower(), "other"),
                size=path.stat().st_size,
                sha256=file_sha256(path),
            )
        )
    return records


def _to_float(value: object) -> float | str:
    raw = str(value if value is not None else "").strip()
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_steps(path: Path) -> dict[str, object]:
    """解析 `*_steps.csv`：实际执行了哪几步、最终数值是多少。"""
    rows = _read_csv(path)
    if not rows:
        return {}
    steps = [str(row.get("步骤", "")).strip() for row in rows if row.get("步骤")]
    last = rows[-1]
    return {
        "steps": steps,
        "step_count": len(steps),
        "final": {
            "npts": last.get("npts"),
            "sampling_rate_hz": last.get("采样率(Hz)"),
            "rms": last.get("RMS"),
        },
    }


def parse_qc(qc_dir: Path) -> dict[str, object]:
    """解析 QC 产物：扫了多少、多少合格、站对结论。"""
    raw = _read_csv(qc_dir / "qc_raw.csv")
    pairs = _read_csv(qc_dir / "qc_pairs.csv")

    def passed(row: dict[str, str]) -> bool:
        return str(row.get("是否合格", "")).strip().lower() == "true"

    return {
        "files_scanned": len(raw),
        "files_passed": sum(1 for row in raw if passed(row)),
        "failed_reasons": sorted(
            {
                str(row.get("原因", "")).strip()
                for row in raw
                if not passed(row) and str(row.get("原因", "")).strip()
            }
        ),
        "pairs": [
            {
                "pair": row.get("台站对"),
                "date": row.get("日期"),
                "qualified": passed(row),
                "reason": row.get("原因", ""),
            }
            for row in pairs
        ],
    }


def parse_xcorr(cc_dir: Path) -> dict[str, object]:
    rows = _read_csv(cc_dir / "xcorr_log.csv")
    return {
        "pairs": [
            {
                "pair": row.get("台站对"),
                "date": row.get("日期"),
                "conclusion": row.get("结论"),
                "common_coverage": row.get("共同覆盖率"),
                "lag_range": row.get("lag范围"),
                "zero_lag_value": _to_float(row.get("零lag值")),
                # 注意：CSV 这一列存的是"零 lag 处的 lag 值"（应为 0），不是数组下标；
                # 下标由 stdout 提供，见 zero_lag_index_from_log
                "zero_lag_axis_s": _to_float(row.get("lags[零lag索引]")),
                "distance_km": _to_float(row.get("台间距(km)")),
                "global_peak_lag_s": _to_float(row.get("global_peak_lag")),
                "global_peak_velocity_kmps": _to_float(row.get("global_peak_velocity_kmps")),
                "surface_window": row.get("surface_window_s"),
                "surface_window_snr": _to_float(row.get("surface_window_snr")),
                "zero_over_surface_peak": _to_float(row.get("zero_over_surface_peak")),
                "nan_inf": row.get("NaN/Inf"),
            }
            for row in rows
        ]
    }


def parse_stack(stack_dir: Path) -> dict[str, object]:
    rows = _read_csv(stack_dir / "stack_log.csv")
    return {
        "stacks": [
            {
                "days": _to_float(row.get("N天")),
                "date_range": row.get("日期范围"),
                "actual_days": _to_float(row.get("天数")),
                "global_peak_lag_s": _to_float(row.get("global_peak_lag")),
                "global_peak_velocity_kmps": _to_float(row.get("global_peak_velocity_kmps")),
                "surface_window": row.get("surface_window_s"),
                "surface_window_snr": _to_float(row.get("surface_window_snr")),
                "zero_over_surface_peak": _to_float(row.get("zero_over_surface_peak")),
                "surface_over_global": _to_float(row.get("surface_over_global")),
            }
            for row in rows
        ]
    }


def summarize_run(run_dir: Path) -> dict[str, object]:
    """汇总一次运行目录：四段指标 + 实际步骤 + 产物清单。"""
    run_dir = Path(run_dir)
    summary: dict[str, object] = {}

    if (run_dir / "qc").is_dir():
        summary["qc"] = parse_qc(run_dir / "qc")

    prep = run_dir / "prep"
    if prep.is_dir():
        per_station: dict[str, object] = {}
        actual_steps: list[str] = []
        for csv_path in sorted(prep.glob("*_steps.csv")):
            station = csv_path.name.split("_steps.csv")[0]
            parsed = parse_steps(csv_path)
            per_station[station] = parsed
            if parsed.get("steps") and not actual_steps:
                actual_steps = list(parsed["steps"])
        summary["preprocess"] = {
            "stations": per_station,
            "actual_steps": actual_steps,
            "actual_step_count": len(actual_steps),
        }

    if (run_dir / "cc").is_dir():
        summary["xcorr"] = parse_xcorr(run_dir / "cc")

    if (run_dir / "stack").is_dir():
        summary["stack"] = parse_stack(run_dir / "stack")

    artifacts = collect_artifacts(run_dir, skip=("run.json",))
    summary["artifacts"] = [
        {
            "path": item.relative_path,
            "kind": item.kind,
            "size": item.size,
            "sha256": item.sha256,
        }
        for item in artifacts
    ]
    summary["artifact_count"] = len(artifacts)
    return summary


def compare_steps_csv(left: Path, right: Path, *, ignore: set[str] | None = None) -> list[str]:
    """逐列比较两份 `*_steps.csv`，忽略耗时这类非确定列。返回差异描述列表。"""
    ignored = set(VOLATILE_COLUMNS) | set(ignore or ())
    left_rows = _read_csv(left)
    right_rows = _read_csv(right)
    if len(left_rows) != len(right_rows):
        return [f"行数不同：{len(left_rows)} vs {len(right_rows)}"]

    differences: list[str] = []
    for index, (a, b) in enumerate(zip(left_rows, right_rows)):
        for key in sorted(set(a) | set(b)):
            if key in ignored:
                continue
            if str(a.get(key, "")).strip() != str(b.get(key, "")).strip():
                differences.append(f"第 {index} 行 {key}：{a.get(key)!r} vs {b.get(key)!r}")
    return differences


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

"""四段计算的参数模型：argv 的唯一真源。

所有命令都由这里的 dataclass 拼出来，字段是强类型的（路径、日期、浮点、枚举）。
调用方拿不到"往 argv 里塞任意字符串"的口子——命令注入在类型层就消失了。

对应脚本的既有 CLI（参数名与原脚本逐字一致，本模块不引入新语义）：

    check.py       --sac-dir --dates START END --stations ... --limit N --outdir
    preprocess.py  --sac-dir --date --stations ... --period TMIN TMAX --resample
                   --norm --norm-window --outdir
    xcorr.py       --stations ... --date --prep-dir --max-lag --outdir
    stack.py       --stations ... --cc-dir --n ... --outdir
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .env import SCRIPT_FILES


class SpecError(ValueError):
    """参数不合法。"""


# 台站前缀形如 IC_BJT_BHZ；日期形如 2022-11-07 或 20221107
STATION_RE = re.compile(r"^[A-Z]{2}_[A-Z0-9]{3,5}_[A-Z0-9]{3}$")
DATE_RE = re.compile(r"^\d{4}-?\d{2}-?\d{2}$")

NORM_CHOICES = ("ram", "1bit", "none")


def normalize_date(raw: str) -> str:
    value = (raw or "").strip()
    if not DATE_RE.match(value):
        raise SpecError(f"日期格式应为 YYYY-MM-DD 或 YYYYMMDD：{raw!r}")
    return value.replace("-", "")


def check_stations(stations: list[str] | tuple[str, ...], *, minimum: int = 1) -> tuple[str, ...]:
    items = tuple(stations or ())
    if len(items) < minimum:
        raise SpecError(f"至少需要 {minimum} 个台站")
    for item in items:
        if not STATION_RE.match(item):
            raise SpecError(f"台站标识不合法（形如 IC_BJT_BHZ）：{item!r}")
    return items


@dataclass(frozen=True)
class QcSpec:
    """QC：只扫本次实验用到的日期与台站，不遍历整个数据目录。"""

    stage: str = field(default="check", init=False)
    sac_dir: Path = Path(".")
    out_dir: Path = Path(".")
    dates: tuple[str, str] | None = None      # 闭区间，形如 ("20221107", "20221107")
    stations: tuple[str, ...] = ()
    limit: int = 0

    def argv(self, code_dir: Path) -> list[str]:
        args = ["--sac-dir", str(self.sac_dir)]
        if self.dates:
            args += ["--dates", self.dates[0], self.dates[1]]
        if self.stations:
            args += ["--stations", *self.stations]
        if self.limit:
            args += ["--limit", str(int(self.limit))]
        args += ["--outdir", str(self.out_dir)]
        return [str(code_dir / SCRIPT_FILES["check"]), *args]


@dataclass(frozen=True)
class PreprocessSpec:
    stage: str = field(default="preprocess", init=False)
    sac_dir: Path = Path(".")
    date: str = "20221107"
    stations: tuple[str, ...] = ()
    period: tuple[float, float] = (7.0, 150.0)
    resample: float = 1.0
    norm: str = "ram"
    norm_window: float = 0.0
    out_dir: Path = Path(".")

    def argv(self, code_dir: Path) -> list[str]:
        if self.norm not in NORM_CHOICES:
            raise SpecError(f"归一化方式只能是 {NORM_CHOICES}：{self.norm!r}")
        if self.period[0] <= 0 or self.period[1] <= self.period[0]:
            raise SpecError(f"周期范围不合法：{self.period}")
        if self.resample <= 0:
            raise SpecError(f"重采样率必须为正：{self.resample}")
        args = [
            "--sac-dir", str(self.sac_dir),
            "--date", self.date,
            "--stations", *self.stations,
            "--period", str(self.period[0]), str(self.period[1]),
            "--resample", str(self.resample),
            "--norm", self.norm,
        ]
        if self.norm_window:
            args += ["--norm-window", str(self.norm_window)]
        args += ["--outdir", str(self.out_dir)]
        return [str(code_dir / SCRIPT_FILES["preprocess"]), *args]


@dataclass(frozen=True)
class XcorrSpec:
    stage: str = field(default="xcorr", init=False)
    stations: tuple[str, ...] = ()
    date: str = "20221107"
    prep_dir: Path = Path(".")
    max_lag: float = 5000.0
    out_dir: Path = Path(".")

    def argv(self, code_dir: Path) -> list[str]:
        if len(self.stations) != 2:
            raise SpecError(f"互相关需要恰好两个台站：{self.stations}")
        if self.max_lag <= 0:
            raise SpecError(f"max_lag 必须为正：{self.max_lag}")
        args = [
            "--stations", *self.stations,
            "--date", self.date,
            "--prep-dir", str(self.prep_dir),
            "--max-lag", str(self.max_lag),
            "--outdir", str(self.out_dir),
        ]
        return [str(code_dir / SCRIPT_FILES["xcorr"]), *args]


@dataclass(frozen=True)
class StackSpec:
    """叠加。

    `days` 是"叠加到第 N 天"的列表。单日实验只允许 N=1：用一天数据算 3/7 天叠加
    是假结果。真正开放多日叠加的前提是上游确实有那么多天的 daily CCF，
    由 `ensure_days_available` 在运行前校验。
    """

    stage: str = field(default="stack", init=False)
    stations: tuple[str, ...] = ()
    cc_dir: Path = Path(".")
    days: tuple[int, ...] = (1,)
    out_dir: Path = Path(".")

    def argv(self, code_dir: Path) -> list[str]:
        if len(self.stations) != 2:
            raise SpecError(f"叠加需要恰好两个台站：{self.stations}")
        if not self.days:
            raise SpecError("至少要指定一个叠加天数")
        if any(day < 1 for day in self.days):
            raise SpecError(f"叠加天数必须为正：{self.days}")
        args = [
            "--stations", *self.stations,
            "--cc-dir", str(self.cc_dir),
            "--n", *[str(int(day)) for day in self.days],
            "--outdir", str(self.out_dir),
        ]
        return [str(code_dir / SCRIPT_FILES["stack"]), *args]


def ensure_days_available(cc_dir: Path, days: tuple[int, ...]) -> int:
    """校验叠加请求没有超出实际拥有的天数，返回可用天数。

    直接读 cc 目录里 `*_cc.SAC` 的数量。少一天就拒绝，绝不"凑数"。
    """
    available = len(sorted(cc_dir.glob("*_cc.SAC"))) if cc_dir.is_dir() else 0
    if available == 0:
        raise SpecError(f"互相关目录里没有 daily CCF：{cc_dir}")
    need = max(days)
    if need > available:
        raise SpecError(
            f"请求叠加到 {need} 天，但实际只有 {available} 天 daily CCF；"
            "多日叠加要等真实调度多天数据后才开放"
        )
    return available

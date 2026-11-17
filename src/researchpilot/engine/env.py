"""计算环境：固定解释器、依赖版本快照、脚本校验值。

设计要点：

- **计算跑在独立解释器里**。界面与数据库在 `rp`（Py3.11），地震学脚本需要
  obspy / scipy / matplotlib，这些装在一个已验证过的 base 环境里；两者不混。
- 解释器路径是配置项，启动时校验存在且可执行，绝不从界面传入。
- 每次运行都把解释器路径、依赖版本、四个脚本的 sha256 记进快照，
  这样"这份结果是用什么跑出来的"事后可查。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import IS_WINDOWS


class EngineEnvironmentError(RuntimeError):
    """计算环境不可用。消息直接面向排错，不含用户输入。"""


def _posix_like(raw: str) -> bool:
    """像一个 POSIX 绝对路径（而不是 Windows 盘符路径）。"""
    text = str(raw or "").strip()
    return text.startswith("/") and not text.startswith("//")


def wsl_bridge_status() -> tuple[bool, str]:
    """Windows 侧能不能通过 ``wsl.exe`` 调用 WSL 里的计算引擎。

    只做"能不能拿到 wsl.exe"这一层判断：**不在探测阶段真的起一次互操作**，
    也不猜发行版。返回 (可用, 说明)；说明里不含绝对路径。
    """
    if not IS_WINDOWS:
        return False, "当前界面已经跑在 Linux/WSL 里，计算引擎可直接使用，不需要桥接"
    import shutil

    found = shutil.which("wsl.exe") or shutil.which("wsl")
    if found:
        return True, (
            "检测到 wsl.exe：可以把计算交给 WSL 里的引擎，但需要把数据与产物路径"
            "转换成 WSL 路径，属于尚未启用的桥接方式"
        )
    return False, "这台机器上找不到 wsl.exe，界面无法调用 WSL 里的计算引擎"


# 探测依赖版本的固定小程序。这是我们自己写死的常量，不是用户输入；
# 走 -I（isolated）避免被外部 PYTHONPATH / sitecustomize 影响。
_VERSION_PROBE = """
import importlib.metadata as md
import json
import sys

names = ["obspy", "numpy", "scipy", "matplotlib"]
out = {"python": sys.version.split()[0]}
for name in names:
    try:
        out[name] = md.version(name)
    except Exception:
        out[name] = None
print(json.dumps(out, ensure_ascii=False))
"""


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def resolve_python(raw: str | os.PathLike[str]) -> Path:
    """把配置里的解释器路径解析成可执行文件。"""
    path = Path(raw).expanduser()
    if not path.exists():
        raise EngineEnvironmentError(f"计算解释器不存在：{path}")
    if path.is_dir():
        raise EngineEnvironmentError(f"计算解释器是目录，不是可执行文件：{path}")
    if not os.access(path, os.X_OK):
        raise EngineEnvironmentError(f"计算解释器不可执行：{path}")
    return path


def resolve_code_dir(raw: str | os.PathLike[str]) -> Path:
    """定位既有脚本目录，并确认四个白名单脚本都在。"""
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise EngineEnvironmentError(f"脚本目录不存在：{path}")
    missing = [name for name in SCRIPT_FILES.values() if not (path / name).is_file()]
    if missing:
        raise EngineEnvironmentError(f"脚本目录缺少文件：{missing}（目录 {path}）")
    return path


# 段名 → 文件名。白名单只认这四份，别的一律拒绝执行。
SCRIPT_FILES = {
    "check": "check.py",
    "preprocess": "preprocess.py",
    "xcorr": "xcorr.py",
    "stack": "stack.py",
}


def probe_versions(python: Path, timeout: float = 120.0) -> dict[str, str | None]:
    """读取计算解释器的依赖版本。失败时抛错，不静默降级。"""
    try:
        done = subprocess.run(
            [str(python), "-I", "-c", _VERSION_PROBE],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineEnvironmentError(f"依赖版本探测超时（{timeout}s）：{python}") from exc
    if done.returncode != 0:
        raise EngineEnvironmentError(
            f"依赖版本探测失败（退出码 {done.returncode}）：{done.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise EngineEnvironmentError(f"依赖版本探测输出无法解析：{done.stdout[:200]}") from exc
    return {str(key): (None if value is None else str(value)) for key, value in payload.items()}


def script_hashes(code_dir: Path, stages: Iterable[str] = tuple(SCRIPT_FILES)) -> dict[str, str]:
    """四个脚本的 sha256。用来回答"结果是由哪一版代码跑出来的"。"""
    return {
        stage: file_sha256(code_dir / SCRIPT_FILES[stage])
        for stage in stages
    }


@dataclass(frozen=True)
class EngineEnvironment:
    """一次运行用到的计算环境快照。"""

    python: Path
    code_dir: Path
    versions: dict[str, str | None] = field(default_factory=dict)
    script_hashes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def probe(cls, python: str | os.PathLike[str], code_dir: str | os.PathLike[str]) -> "EngineEnvironment":
        resolved_python = resolve_python(python)
        resolved_code = resolve_code_dir(code_dir)
        return cls(
            python=resolved_python,
            code_dir=resolved_code,
            versions=probe_versions(resolved_python),
            script_hashes=script_hashes(resolved_code),
        )

    def snapshot(self) -> dict[str, object]:
        """写进 run.json 的形式：全部是字符串/数字，可直接 JSON 序列化。"""
        return {
            "python": str(self.python),
            "code_dir": str(self.code_dir),
            "versions": dict(self.versions),
            "script_sha256": dict(self.script_hashes),
        }


def engine_health(python: str | os.PathLike[str], code_dir: str | os.PathLike[str]) -> tuple[bool, str]:
    """计算环境自检（P0-2）：**不抛异常**，把真实原因说清楚。

    界面不能等到用户点了"开始实验"才报错——那时用户已经以为能跑。这里给的是
    "这台机器现在能不能算"，以及该怎么配。返回 (可用, 说明)，说明里**不含绝对路径**，
    只留文件名与配置项名字。

    P0-C：解释器路径**先看它属不属于当前平台**。Windows 上拿一个 POSIX 路径去探测，
    结果必然失败，而且会把一个与这台机器无关的 Linux 路径显示给用户——那既不是
    "缺依赖"也不是"脚本缺失"，是配置本身指错了系统。
    """
    def tidy(text: str) -> str:
        # 把路径压成文件名，三种写法都要处理（界面规范禁展示内部路径）：
        #   POSIX  /home/u/bin/python3      → python3
        #   Windows C:\dir\code            → code
        #   UNC     \\wsl.localhost\...\x  → x
        import re as _re

        def shorten(match: "_re.Match[str]") -> str:
            token = match.group(0)
            tail = _re.split(r"[\\/]", token.rstrip("\\/"))[-1]
            return tail or "（路径）"

        pattern = _re.compile(
            r"(?:[A-Za-z]:[\\/][^\s：:]*)"          # C:\...
            r"|(?:\\\\[^\s：:]+)"                    # \\wsl.localhost\...
            r"|(?:/[^\s：:]*[\\/][^\s：:]*)"         # /home/u/bin/python3
        )
        return pattern.sub(shorten, str(text))

    configured = str(python or "").strip()
    if not configured:
        bridge_ok, bridge_note = wsl_bridge_status()
        if IS_WINDOWS:
            return False, (
                "计算引擎没有配置：本界面运行在 Windows，而地震学引擎（obspy/scipy/"
                "matplotlib）与 40 GB 原始数据都在 WSL 里。"
                f"{bridge_note}。"
                "最小做法是在 WSL 里启动界面（推荐），或把 RESEARCHPILOT_ENGINE_PYTHON "
                "指向 Windows 侧一个装了 obspy 的解释器。"
                + ("" if not bridge_ok else "（已检测到 wsl.exe）")
            )
        return False, (
            "计算引擎没有配置：请把 RESEARCHPILOT_ENGINE_PYTHON 指向装有 obspy 的解释器，"
            "RESEARCHPILOT_ENGINE_CODE_DIR 指向脚本目录。"
        )
    if IS_WINDOWS and _posix_like(configured):
        _bridge_ok, bridge_note = wsl_bridge_status()
        return False, (
            "计算解释器配置成了 Linux 路径，Windows 进程无法执行它"
            "（该路径只在 WSL 里存在）。"
            f"{bridge_note}。"
            "可选：在 WSL 里启动界面（推荐），或把 RESEARCHPILOT_ENGINE_PYTHON "
            "指向 Windows 侧装有 obspy 的解释器。"
        )

    try:
        environment = EngineEnvironment.probe(python, code_dir)
    except EngineEnvironmentError as exc:
        return False, (
            f"{tidy(exc)}　→ 需要把 RESEARCHPILOT_ENGINE_PYTHON 指向装有 obspy 的解释器、"
            "RESEARCHPILOT_ENGINE_CODE_DIR 指向脚本目录；Windows 侧默认没有这套环境。"
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可见状态，不能静默
        return False, f"计算环境探测失败：{type(exc).__name__}：{tidy(exc)}"

    versions = "、".join(
        f"{name} {value}" for name, value in (environment.versions or {}).items() if value
    )
    scripts = len(environment.script_hashes or {})
    return True, f"计算环境可用（Python {environment.versions.get('python', '?')}，{versions}）；已校验 {scripts} 个脚本"

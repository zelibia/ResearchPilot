"""受控子进程执行：白名单、超时、取消、日志落盘。

三条不允许破的线：

1. **不用 shell**。命令是 `[解释器, "-B", 脚本, *参数]` 这样的列表，
   参数来自 `specs.py` 的强类型字段。没有字符串拼接，也就没有命令注入。
2. **脚本必须在白名单里**，而且必须是"脚本目录 + 白名单文件名"这一种形状。
3. **进程组隔离**。子进程用 `start_new_session=True` 起，超时或取消时按进程组
   发信号，避免留下孤儿进程继续吃 CPU。

顺带处理一个容易被忽略的污染：脚本之间是裸 import，运行时会在脚本目录生成
`__pycache__`。这里用 `-B` 加 `PYTHONDONTWRITEBYTECODE=1` 双保险，
保证既有科研代码目录一个字节都不被写。
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .env import SCRIPT_FILES


class RunnerError(RuntimeError):
    """执行前的校验失败（还没起进程）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class StageResult:
    """一段脚本的执行结果。失败也照样返回，由上层决定怎么记录。"""

    stage: str
    argv: list[str]
    status: str                     # succeeded | failed | timeout | cancelled
    returncode: int | None = None
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0
    stdout_path: str = ""
    stderr_path: str = ""
    error: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """给人看的命令字符串（仅用于记录，绝不拿它去执行）。"""
        return shlex.join(self.argv)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


class ControlledRunner:
    """把一段脚本跑起来，并保证它不会跑飞。"""

    def __init__(
        self,
        *,
        python: Path,
        code_dir: Path,
        log_dir: Path,
        default_timeout: float = 600.0,
    ) -> None:
        self.python = Path(python)
        self.code_dir = Path(code_dir).resolve()
        self.log_dir = Path(log_dir)
        self.default_timeout = float(default_timeout)

    # ---------------------------------------------------------------- 校验

    def _validate(self, spec_argv: list[str]) -> Path:
        if not spec_argv:
            raise RunnerError("空命令")
        if not all(isinstance(item, str) for item in spec_argv):
            raise RunnerError("命令参数必须是字符串")

        script = Path(spec_argv[0])
        if script.parent.resolve() != self.code_dir:
            raise RunnerError(f"脚本不在既定目录内：{script}")
        allowed = set(SCRIPT_FILES.values())
        if script.name not in allowed:
            raise RunnerError(f"脚本不在白名单内：{script.name}（允许：{sorted(allowed)}）")
        if not script.is_file():
            raise RunnerError(f"脚本不存在：{script}")
        return script

    def _subprocess_env(self) -> dict[str, str]:
        """最小环境：不继承界面的环境变量，也不给脚本写字节码的机会。"""
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "MPLBACKEND": "Agg",                 # 无显示环境，直接出图
            "PYTHONDONTWRITEBYTECODE": "1",      # 不在既有代码目录留 __pycache__
            "PYTHONUNBUFFERED": "1",
        }
        return base

    # ---------------------------------------------------------------- 执行

    def run(
        self,
        spec_argv: list[str],
        *,
        stage: str,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        poll_interval: float = 0.2,
    ) -> StageResult:
        """执行一段脚本。超时/取消都会真正终止进程组。"""
        script = self._validate(spec_argv)
        limit = float(timeout if timeout is not None else self.default_timeout)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.log_dir / f"{stage}.stdout.log"
        stderr_path = self.log_dir / f"{stage}.stderr.log"

        argv = [str(self.python), "-B", *spec_argv]
        result = StageResult(
            stage=stage, argv=argv, status="failed",
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            extra={"script": script.name, "timeout_s": limit},
        )

        started = time.monotonic()
        result.started_at = _now()
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(  # noqa: S603 - argv 由白名单与强类型字段拼成
                argv,
                cwd=str(self.code_dir),
                env=self._subprocess_env(),
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,   # 独立进程组，便于整组终止
                shell=False,
            )
            status, error = self._supervise(proc, limit, cancel_event, poll_interval)

        result.ended_at = _now()
        result.duration_s = round(time.monotonic() - started, 3)
        result.returncode = proc.returncode
        result.status = status
        result.error = error
        if status == "failed" and not error:
            result.error = f"退出码 {proc.returncode}"
        return result

    def _supervise(
        self,
        proc: subprocess.Popen,
        limit: float,
        cancel_event: threading.Event | None,
        poll_interval: float,
    ) -> tuple[str, str]:
        deadline = time.monotonic() + limit
        while True:
            code = proc.poll()
            if code is not None:
                return ("succeeded" if code == 0 else "failed"), ""
            if cancel_event is not None and cancel_event.is_set():
                self.terminate(proc)
                return "cancelled", "已被用户取消"
            if time.monotonic() > deadline:
                self.terminate(proc)
                return "timeout", f"超时（{limit:g}s）后已终止进程组"
            time.sleep(poll_interval)

    @staticmethod
    def terminate(proc: subprocess.Popen, grace: float = 10.0) -> None:
        """按进程组终止：先 SIGTERM，宽限期后 SIGKILL。"""
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

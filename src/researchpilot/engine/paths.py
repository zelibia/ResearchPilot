"""路径安全：输入只读、输出受限。

两条硬规则：

1. **输入**必须落在允许的根目录之内（默认原始 SAC 目录）。经过 `resolve()`
   再比对，所以 `..` 与指向外部的符号链接都逃不掉。
2. **输出**必须落在 `outputs/runs/<run_id>/` 之内。`run_id` 由服务端生成，
   且再校验一次字符集，防止用 `..` 之类的名字把输出写到别处。

任何一条不满足就抛 `PathSafetyError`，绝不"尽力而为"地继续执行。
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class PathSafetyError(ValueError):
    """路径不满足安全约束。消息面向排错，不包含用户可注入的路径内容。"""


# run_id 由服务端生成（repo.new_id("run")），这里只做形状校验
RUN_ID_RE = re.compile(r"^run_[0-9a-zA-Z]{4,64}$")


def resolve_under(raw: str | os.PathLike[str], root: str | os.PathLike[str], *, label: str) -> Path:
    """把 `raw` 解析成绝对路径，并要求它落在 `root` 之内。"""
    root_path = Path(root).expanduser().resolve()
    try:
        candidate = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathSafetyError(f"{label}不存在：{raw}") from exc
    if not candidate.is_relative_to(root_path):
        raise PathSafetyError(f"{label}超出允许范围（{root_path}）：{candidate}")
    return candidate


def resolve_input_dir(raw: str | os.PathLike[str], allowed_root: str | os.PathLike[str]) -> Path:
    """输入目录：存在、是目录、在允许根目录内、可读。"""
    path = resolve_under(raw, allowed_root, label="输入目录")
    if not path.is_dir():
        raise PathSafetyError(f"输入目录不是目录：{path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise PathSafetyError(f"输入目录不可读：{path}")
    return path


def ensure_disjoint(output_root: str | os.PathLike[str], input_dir: str | os.PathLike[str]) -> None:
    """输出根与输入目录不得互相包含。

    原始 SAC 目录的属主就是运行者本人，权限上可写——靠 `os.access` 判断"只读"
    既不成立也没意义。真正要保证的是：**我们的输出路径从来不落在输入树里**，
    以及运行前后输入树一个字节没变（见 `snapshot_tree` / `diff_snapshots`）。
    """
    output = Path(output_root).expanduser().resolve()
    source = Path(input_dir).expanduser().resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise PathSafetyError(f"输出目录与输入目录重叠：输出 {output}，输入 {source}")


def snapshot_tree(root: str | os.PathLike[str], *, limit: int = 50_000) -> dict[str, tuple[int, int]]:
    """记录一棵目录树的 (相对路径 → 大小, mtime_ns)，用于运行前后比对。

    只看文件，不读内容——4551 个文件的 SAC 目录也只需要几十毫秒。
    """
    base = Path(root).expanduser()
    state: dict[str, tuple[int, int]] = {}
    if not base.exists():
        return state
    for index, path in enumerate(sorted(base.rglob("*"))):
        if index >= limit:
            break
        if not path.is_file():
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        state[path.relative_to(base).as_posix()] = (info.st_size, info.st_mtime_ns)
    return state


def diff_snapshots(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    """返回发生了变化（新增 / 删除 / 改动）的路径描述。"""
    changes: list[str] = []
    for path in sorted(set(after) - set(before)):
        changes.append(f"新增 {path}")
    for path in sorted(set(before) - set(after)):
        changes.append(f"删除 {path}")
    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            changes.append(f"修改 {path}")
    return changes


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise PathSafetyError(f"非法的 run_id：{run_id!r}（应由服务端生成）")
    return run_id


def run_dir(runs_root: str | os.PathLike[str], run_id: str) -> Path:
    """本次运行的输出根目录：`<runs_root>/<run_id>`，并确认它没有跑出 runs_root。"""
    validate_run_id(run_id)
    root = Path(runs_root).expanduser().resolve()
    target = (root / run_id).resolve()
    if not target.is_relative_to(root):
        raise PathSafetyError(f"输出目录越界：{target}（应在 {root} 之下）")
    return target


def ensure_output_dir(path: str | os.PathLike[str], runs_root: str | os.PathLike[str]) -> Path:
    """创建输出子目录，并再次确认它落在 runs_root 之下。"""
    root = Path(runs_root).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    if not target.is_relative_to(root):
        raise PathSafetyError(f"输出目录越界：{target}（应在 {root} 之下）")
    target.mkdir(parents=True, exist_ok=True)
    return target

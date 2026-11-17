"""文献文件的路径解析（P0-1）。

为什么必须集中在一处：库里存的 `local_path` 是**入库那台机器**的写法。
Windows 侧存的是 `E:\\给wsl的文件\\...\\x.pdf`，而 WSL 里同一份文件是
`/mnt/e/给wsl的文件/.../x.pdf`。用 `pathlib` 直接处理跨系统的路径会出错——
在 Linux 上 `Path(r"E:\\a\\b.pdf")` 会被当成**一个文件名**（反斜杠不是分隔符），
于是 `.with_suffix(".pages.txt")`、`.name`、`.stem` 全部得到错误结果：

- 读不到按页正文 → 界面误报"PDF 未成功解析"；
- 标题候选变成一整条 Windows 路径；
- 文件名里本来能拆出的作者也拆不出来。

这里的三个函数是全项目唯一的路径入口，读 PDF 与按页正文都必须经过它们。
纯只读：只解析路径、只判断存在性，绝不写入或覆盖原始文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import PATHS

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_UNC_WSL_RE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\?(.*)$", re.IGNORECASE)


def normalize(raw: str) -> str:
    """把任意写法的路径统一成当前系统可用的字符串形式。只做格式转换，不判断存在。"""
    text = (raw or "").strip().strip('"')
    if not text:
        return ""
    unc = _UNC_WSL_RE.match(text)
    if unc:                      # \\wsl.localhost\Ubuntu\home\... → /home/...
        return "/" + unc.group(1).replace("\\", "/").lstrip("/")
    drive = _WINDOWS_DRIVE_RE.match(text)
    if drive:
        tail = drive.group(2).replace("\\", "/")
        # 在 Linux（WSL）里把 C:\ 映射到 /mnt/c；在 Windows 上保持盘符
        if Path("/mnt").is_dir() and Path(f"/mnt/{drive.group(1).lower()}").is_dir():
            return f"/mnt/{drive.group(1).lower()}/{tail}"
        return f"{drive.group(1).upper()}:\\" + tail.replace("/", "\\")
    return text.replace("\\", "/") if "/" not in text and "\\" in text else text


def file_name(raw: str) -> str:
    """文件名（含扩展名）。Windows 反斜杠与 POSIX 斜杠都当分隔符。"""
    text = normalize(raw)
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def stem(raw: str) -> str:
    name = file_name(raw)
    return name.rsplit(".", 1)[0] if "." in name else name


def pages_path(raw: str) -> Path | None:
    """按页正文文件（`<pdf>.pages.txt`）的路径。没有 local_path 时返回 None。"""
    resolved = resolve_local_path(raw, allow_missing=True)
    return resolved.with_suffix(".pages.txt") if resolved else None


def resolve_local_path(raw: str, *, allow_missing: bool = False) -> Path | None:
    """把库里的 local_path 解析成一个真实存在的路径；找不到返回 None。

    顺序：① 原样；② 按当前系统规范化（盘符 → /mnt/x）；③ 用仓库 uploads 目录兜底
    （同一条记录在另一台机器上入库时，绝对前缀会变，但 `uploads/...` 之后的部分不变）。
    """
    text = (raw or "").strip()
    if not text:
        return None

    candidates: list[Path] = []
    candidates.append(Path(text))
    normalized = normalize(text)
    if normalized and normalized != text:
        candidates.append(Path(normalized))

    marker = "uploads/"
    probe = normalized.replace("\\", "/")
    if marker in probe:
        candidates.append(Path(PATHS.uploads) / probe.split(marker, 1)[1])

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    if allow_missing:
        # 调用方只想拿到"应该在哪儿"的位置（例如 pages.txt 的推导）
        return Path(normalized) if normalized else None
    return None


def describe_missing(raw: str) -> str:
    """找不到 PDF 时，给用户一句能分辨原因的话（不猜、不含绝对路径）。"""
    if not (raw or "").strip():
        return "这篇文献没有关联的本地文件（多为自动导入的候选文献，只有元数据）"
    return (
        f"本地找不到 PDF 文件（{file_name(raw)} 或其所在目录已被移动/重命名）。"
        "原始文件不会被修改；重新上传同一份 PDF 即可恢复按页正文。"
    )

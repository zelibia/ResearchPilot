"""标题修正：为文献收集候选标题，并把人工选定的值写回。

设计取舍：**只提供候选与来源，不自动替换。**
检测到乱码后自动改用外部来源的标题，本质上就是"静默覆盖"——用户再也看不到
原始解析结果，也说不清标题是什么时候被换掉的。所以这里只产候选，采用与否由人点。

候选来源（都记录来源，界面上一目了然）：

| source | 取值 |
|---|---|
| `raw` | 当前入库值 / 原始解析值 |
| `filename` | PDF 文件名（去掉扩展名） |
| `body` | 正文首页里最像标题的一行 |
| `external:crossref` | Crossref 权威标题 |
| `external:openalex` | OpenAlex 标题 |
| `manual` | 人工输入 |
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..storage import repository as repo
from ..storage.models import Paper
from . import paths as paper_paths
from .normalize import normalize_title
from .text_quality import has_suspicious_controls, sanitize_for_display

_SOURCE_LABEL = {
    "raw": "原始解析值",
    "filename": "PDF 文件名",
    "filename:author": "PDF 文件名（作者部分）",
    "body": "正文首页",
    "external:crossref": "Crossref 权威元数据",
    "external:openalex": "OpenAlex 元数据",
    "manual": "人工输入",
    "manual:author": "人工输入（作者）",
    "embedded": "PDF 内嵌元数据",
    "first_page": "正文首页",
    "": "未记录",
}

_UNREADABLE_FILENAME_RE = re.compile(r"^[0-9a-f]{16,}$|^\d{6,}$|^[A-Za-z0-9_-]{1,3}$")
_PAGES_SUFFIX = ".pages.txt"

# 文件名里的分隔符：实测常见 "_" 与 "-"，也见全角形式
_NAME_SEPARATORS = re.compile(r"[_\-—－|｜]+")
# 标题特征词：带这些词的部分通常是标题，不是作者名
_TITLE_KEYWORDS = (
    "算法", "分析", "研究", "实现", "方法", "系统", "设计", "应用", "综述",
    "进展", "基于", "探讨", "实验", "检测", "识别", "预测", "模型", "技术",
    "初探", "浅析", "探究", "实践", "总结", "报告", "手册", "指南", "评述",
    "刍议", "论", "学报",
)
# 纯中文姓名：2–4 字
_CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
# 文件名尾部的版本 / 年份噪声
_TRAILING_NOISE_RE = re.compile(
    r"[\s_\-]*(v\d+(\.\d+)*|final|终稿|终版|修改稿|副本|拷贝|"
    r"\(\d{4}\)|（\d{4}）|\d{4}年?版?)$",
    re.IGNORECASE,
)


def _looks_like_author(part: str) -> bool:
    """像不像作者名。宁缺勿滥：拿不准就不给作者候选。"""
    text = (part or "").strip()
    if not text or len(text) > 30:
        return False
    if any(keyword in text for keyword in _TITLE_KEYWORDS):
        return False
    if _CHINESE_NAME_RE.match(text):
        return True
    # 多作者：顿号 / 逗号 / 空格分隔，每段都是 2–4 字中文
    pieces = [item for item in re.split(r"[，,、;；\s]+", text) if item]
    return bool(pieces) and all(_CHINESE_NAME_RE.match(item) for item in pieces)


def _split_filename(stem: str) -> tuple[str, str]:
    """把文件名拆成 (标题, 作者)。拆不出来时作者为空串。

    实测的命名习惯是 `标题_作者`，也有 `作者_标题`；两种都试，拿不准就不拆——
    错误地把标题当作者，比不给候选更糟。
    """
    cleaned = stem.strip()
    while True:
        trimmed = _TRAILING_NOISE_RE.sub("", cleaned).strip()
        if trimmed == cleaned:
            break
        cleaned = trimmed

    parts = [item.strip() for item in _NAME_SEPARATORS.split(cleaned) if item.strip()]
    if len(parts) < 2:
        return cleaned, ""

    # 先从尾部收集连续的人名段：`标题_张三_李四` 能整体识别出两位作者
    index = len(parts)
    while index > 0 and _looks_like_author(parts[index - 1]):
        index -= 1
    if index < len(parts):
        authors = parts[index:]
        head = " ".join(parts[:index]).strip()
        return (head or cleaned), "、".join(authors)

    # 再看开头是不是人名（`张三_标题`）。这里只认 2–3 字中文姓名：
    # 「环境噪声_互相关研究」这种把标题前半段误判成人名的代价太大。
    if _CHINESE_NAME_RE.match(parts[0]) and len(parts[0]) <= 3:
        head = " ".join(parts[1:]).strip()
        if head:
            return head, parts[0]
    return cleaned, ""


def _from_filename_candidates(paper: Paper) -> list[dict]:
    """从文件名产出标题与作者候选。作者候选必须由用户确认后才写入。"""
    if not paper.local_path:
        return []
    stem = paper_paths.stem(paper.local_path).strip()
    if not stem or _UNREADABLE_FILENAME_RE.match(stem) or has_suspicious_controls(stem):
        return []
    if len(stem) < 4:
        return []

    title, author = _split_filename(stem)
    out: list[dict] = [
        {
            "value": title,
            "source": "filename",
            "note": (
                "来自 PDF 文件名，已按「标题_作者」结构拆分；采用前请核对"
                if author else "来自 PDF 文件名；文件名常带编号或版本后缀，采用前请核对"
            ),
        }
    ]
    if author:
        out.append(
            {
                "value": author,
                "source": "filename:author",
                "note": "从文件名识别出的作者；需人工确认后才写入作者字段",
            }
        )
    return out


def source_label(source: str) -> str:
    return _SOURCE_LABEL.get(source, source or "未记录")


def _from_filename(paper: Paper) -> dict | None:
    candidates = _from_filename_candidates(paper)
    return candidates[0] if candidates else None


def _from_body(paper: Paper) -> dict | None:
    """正文首页里最像标题的一行。

    读的是入库时留下的 `<pdf>.pages.txt`，不需要重新解析 PDF；
    文件不在了就跳过，不编造。
    """
    if not paper.local_path:
        return None
    pages_file = paper_paths.pages_path(paper.local_path)
    if pages_file is None or not pages_file.is_file():
        return None
    try:
        text = pages_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    blocks = re.split(r"=====\s*第\s*\d+\s*页\s*=====", text)
    first_page = blocks[1] if len(blocks) > 1 else text
    for line in first_page.splitlines()[:25]:
        candidate = line.strip()
        if not (12 <= len(candidate) <= 200):
            continue
        if has_suspicious_controls(candidate):
            continue
        letters = sum(char.isalnum() for char in candidate)
        if letters >= len(candidate) * 0.6 and not candidate.lower().startswith(("doi", "http")):
            return {
                "value": candidate,
                "source": "body",
                "note": "正文首页里最像标题的一行；抽取自保留的按页正文",
            }
    return None


def _from_external(paper: Paper) -> list[dict]:
    out: list[dict] = []
    metadata = paper.external_metadata or {}
    for provider in ("crossref", "openalex"):
        title = (metadata.get(provider) or {}).get("title") or ""
        if title and not has_suspicious_controls(title):
            out.append(
                {
                    "value": title,
                    "source": f"external:{provider}",
                    "note": f"{source_label(f'external:{provider}')}；属于可信外部来源",
                }
            )
    return out


def candidate_titles(paper: Paper) -> list[dict]:
    """收集候选标题。去重按归一化后的文本做，重复值合并来源说明。"""
    raw_value = paper.title_raw or paper.title
    candidates: list[dict] = []
    if raw_value:
        candidates.append(
            {
                "value": raw_value,
                "source": "raw",
                "note": (
                    "原始解析值（含异常控制字符，已保留）"
                    if has_suspicious_controls(raw_value)
                    else "当前入库值"
                ),
            }
        )
    for extra in (*_from_filename_candidates(paper), _from_body(paper), *_from_external(paper)):
        # 作者候选不是标题候选，别混进标题列表
        if extra and extra.get("source") == "filename:author":
            continue
        if extra:
            candidates.append(extra)

    merged: dict[str, dict] = {}
    for item in candidates:
        key = normalize_title(item["value"]) or item["value"]
        if key in merged:
            existing = merged[key]
            if item["source"] not in existing["source"]:
                existing["source"] = f"{existing['source']}／{item['source']}"
        else:
            merged[key] = dict(item)
    return list(merged.values())


def apply_title(paper: Paper, value: str, source: str, note: str = "") -> Paper:
    """把人工选定的标题写回。

    - `title_raw` 只在为空时写入，保证原始解析值不被后续修改抹掉。
    - `title_source` / `title_confirmed_at` 记录依据与时间。
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("标题不能为空")
    if not paper.title_raw:
        paper.title_raw = paper.title
    paper.title = cleaned
    paper.title_source = source
    paper.title_confirmed_at = datetime.now().isoformat(timespec="seconds")
    if note:
        paper.title_candidates = [
            item for item in (paper.title_candidates or [])
            if normalize_title(item.get("value", "")) != normalize_title(cleaned)
        ] + [{"value": cleaned, "source": source, "note": note}]
    return repo.save(paper)


def display_title(paper: Paper) -> str:
    """目录里用的标题：异常时明确标出来，且不出现裸控制字符。"""
    title = sanitize_for_display(paper.title_raw or paper.title)
    if has_suspicious_controls(paper.title_raw or paper.title):
        return f"⚠ 标题待修正｜{title}" if title else "⚠ 标题待修正"
    if not title:
        return "（标题未识别）"
    return title


def detail_title(paper: Paper) -> str:
    """详情页的主标题。

    乱码时**不把乱码值当主标题**，也**不擅自拿候选顶替**——候选取自文件名，
    未经确认就顶上去等于静默改数据。这里给一个中性占位，真正的选择留给
    「标题修正」（默认收起，但一直可用）。
    """
    current = paper.title or ""
    if current and not has_suspicious_controls(current) and paper.title_source:
        return sanitize_for_display(current)      # 已人工确认过
    raw = paper.title_raw or current
    if has_suspicious_controls(raw):
        return "（标题乱码，待确认）"
    cleaned = sanitize_for_display(raw)
    return cleaned or "（标题未识别）"


def candidate_authors(paper: Paper) -> list[dict]:
    """作者候选。只给候选，写法与标题一致：人工确认后才落库。"""
    candidates: list[dict] = []
    current = (paper.authors or "").strip()
    if current:
        candidates.append(
            {
                "value": current,
                "source": "raw",
                "note": (
                    "原始解析值（含异常字符，已保留）"
                    if has_suspicious_controls(current) else "当前入库值"
                ),
            }
        )
    for item in _from_filename_candidates(paper):
        if item.get("source") == "filename:author":
            candidates.append(item)
    merged: dict[str, dict] = {}
    for item in candidates:
        key = normalize_title(item["value"]) or item["value"]
        merged.setdefault(key, dict(item))
    return list(merged.values())


def apply_author(paper: Paper, value: str, source: str, note: str = "") -> Paper:
    """把人工确认的作者写回，原始值保留在 `authors_raw`。"""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("作者不能为空")
    if not paper.authors_raw:
        paper.authors_raw = paper.authors
    paper.authors = cleaned
    paper.author_source = source
    paper.author_confirmed_at = datetime.now().isoformat(timespec="seconds")
    if note:
        paper.author_candidates = [
            item for item in (paper.author_candidates or [])
            if normalize_title(item.get("value", "")) != normalize_title(cleaned)
        ] + [{"value": cleaned, "source": source, "note": note}]
    return repo.save(paper)

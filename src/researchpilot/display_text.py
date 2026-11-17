"""把内部标识挡在用户界面之外（M1）。

三条规则，都是"渲染时才做"的处理，不改动库里的原始数据：

1. **行内条目编号不进正文**。计划里的步骤文本常常带 `（claim_ab12cd34）`——那是模型的
   写法，对用户没有意义。界面上把它去掉，出处由旁边的「PDF · 第 N 页」证据入口承担；
   导出的报告里则换成「（依据：PDF · 第 N 页）」，因为文档需要自解释。
2. **路径只显示文件名**。`E:\\...\\uploads\\proj_x\\a.pdf` 与 `/mnt/.../a.pdf` 都只留 `a.pdf`。
   Windows 与 WSL 的分隔符都要处理——用 `pathlib` 在跨平台下会把整条路径当成文件名。
3. 该模块只做字符串处理，不依赖 Streamlit，也不接触数据库。
"""

from __future__ import annotations

import re

# 形如 （claim_ab12cd34） / ［claim_…］ / （依据 claim_…） / （依据：claim_…、claim_…）
# 实际数据里模型会写"（依据 claim_xxx）"，所以不能只匹配"括号里只有编号"这一种写法。
CLAIM_ID_RE = re.compile(r"claim_[0-9a-z]{4,}")
BRACKET_RE = re.compile(r"[（(\[［]([^）)\]］]{0,120})[）)\]］]")
# 去掉编号后，括号里剩下的只能是连接词或标点，才算"纯出处括号"
CONNECTOR_RE = re.compile(r"^[\s依据见参见参考出自来自于数据来源、,，;；:：和与及/·\-]*$")

_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_SPACE_RE = re.compile(r"\s+([，。；：、)）])")
_EMPTY_BRACKET_PAIR_RE = re.compile(r"[（(]\s*[，、;；]?\s*[)）]")
# 去掉括号后可能留下悬空连接词：「…并给出与（，」→「…」
_DANGLING_RE = re.compile(r"[，、]?(?:与|和|及|、)$|(?:与|和|及)(?=[，。；：])")
# 悬空的提示动词：「…并给出」/「…参见」——括号被拿走后这些词没有宾语
_TRAILING_VERB_RE = re.compile(
    r"[，、]?(?:并|且)?(?:给出|引用|参考|参见|依据|据|见)(?=[。；，]|$)"
)


def short_path(value: str) -> str:
    """只保留文件名。同时兼容 `\\` 与 `/` 两种分隔符。"""
    text = (value or "").strip()
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def strip_claim_refs(text: str) -> str:
    """去掉行内条目编号，并收拾留下的小空白。库里的原文不动。"""
    def _clean_bracket(match: re.Match) -> str:
        inner = match.group(1)
        ids = CLAIM_ID_RE.findall(inner)
        if not ids:
            return match.group(0)          # 普通括号（与条目无关），原样保留
        rest = CLAIM_ID_RE.sub("", inner)
        if CONNECTOR_RE.match(rest):
            return ""                      # 整段括号都是出处 → 去掉，出处由证据入口承担
        rest = rest.strip(" 　、,，;；:：/·-")
        return f"（{rest}）" if rest else ""

    value = BRACKET_RE.sub(_clean_bracket, text or "")
    value = _MULTI_SPACE_RE.sub(" ", value)
    value = _TRAILING_SPACE_RE.sub(r"\1", value)
    value = _EMPTY_BRACKET_PAIR_RE.sub("", value)
    value = _DANGLING_RE.sub("", value)
    value = _TRAILING_VERB_RE.sub("", value)
    return value.strip()


def page_label(claim_id: str, pages: dict[str, int | None] | None = None) -> str:
    """一个条目编号在人话里的写法。"""
    page = (pages or {}).get(claim_id)
    return f"PDF · 第 {page} 页" if page else "出处未定位"


def humanize_claim_refs(text: str, pages: dict[str, int | None] | None = None) -> str:
    """把行内编号换成可读出处，用于导出文档（界面用 strip_claim_refs）。"""
    def _replace(match: re.Match) -> str:
        inner = match.group(1)
        ids = CLAIM_ID_RE.findall(inner)
        if not ids:
            return match.group(0)          # 与条目无关的括号，原样保留
        labels = "、".join(page_label(claim_id, pages) for claim_id in ids)
        if CONNECTOR_RE.match(CLAIM_ID_RE.sub("", inner)):
            return f"（依据：{labels}）"
        return f"（{CLAIM_ID_RE.sub(labels, inner)}）"

    value = BRACKET_RE.sub(_replace, text or "")
    return _MULTI_SPACE_RE.sub(" ", value).strip()


def page_map(claims) -> dict[str, int | None]:
    """从一批条目构建 {claim_id: page}，供上面两个函数查询。"""
    return {claim.id: getattr(claim, "page", None) for claim in claims}

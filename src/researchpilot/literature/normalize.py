"""做一致性对比前的字符串规范化。

这里的每个函数都对应一个真实踩过的坑：

- DOI **大小写不敏感**。PDF 里抽出来是 `...03374.X`，Crossref 返回 `...03374.x`，
  不规范化就会把同一篇文献判成"DOI 冲突"。
- 标题里经常有换行、连字符断词、多余空格，逐字符比较必然误报。
- 作者在不同来源里的写法差别很大（"Bensen, G. D." / "G. D. Bensen" / "Bensen"），
  所以只比较姓氏。
"""

from __future__ import annotations

import difflib
import re

_DOI_PREFIX_RE = re.compile(r"^\s*(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_YEAR_RE = re.compile(r"\b(1[6-9]\d{2}|20\d{2}|21\d{2})\b")


def normalize_doi(value: str | None) -> str:
    """DOI 归一化：去掉 URL 前缀、转小写、去掉结尾标点。大小写不敏感。"""
    if not value:
        return ""
    doi = _DOI_PREFIX_RE.sub("", str(value).strip())
    doi = doi.strip().rstrip(".,;)").lower()
    return doi if doi.startswith("10.") else ""


def normalize_title(value: str | None) -> str:
    """标题归一化：转小写，只留字母数字，去掉所有标点与空白差异。"""
    if not value:
        return ""
    return _NON_WORD_RE.sub(" ", str(value).lower()).strip()


def title_similarity(left: str | None, right: str | None) -> float:
    """0~1 的相似度。用 difflib，不引入额外依赖。"""
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def extract_year(value: str | None) -> str:
    """从任意字符串里挑出年份。取不到返回空串。"""
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    match = _YEAR_RE.search(str(value))
    return match.group(0) if match else ""


def surname(name: str | None) -> str:
    """从一个人名里取姓氏，用于宽松比较。

    支持的写法：`Bensen, G. D.`、`G. D. Bensen`、`Bensen`。
    取不到时返回归一化后的最后一个词。
    """
    if not name:
        return ""
    text = str(name).strip()
    if not text:
        return ""
    if "," in text:
        head = text.split(",", 1)[0]
        return _NON_WORD_RE.sub("", head.lower())
    parts = [p for p in re.split(r"\s+", text) if p]
    if not parts:
        return ""
    return _NON_WORD_RE.sub("", parts[-1].lower())


def mentions_surname(text: str | None, family_name: str | None) -> bool:
    """判断一段文本里是否出现某个姓氏（用于"作者猜得对不对"这种宽松核查）。"""
    target = surname(family_name)
    if not target or not text:
        return False
    return target in _NON_WORD_RE.sub("", str(text).lower())

"""文本质量检测：控制字符识别与展示净化。

背景：中文 PDF 用子集化 CID 字体且缺 ToUnicode 映射时，PyMuPDF 只能返回字形索引，
落到控制字符区。实测入库的标题是：

    '按时间抽取的基\\x07\\x07\\x07算法分析及\\x07\\x07\\x07\\x07\\x07实现'

两条原则：

1. **只检测与净化，不改数据。** 原始值一律保留在 `Paper.title_raw`。
2. **净化只用于展示。** 不能拿净化后的字符串覆盖入库值——那等于把证据抹掉。
"""

from __future__ import annotations

import unicodedata

# C0 控制字符（去掉 \t \n \r，它们是正常排版字符）+ DEL + C1 控制字符
_CONTROL_EXCEPTIONS = {"\t", "\n", "\r"}
CONTROL_PLACEHOLDER = "□"

# 判定阈值：出现 2 个以上控制字符，或占比超过 5%，就认为标题不可信
MIN_CONTROL_COUNT = 2
MIN_CONTROL_RATIO = 0.05


def is_control(char: str) -> bool:
    """是否属于异常控制字符。"""
    if char in _CONTROL_EXCEPTIONS:
        return False
    code = ord(char)
    if code < 32 or code == 127:
        return True
    return unicodedata.category(char) == "Cc" or unicodedata.category(char) == "Cf"


def control_chars(text: str | None) -> list[str]:
    if not text:
        return []
    return [char for char in str(text) if is_control(char)]


def control_stats(text: str | None) -> dict[str, float | int]:
    """控制字符的个数与占比。空文本返回全 0。"""
    if not text:
        return {"count": 0, "total": 0, "ratio": 0.0}
    value = str(text)
    found = control_chars(value)
    total = len(value)
    return {
        "count": len(found),
        "total": total,
        "ratio": round(len(found) / total, 4) if total else 0.0,
    }


def has_suspicious_controls(text: str | None) -> bool:
    """标题或其他短文本是否含异常控制字符。"""
    stats = control_stats(text)
    return bool(
        stats["count"] >= MIN_CONTROL_COUNT
        or (stats["count"] >= 1 and stats["ratio"] >= MIN_CONTROL_RATIO)
    )


def sanitize_for_display(text: str | None, placeholder: str = CONTROL_PLACEHOLDER) -> str:
    """把控制字符换成可见占位符，**仅供界面展示**。"""
    if not text:
        return ""
    return "".join(placeholder if is_control(char) else char for char in str(text)).strip()


def describe_controls(text: str | None) -> str:
    """给 parse_warnings 用的一句话说明。"""
    stats = control_stats(text)
    if not stats["count"]:
        return ""
    codes = sorted({f"\\x{ord(char):02x}" for char in control_chars(text)})
    return (
        f"标题含 {stats['count']} 个异常控制字符（{', '.join(codes)}，"
        f"占比 {stats['ratio']:.0%}）。这类字符通常是子集化 CID 字体缺 ToUnicode 映射"
        "导致的字形索引错位；原始值已保留，可在「标题修正」里采用其他候选或手工填写。"
    )


# --------------------------------------------------------------------------
# 正文可靠性：乱码全文不许交给模型去"猜"
# --------------------------------------------------------------------------

REPLACEMENT_CHAR = "\ufffd"          # PyMuPDF 抽不出字形时的替换符
MIN_BODY_CHARS = 200                  # 少于这个长度不足以支撑抽取
BODY_BAD_RATIO = 0.02                 # 控制字符 / 替换符 / 私用区占比上限
BODY_MIN_READABLE = 0.60              # 可读字符占比下限


def _is_private_use(char: str) -> bool:
    return 0xE000 <= ord(char) <= 0xF8FF


def _is_readable(char: str) -> bool:
    """中文、字母、数字和常见标点算"可读"。"""
    if char.isalnum():
        return True
    if 0x4E00 <= ord(char) <= 0x9FFF:            # CJK 基本区
        return True
    if 0x3000 <= ord(char) <= 0x303F:            # CJK 标点
        return True
    return char in " \t\n\r.,;:!?()[]{}<>/\\-_%+='\"，。；：！？（）【】《》、—…"


def body_quality(text: str | None) -> dict[str, float | int | bool]:
    """正文的可读性评估。用于判断"能不能交给模型抽取"。"""
    value = str(text or "")
    total = len(value)
    if not total:
        return {"total": 0, "bad": 0, "bad_ratio": 0.0,
                "readable_ratio": 0.0, "reliable": False}

    bad = sum(
        1 for char in value
        if is_control(char) or char == REPLACEMENT_CHAR or _is_private_use(char)
    )
    readable = sum(1 for char in value if _is_readable(char))
    bad_ratio = round(bad / total, 4)
    readable_ratio = round(readable / total, 4)
    reliable = (
        total >= MIN_BODY_CHARS
        and bad_ratio <= BODY_BAD_RATIO
        and readable_ratio >= BODY_MIN_READABLE
    )
    return {
        "total": total,
        "bad": bad,
        "bad_ratio": bad_ratio,
        "readable_ratio": readable_ratio,
        "reliable": reliable,
    }


def body_is_reliable(text: str | None, *, min_chars: int = MIN_BODY_CHARS) -> tuple[bool, str]:
    """能不能拿这段正文做 AI 抽取？返回 (是否可靠, 原因)。

    判不可靠时**不要**调用模型：纯文本模型没有能力把字形索引错位"读回来"，
    让它猜等于生产伪造的科研结论。这种情况该走 OCR / 视觉识别降级（按需），
    而不是让模型硬编。
    """
    stats = body_quality(text)
    if stats["total"] < min_chars:
        return False, f"正文只有 {stats['total']} 个字符，不足以支撑抽取"
    if stats["bad_ratio"] > BODY_BAD_RATIO:
        return False, (
            f"正文含 {stats['bad_ratio']:.1%} 的异常字符（控制字符/替换符/私用区），"
            "属于字体映射缺失导致的乱码；纯文本模型无法可靠读取，不应据此抽取"
        )
    if stats["readable_ratio"] < BODY_MIN_READABLE:
        return False, (
            f"正文可读字符占比仅 {stats['readable_ratio']:.1%}，判断为乱码文本，"
            "不应据此抽取"
        )
    return True, ""

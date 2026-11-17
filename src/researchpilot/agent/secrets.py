"""密钥脱敏：日志与错误信息里永远不出现完整密钥。

这里只做"把已知密钥与常见密钥形态替换成 `***`"，不保存任何密钥。
"""

from __future__ import annotations

import logging
import re

MASK = "***"

# 常见密钥形态：OpenAI 系 sk-...，以及 Authorization 头
_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"),
)


def mask_key(key: str) -> str:
    """给界面看的密钥指纹：只露头尾几位，永不显示完整密钥。"""
    text = (key or "").strip()
    if not text:
        return "（未设置）"
    if len(text) <= 10:
        return f"{text[:2]}{MASK}"
    return f"{text[:4]}{MASK}{text[-4:]}"


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """把文本里的密钥替换掉。已知密钥优先按原样替换。"""
    result = str(text or "")
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            result = result.replace(secret, MASK)
    for pattern in _PATTERNS:
        result = pattern.sub(MASK, result)
    return result


class RedactingFilter(logging.Filter):
    """给日志加一层脱敏。挂到 root handler 上，任何模块都不会漏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: (redact(str(value)) if isinstance(value, str) else value)
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(str(value)) if isinstance(value, str) else value
                    for value in record.args
                )
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction() -> None:
    """给 root logger 装上脱敏过滤器（幂等）。"""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, RedactingFilter) for item in handler.filters):
            handler.addFilter(RedactingFilter())

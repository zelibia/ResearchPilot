"""兼容层：老代码从这里导入 LLMClient / LLMUnavailable。

真正的实现在 `client.py`（统一入口）、`providers.py`（可替换 provider）、
`session.py`（会话级 BYOK 凭据）。保留这个模块是为了不改动既有调用点
（`scripts/smoke_m1.py` 等），不是新的入口。
"""

from __future__ import annotations

from .client import LLMClient
from .providers import (
    DEFAULT_PROVIDER,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_MODELS,
    LLMAuthError,
    LLMBadResponseError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailable,
    ProbeResult,
    ProviderConfig,
)
from .session import CredentialStore, Credentials

__all__ = [
    "LLMClient", "LLMUnavailable", "LLMAuthError", "LLMRateLimitError",
    "LLMConnectionError", "LLMBadResponseError", "LLMConfigError", "LLMError",
    "ProbeResult", "ProviderConfig", "Credentials", "CredentialStore",
    "DEFAULT_PROVIDER", "DEEPSEEK_BASE_URL", "DEEPSEEK_DEFAULT_MODEL", "DEEPSEEK_MODELS",
]

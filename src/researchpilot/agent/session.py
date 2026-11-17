"""会话级凭据：BYOK 的密钥只活在当前服务端会话内存里。

硬约束（对应 C3 补充需求第三节）：

- 密钥**只**保存在当前会话的内存里（Streamlit 的 `st.session_state`，
  服务端、每会话独立），不写 SQLite / JSON / 日志 / 缓存 / URL / 浏览器存储 / Git；
- 不放进模块级变量或共享缓存，两个会话的密钥互不可见；
- 支持主动断开并清除；
- 界面只显示掩码指纹，永不显示完整密钥。

留了一条自托管单用户的后路：`.env` 里的 `LLM_API_KEY` 作为**服务端配置**兜底，
但一旦本会话自己连接过，就**只用本会话的**（`source` 字段可区分）。
"""

from __future__ import annotations

import os

from collections.abc import MutableMapping
from dataclasses import dataclass, field

from . import providers as pv
from .secrets import mask_key

SESSION_KEY = "_llm_credentials"
PROBE_KEY = "_llm_last_probe"

# 密钥形状检查：明显不是密钥的值不许发出去浪费一次请求，也避免 401 被误读成"密钥失效"
MIN_KEY_LEN = 8


def key_shape_problem(api_key: str, *, base_url: str) -> str:
    """返回不合法原因的说明；没问题返回空串。

    只拦"一眼就能看出不是密钥"的情况：
    - 含空白或换行（多半是粘进了整条命令或带换行的文本）；
    - 含路径分隔符（多半是粘进了文件名或路径，例如以 `p.py` 结尾）；
    - DeepSeek 官方地址要求以 `sk-` 开头。
    """
    value = api_key or ""
    if not value.strip():
        return "API Key 不能为空"
    if value != value.strip():
        return "API Key 首尾有空白字符，请只粘贴密钥本身"
    if any(ch.isspace() for ch in value):
        return "API Key 里含空格或换行：看起来粘进了别的文本，请只粘贴密钥本身"
    if "/" in value or "\\" in value:
        return (
            f"API Key 里含路径分隔符（以「…{value[-6:]}」结尾）："
            "看起来粘进了文件名或路径，而不是密钥"
        )
    if value.endswith((".py", ".txt", ".json", ".env", ".md", ".bat", ".sh")):
        return (
            f"API Key 以「{value[-6:]}」结尾：这是文件名，不是密钥。"
            "请粘贴服务商控制台里那串以 sk- 开头的密钥"
        )
    if len(value) < MIN_KEY_LEN:
        return f"API Key 太短（{len(value)} 个字符），不像是密钥"
    if pv.is_deepseek(base_url) and not value.startswith("sk-"):
        return (
            f"DeepSeek 的密钥以「sk-」开头，当前这串以「{value[:4]}」开头。"
            "请确认粘贴的是 API Key，而不是文件名、命令或其它内容"
        )
    return ""

# 只在**没有 Streamlit 运行时**用到（命令行脚本、离线测试）：命令行本来就是
# 单用户单进程，进程结束即消失。Web 场景永远走 st.session_state，绝不走这里。
_EPHEMERAL: dict = {}


@dataclass(frozen=True)
class Credentials:
    """一次调用要用的凭据。`api_key` 不参与 repr / 序列化。"""

    provider: str = pv.DEFAULT_PROVIDER
    base_url: str = pv.DEEPSEEK_BASE_URL
    model: str = pv.DEEPSEEK_DEFAULT_MODEL
    timeout: float = 120.0
    api_key: str = field(default="", repr=False)
    source: str = "session"          # session | env
    # 思考模式：只有声明支持的服务端（目前 DeepSeek 官方地址）才发这个参数。
    # "disabled" 时不带；"enabled" 时带 thinking + reasoning_effort。
    thinking: str = "disabled"       # disabled | enabled
    reasoning_effort: str = ""       # low | medium | high | ""（交给服务端默认）

    def config(self, **overrides) -> pv.ProviderConfig:
        payload = dict(
            provider=self.provider, base_url=self.base_url, model=self.model,
            timeout=self.timeout,
            thinking=self.thinking if self.thinking in {"disabled", "enabled"} else "disabled",
            reasoning_effort=self.reasoning_effort,
        )
        payload.update(overrides)
        return pv.ProviderConfig(**payload)

    @property
    def key_fingerprint(self) -> str:
        return mask_key(self.api_key)


class CredentialStore:
    """绑在一个可变映射上。生产用法是 `CredentialStore.for_streamlit()`；
    测试可以直接给一个普通 dict——两个 dict 就等于两个独立会话。"""

    def __init__(self, mapping: MutableMapping) -> None:
        self._mapping = mapping

    @classmethod
    def for_streamlit(cls) -> "CredentialStore":
        import streamlit as st

        return cls(st.session_state)

    # ------------------------------------------------------------ 读写

    def connect(self, *, provider: str, base_url: str, model: str,
                api_key: str, allow_custom_url: bool = False,
                timeout: float | None = None) -> Credentials:
        """校验地址与密钥形状后写入本会话内存。地址不合规直接拒绝。"""
        if not api_key or not api_key.strip():
            raise pv.LLMConfigError("API Key 不能为空")
        resolved = pv.validate_base_url(base_url, allow_custom=allow_custom_url)
        problem = key_shape_problem(api_key, base_url=resolved)
        if problem:
            raise pv.LLMConfigError(problem)
        if not model or not model.strip():
            raise pv.LLMConfigError("模型名称不能为空")

        from ..config import LLM as _llm

        credentials = Credentials(
            provider=(provider or pv.DEFAULT_PROVIDER).strip().lower(),
            base_url=resolved,
            model=model.strip(),
            timeout=float(timeout or _llm.timeout or 120.0),
            api_key=api_key.strip(),
            source="session",
        )
        pv.get_provider(credentials.provider)   # 名字不认识就报错，不做猜测
        self._mapping[SESSION_KEY] = {
            "provider": credentials.provider,
            "base_url": credentials.base_url,
            "model": credentials.model,
            "timeout": credentials.timeout,
            "api_key": credentials.api_key,
            "source": credentials.source,
            "thinking": credentials.thinking,
            "reasoning_effort": credentials.reasoning_effort,
        }
        return credentials

    def supports_thinking(self) -> bool:
        """当前连接的服务端是否声明支持思考参数（目前只有 DeepSeek 官方地址）。"""
        current = self.current()
        return bool(current and pv.is_deepseek(current.base_url))

    def set_thinking(self, enabled: bool, effort: str = "") -> Credentials | None:
        """改本会话的思考模式设置（不动密钥）。未连接时返回 None。"""
        current = self.session_credentials()
        if current is None:
            return None
        raw = dict(self._mapping.get(SESSION_KEY) or {})
        raw["thinking"] = "enabled" if enabled else "disabled"
        raw["reasoning_effort"] = (effort or "").strip() if enabled else ""
        self._mapping[SESSION_KEY] = raw
        return self.session_credentials()

    def disconnect(self) -> None:
        """断开并清除：把密钥从内存里删掉，之后无法再用旧密钥。"""
        self._mapping.pop(SESSION_KEY, None)
        self._mapping.pop(PROBE_KEY, None)

    def session_credentials(self) -> Credentials | None:
        raw = self._mapping.get(SESSION_KEY)
        if not isinstance(raw, dict) or not raw.get("api_key"):
            return None
        return Credentials(
            provider=str(raw.get("provider") or pv.DEFAULT_PROVIDER),
            base_url=str(raw.get("base_url") or pv.DEEPSEEK_BASE_URL),
            model=str(raw.get("model") or pv.DEEPSEEK_DEFAULT_MODEL),
            timeout=float(raw.get("timeout") or env_timeout()),
            api_key=str(raw.get("api_key") or ""),
            source="session",
            thinking="enabled" if str(raw.get("thinking") or "") == "enabled" else "disabled",
            reasoning_effort=str(raw.get("reasoning_effort") or ""),
        )

    def current(self) -> Credentials | None:
        """本会话优先；没连过才看服务端 `.env` 配置。"""
        return self.session_credentials() or env_credentials()

    # ------------------------------------------------------------ 状态

    def record_probe(self, result: pv.ProbeResult) -> None:
        self._mapping[PROBE_KEY] = {
            "ok": result.ok, "provider": result.provider, "model": result.model,
            "elapsed_s": result.elapsed_s, "detail": result.detail,
        }

    def last_probe(self) -> dict | None:
        raw = self._mapping.get(PROBE_KEY)
        return dict(raw) if isinstance(raw, dict) else None

    def status(self) -> dict:
        """给界面看的连接状态。**不含完整密钥**。"""
        credentials = self.current()
        if credentials is None:
            return {
                "connected": False,
                "provider": pv.DEFAULT_PROVIDER,
                "model": pv.DEEPSEEK_DEFAULT_MODEL,
                "base_url": pv.DEEPSEEK_BASE_URL,
                "source": "none",
                "key": "",
                "label": "未连接",
            }
        label = pv.KNOWN_HOSTS.get(pv.host_of(credentials.base_url), credentials.provider)
        return {
            "connected": True,
            "provider": credentials.provider,
            "model": credentials.model,
            "base_url": credentials.base_url,
            "source": credentials.source,
            "key": credentials.key_fingerprint,
            "label": (
                f"{label} / {credentials.model}"
                + ("（服务端配置）" if credentials.source == "env" else "（本会话）")
            ),
        }

    def require(self) -> Credentials:
        credentials = self.current()
        if credentials is None:
            raise pv.LLMUnavailable(
                "尚未连接 AI 模型。请在「用户设置 → AI 模型」里填入自己的 API Key；"
                "不连接也能继续使用文献解析、元数据核查与科研计算。"
            )
        return credentials


def _live_llm_settings() -> tuple[str, str, str, str]:
    """按「Secrets → 环境变量 → 启动时快照」的顺序取模型配置。

    `config.LLM` 是导入时固化的快照，而云端（Streamlit Community Cloud）
    的 Secrets 可能在进程启动之后才写入。这里每次调用都重新读一遍，
    使「在控制台补填 Key」可以立即生效，无需重启。
    本地运行时 st.secrets 不存在，自然退回到环境变量。
    """
    from ..config import LLM

    def pick(name: str, fallback: str) -> str:
        try:
            import streamlit as st

            value = st.secrets.get(name)  # type: ignore[union-attr]
            if value:
                return str(value).strip()
        except Exception:  # noqa: BLE001 - 无 Streamlit 运行时或未配置
            pass
        raw = os.environ.get(name, "")
        if raw.strip():
            return raw.strip()
        return (fallback or "").strip()

    return (
        pick("LLM_PROVIDER", LLM.provider),
        pick("LLM_BASE_URL", LLM.base_url),
        pick("LLM_MODEL", LLM.model),
        pick("LLM_API_KEY", LLM.api_key),
    )


def env_credentials() -> Credentials | None:
    """服务端配置的兜底凭据（自托管单用户 / 云端 Secrets 场景）。"""
    provider, base_url_raw, model, api_key = _live_llm_settings()
    if not (api_key and base_url_raw and model):
        return None
    try:
        base_url = pv.validate_base_url(base_url_raw, allow_custom=True)
    except pv.LLMConfigError:
        return None
    return Credentials(
        provider=(provider or pv.DEFAULT_PROVIDER).strip().lower(),
        base_url=base_url,
        model=model.strip(),
        timeout=env_timeout(),
        api_key=api_key.strip(),
        source="env",
    )


def env_timeout() -> float:
    """服务端配置的超时（`.env` 的 LLM_TIMEOUT，默认 120 秒）。"""
    from ..config import LLM

    return float(LLM.timeout or 120.0)


def store() -> CredentialStore:
    """当前会话的凭据存储。

    Web 场景返回绑在 `st.session_state` 上的存储——每个会话一份，彼此看不见。
    没有 Streamlit 运行时（命令行/测试）才退回进程内的临时存储。
    """
    try:
        return CredentialStore.for_streamlit()
    except Exception:  # noqa: BLE001 - 没有运行时是预期情况，不是错误
        return CredentialStore(_EPHEMERAL)

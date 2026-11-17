"""模型服务接入：可替换的 provider、DeepSeek 默认配置、URL 安全校验。

三条约束：

1. **provider 可替换**：注册表按名字取；取不到直接报错，不猜接口。
2. **不带密钥的请求不发**：`validate_base_url` 先检查地址，再谈调用。
3. **错误要真实**：鉴权失败、限流、连接失败分开报，界面照原样显示。

注意：本模块不保存任何密钥。密钥由调用方按次传入（见 session.py 的会话级存储）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol
from urllib.parse import urlparse

import requests

from .secrets import redact


# ----------------------------------------------------------------- 默认配置

DEFAULT_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_MODELS: tuple[str, ...] = ("deepseek-flash", "deepseek-v4-pro")

# 已知服务商域名 → 展示名。不在表里的地址需要用户显式确认后才允许使用。
KNOWN_HOSTS: dict[str, str] = {
    "api.deepseek.com": "DeepSeek",
}

# 本地模型服务（Ollama / vLLM 等）允许 http，因为流量不出机器
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_CREDENTIALS_IN_URL = re.compile(r"^[^/]+:[^/]+@")


class LLMError(RuntimeError):
    """模型调用失败。message 面向用户，必须是真实原因。"""


class LLMUnavailable(LLMError):
    """未连接模型（没有凭据）。"""


class LLMAuthError(LLMError):
    """鉴权失败：密钥无效、过期或没有权限。"""


class LLMRateLimitError(LLMError):
    """被限流。"""


class LLMConnectionError(LLMError):
    """网络层失败：连接被拒、超时、DNS。"""


class LLMTimeoutError(LLMConnectionError):
    """请求超时。**超时不做重试**：重试只会把等待时间翻倍，对用户没有任何好处。"""


class LLMBadResponseError(LLMError):
    """返回内容无法使用（结构不对、不是合法 JSON）。"""


class LLMConfigError(LLMError):
    """配置本身不合法（地址不合规等）。"""


@dataclass(frozen=True)
class ProviderConfig:
    """与密钥无关的那部分配置。可以安全地记录、展示、写进快照。"""

    provider: str = DEFAULT_PROVIDER
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_DEFAULT_MODEL
    timeout: float = 120.0
    max_tokens: int = 4000
    temperature: float = 0.1
    json_mode: str = "auto"          # auto | on | off
    # DeepSeek 等支持思考模式的模型：默认关掉。
    # 开着的时候，思考过程会占用输出预算，短请求（例如连通性测试）容易
    # 把预算全花在思考上，正文为空——看起来像"模型没回答"。
    thinking: str = "disabled"       # disabled | enabled | omit
    # 思考强度：只在思考模式开启、且服务端支持时才带（DeepSeek: low/medium/high）。
    # 空串 = 不带这个字段，交给服务端默认。
    reasoning_effort: str = ""

    def describe(self) -> str:
        label = KNOWN_HOSTS.get(urlparse(self.base_url).hostname or "", self.provider)
        return f"{label} / {self.model}"

    def public_dict(self) -> dict[str, object]:
        """可安全落库/展示的形式（无密钥）。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "json_mode": self.json_mode,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
        }

    def replace(self, **overrides) -> "ProviderConfig":
        from dataclasses import replace

        return replace(self, **overrides)


# DeepSeek（以及多数 OpenAI 兼容服务）在 response_format=json_object 时要求
# 提示词里**出现 json 字样**，否则直接拒 400：
#   "Prompt must contain the word 'json' ..."
# 所以只要开结构化输出，就确保提示词里带一句明确要求。
JSON_INSTRUCTION = "请只输出合法 JSON，不要输出解释文字或 Markdown 代码块之外的内容。"


def mentions_json(*texts: str) -> bool:
    return any("json" in (text or "").lower() for text in texts)


def ensure_json_instruction(system: str, user: str) -> tuple[str, str]:
    """结构化输出前保证提示词里有 json 字样；没有就补一句。"""
    if mentions_json(system, user):
        return system, user
    system = f"{system}\n{JSON_INSTRUCTION}".strip() if system else JSON_INSTRUCTION
    return system, user


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_s: float
    used_json_mode: bool = False
    usage: dict = field(default_factory=dict)
    warning: str = ""
    finish_reason: str = ""
    has_reasoning: bool = False
    reasoning_len: int = 0


@dataclass
class ProbeResult:
    ok: bool
    provider: str
    model: str
    elapsed_s: float
    detail: str = ""
    supports_json: bool = False
    usage: dict = field(default_factory=dict)


def validate_base_url(raw: str, *, allow_custom: bool = False) -> str:
    """校验服务地址，返回规范化结果。不合规直接抛错——**不带密钥试**。"""
    text = (raw or "").strip()
    if not text:
        raise LLMConfigError("服务地址为空")
    if _CREDENTIALS_IN_URL.match(text.split("//", 1)[-1]):
        raise LLMConfigError("服务地址里不能内嵌用户名或密钥")

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise LLMConfigError(f"服务地址必须以 http(s):// 开头：{text}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise LLMConfigError(f"服务地址缺少主机名：{text}")
    if parsed.query or parsed.fragment:
        raise LLMConfigError("服务地址不应带查询参数或片段")

    if parsed.scheme == "http" and host not in LOCAL_HOSTS:
        raise LLMConfigError(
            "公网地址必须使用 HTTPS；只有本机地址（localhost / 127.0.0.1）允许 http"
        )
    if host not in KNOWN_HOSTS and not allow_custom:
        raise LLMConfigError(
            f"未验证的服务地址：{host}。如果确认可信，请在界面上勾选"
            "「我确认使用自建/第三方地址」后重试"
        )
    return text.rstrip("/")


def chat_endpoint(base_url: str) -> str:
    """OpenAI 兼容的补全端点。DeepSeek 的 base 带不带 /v1 都支持。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def host_of(url: str) -> str:
    """取主机名（小写）。解析不出来返回空串。"""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_deepseek(base_url: str) -> bool:
    return host_of(base_url) == "api.deepseek.com"


def thinking_payload(config: ProviderConfig) -> dict | None:
    """要不要带 thinking 参数。

    只对 DeepSeek 官方地址发这个字段：别的 OpenAI 兼容服务不认识它，
    乱发会招来 400。需要给自建服务用时把 `thinking` 设成 `enabled`/`disabled`
    并且确认服务端支持。
    """
    if config.thinking == "omit":
        return None
    if not is_deepseek(config.base_url) and config.thinking == "disabled":
        return None                     # 非 DeepSeek 默认不带，保持兼容
    return {"type": config.thinking}


def wants_json_output(config: ProviderConfig, structured: bool) -> bool:
    """这次请求要不要带 `response_format`。

    - `off`：永远不带；
    - `on`：一定带（调用方明确要求结构化）；
    - `auto`（默认）：**只在调用方声明要结构化输出时才带**。
      这一点很关键：把 auto 理解成"每次都要 JSON"，会让普通对话与连通性测试
      也被套上 JSON 约束，撞上 DeepSeek 的"提示词必须含 json"规则。
    """
    if config.json_mode == "off":
        return False
    if config.json_mode == "on":
        return True
    return bool(structured)


def build_payload(
    config: ProviderConfig,
    *,
    system: str,
    user: str,
    json_schema: dict | None = None,
    structured: bool = False,
    stream: bool = False,
) -> dict:
    """拼请求体。开结构化输出时**顺手保证提示词里出现 json**。"""
    json_output = wants_json_output(config, structured)
    if json_output:
        system, user = ensure_json_instruction(system, user)

    payload: dict = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    # 思考模式开启时不要带采样参数：DeepSeek 的思考模式不支持 temperature 等，
    # 带了也不会生效，还可能被某些兼容服务直接判 400。
    if config.thinking == "enabled":
        payload.pop("temperature", None)
    if json_output:
        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
    thinking = thinking_payload(config)
    if thinking is not None:
        payload["thinking"] = thinking
        # 思考强度只对声明支持的服务端发（目前是 DeepSeek 官方地址）
        if config.thinking == "enabled" and config.reasoning_effort and is_deepseek(
            config.base_url
        ):
            payload["reasoning_effort"] = config.reasoning_effort
    if stream:
        payload["stream"] = True
    return payload


@dataclass
class LLMStreamEvent:
    """流式响应里的一个片段。

    - ``kind="reasoning"``：思考过程（DeepSeek 的 ``delta.reasoning_content``），
      **过程性内容，不是答案，也不作为证据**；
    - ``kind="content"``：正式回答的增量；
    - ``kind="done"``：本次流结束（带 finish_reason / usage / 总耗时）。
    """

    kind: str
    text: str = ""
    model: str = ""
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    elapsed_s: float = 0.0


class LLMProvider(Protocol):
    """provider 协议。实现只需要管"发一次请求、把结果翻译成统一结构"。"""

    name: str

    def chat(
        self,
        config: ProviderConfig,
        *,
        api_key: str,
        system: str,
        user: str,
        json_schema: dict | None = None,
        structured: bool = False,
    ) -> LLMResponse: ...

    def probe(self, config: ProviderConfig, *, api_key: str) -> ProbeResult: ...

    def stream(
        self,
        config: ProviderConfig,
        *,
        api_key: str,
        system: str,
        user: str,
        json_schema: dict | None = None,
        structured: bool = False,
    ) -> Iterator[LLMStreamEvent]: ...


class OpenAICompatibleProvider:
    """/chat/completions 兼容实现。DeepSeek、Qwen、Moonshot、vLLM、Ollama 都是这一类。"""

    name = "openai-compatible"

    def chat(
        self,
        config: ProviderConfig,
        *,
        api_key: str,
        system: str,
        user: str,
        json_schema: dict | None = None,
        structured: bool = False,
    ) -> LLMResponse:
        if not api_key:
            raise LLMUnavailable("未提供 API Key")

        payload = build_payload(
            config, system=system, user=user,
            json_schema=json_schema, structured=structured,
        )

        started = time.monotonic()
        try:
            response = requests.post(
                chat_endpoint(config.base_url),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=config.timeout,
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError(
                f"请求超时（{config.timeout:g}s 内没有返回）。"
                "可以调大 LLM_TIMEOUT，或改用更快的模型；超时不会重试以免等待翻倍。"
                f"底层信息：{redact(str(exc), (api_key,))}"
            ) from exc
        except requests.RequestException as exc:
            raise LLMConnectionError(
                f"无法连接模型服务：{redact(str(exc), (api_key,))}"
            ) from exc
        elapsed = round(time.monotonic() - started, 2)

        body = self._raise_for_status(response, api_key)
        try:
            choice = body["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponseError(
                f"模型返回结构不符合 OpenAI 兼容格式：{redact(str(body)[:200], (api_key,))}"
            ) from exc

        finish_reason = str(choice.get("finish_reason") or "")
        reasoning = str(message.get("reasoning_content") or "")

        if not text.strip():
            # 正文为空有三种完全不同的原因，不能让用户看到同一句话。
            usage = dict(body.get("usage") or {})
            if reasoning.strip():
                raise LLMBadResponseError(
                    "模型只返回了思考过程（reasoning_content），没有返回正文内容"
                    f"（思考 {len(reasoning)} 字符，finish_reason={finish_reason or '未知'}）。"
                    "本次请求已显式关闭思考模式仍未拿到正文，可能是该模型只支持思考输出；"
                    "请换用非思考模型，或把 max_tokens 调大后重试。"
                    "思考内容不作为回答，也不纳入证据校验。"
                )
            if finish_reason == "length":
                raise LLMBadResponseError(
                    f"生成被截断（finish_reason=length，max_tokens={config.max_tokens} 用尽），"
                    "正文还没开始写就停了。请把 max_tokens 调大或缩短输入。"
                )
            if finish_reason == "content_filter":
                raise LLMBadResponseError(
                    "请求被内容策略拦截（finish_reason=content_filter）。"
                )
            raise LLMBadResponseError(
                "模型返回了空内容："
                f"content 为空（finish_reason={finish_reason or '未知'}，用量 {usage or '无'}）"
            )

        return LLMResponse(
            text=text,
            model=str(body.get("model") or config.model),
            elapsed_s=elapsed,
            used_json_mode="response_format" in payload,
            usage=dict(body.get("usage") or {}),
            finish_reason=finish_reason,
            has_reasoning=bool(reasoning.strip()),
            reasoning_len=len(reasoning),
        )

    @staticmethod
    def _raise_for_status(response, api_key: str) -> dict:
        """把 HTTP 状态码翻译成明确的错误类型，消息保留服务端原文（已脱敏）。"""
        detail = redact(response.text[:400], (api_key,))
        if response.status_code in {401, 403}:
            raise LLMAuthError(
                f"鉴权失败（HTTP {response.status_code}）：密钥无效、已过期或无权访问该模型。"
                f"服务端返回：{detail}"
            )
        if response.status_code == 429:
            raise LLMRateLimitError(
                f"请求被限流（HTTP 429）：{detail}"
            )
        if response.status_code >= 500:
            raise LLMError(f"模型服务异常（HTTP {response.status_code}）：{detail}")
        if response.status_code >= 400:
            raise LLMError(f"请求被拒绝（HTTP {response.status_code}）：{detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMBadResponseError(f"返回不是合法 JSON：{detail}") from exc

    def probe(self, config: ProviderConfig, *, api_key: str) -> ProbeResult:
        """连通性测试：一次最小真实调用，**不启用 JSON 输出模式**。

        为什么必须关掉：DeepSeek 在 `response_format=json_object` 时要求提示词里
        出现 json 字样，而连通性测试只需要模型说一句人话。开着 JSON 模式会招来
        HTTP 400 —— 那既不是鉴权问题，也说明不了密钥好坏，只会让用户以为 Key 错了。
        """
        started = time.monotonic()
        response = self.chat(
            # 连通性测试：关思考、关 JSON、给足最小输出预算（32 会被思考吃光）
            config.replace(json_mode="off", thinking="disabled",
                           max_tokens=64, temperature=0.0),
            api_key=api_key,
            system="你是一个连通性探针，只回答一句简短的话。",
            user="请回答：连接成功",
            structured=False,
        )
        return ProbeResult(
            ok=True,
            provider=self.name,
            model=response.model,
            elapsed_s=round(time.monotonic() - started, 2),
            detail=(response.text or "").strip()[:60],
            supports_json=response.used_json_mode,
            usage=response.usage,
        )

    def stream(
        self,
        config: ProviderConfig,
        *,
        api_key: str,
        system: str,
        user: str,
        json_schema: dict | None = None,
        structured: bool = False,
    ) -> Iterator[LLMStreamEvent]:
        """流式补全（SSE）。思考与正文分开产出，逐段 yield。

        出错时抛的还是那几个明确类型；**已经收到的那部分内容由调用方保管**，
        这里不吞、也不假装成功。
        """
        if not api_key:
            raise LLMUnavailable("未提供 API Key")
        payload = build_payload(
            config, system=system, user=user,
            json_schema=json_schema, structured=structured, stream=True,
        )
        started = time.monotonic()
        try:
            response = requests.post(
                chat_endpoint(config.base_url),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                timeout=config.timeout,
                stream=True,
            )
        except requests.Timeout as exc:
            raise LLMTimeoutError(
                f"连接模型超时（{config.timeout:g}s）：{redact(str(exc), (api_key,))}"
            ) from exc
        except requests.RequestException as exc:
            raise LLMConnectionError(
                f"无法连接模型服务：{redact(str(exc), (api_key,))}"
            ) from exc

        if response.status_code != 200:
            # 非 200 时服务端返回的是普通 JSON，直接复用非流式那套错误翻译
            self._raise_for_status(response, api_key)
            response.close()
            raise LLMError(f"模型服务返回 HTTP {response.status_code}")

        model = config.model
        finish_reason = ""
        usage: dict = {}
        try:
            for raw in response.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise LLMBadResponseError(
                        f"流式响应不是合法 JSON：{redact(data[:160], (api_key,))}"
                    ) from exc
                model = str(chunk.get("model") or model)
                if chunk.get("usage"):
                    usage = dict(chunk["usage"])
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield LLMStreamEvent(kind="reasoning", text=str(reasoning), model=model)
                content = delta.get("content")
                if content:
                    yield LLMStreamEvent(kind="content", text=str(content), model=model)
        except requests.RequestException as exc:   # 流到一半断了
            raise LLMConnectionError(
                f"流式连接中断：{redact(str(exc), (api_key,))}"
            ) from exc
        finally:
            response.close()

        yield LLMStreamEvent(
            kind="done", model=model, finish_reason=finish_reason, usage=usage,
            elapsed_s=round(time.monotonic() - started, 2),
        )


class ScriptedProvider:
    """只用于测试：按脚本返回预设结果，**绝不联网**。

    生产代码不会注册它（见 PROVIDERS）。
    """

    name = "scripted"

    def __init__(
        self,
        responses: list[dict] | None = None,
        streams: list[list[dict]] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        # 每个元素是一次流式调用的脚本：[{"kind":"reasoning","text":"…"}, …]
        self.streams = list(streams or [])
        self.calls: list[dict] = []

    def chat(self, config, *, api_key, system, user, json_schema=None,  # noqa: ANN001
             structured: bool = False):
        self.calls.append({"model": config.model, "system": system, "user": user,
                           "api_key_len": len(api_key or ""),
                           "structured": structured,
                           "json_mode": config.json_mode})
        if not self.responses:
            return LLMResponse(text="OK", model=config.model, elapsed_s=0.01)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            text=item.get("text", ""),
            model=item.get("model", config.model),
            elapsed_s=item.get("elapsed_s", 0.01),
            used_json_mode=bool(item.get("used_json_mode", False)),
            usage=dict(item.get("usage") or {}),
        )

    def probe(self, config, *, api_key):  # noqa: ANN001
        response = self.chat(config, api_key=api_key, system="probe", user="probe")
        return ProbeResult(
            ok=True, provider=self.name, model=response.model,
            elapsed_s=response.elapsed_s, detail=response.text[:60],
        )

    def stream(self, config, *, api_key, system, user, json_schema=None,  # noqa: ANN001
               structured: bool = False):
        """按脚本产出流式事件（思考 / 正文 / 结束），**绝不联网**。"""
        self.calls.append({"model": config.model, "system": system, "user": user,
                           "api_key_len": len(api_key or ""), "structured": structured,
                           "stream": True, "json_mode": config.json_mode,
                           "thinking": config.thinking,
                           "reasoning_effort": config.reasoning_effort})
        if self.streams:
            script = self.streams.pop(0)
        elif self.responses:
            # 只脚本了非流式返回时，把它当成"一次性到达的正文"，
            # 这样老的离线用例不必重写成流式脚本也能覆盖同一批校验逻辑。
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            script = [{"kind": "content", "text": item.get("text", ""),
                       "model": item.get("model"), "usage": item.get("usage"),
                       "finish_reason": item.get("finish_reason")},
                      {"kind": "done", "model": item.get("model")}]
        else:
            script = [{"kind": "content", "text": "OK"}]
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield LLMStreamEvent(
                kind=str(item.get("kind") or "content"),
                text=str(item.get("text") or ""),
                model=str(item.get("model") or config.model),
                finish_reason=str(item.get("finish_reason") or ""),
                usage=dict(item.get("usage") or {}),
                elapsed_s=float(item.get("elapsed_s") or 0.01),
            )


# 生产注册表：只有真实 provider。测试自己实例化 ScriptedProvider。
PROVIDERS: dict[str, LLMProvider] = {
    DEFAULT_PROVIDER: OpenAICompatibleProvider(),
    "openai-compatible": OpenAICompatibleProvider(),
}


def get_provider(name: str) -> LLMProvider:
    key = (name or DEFAULT_PROVIDER).strip().lower()
    provider = PROVIDERS.get(key)
    if provider is None:
        raise LLMConfigError(
            f"未知的模型服务商：{name!r}（可用：{sorted(PROVIDERS)}）"
        )
    return provider

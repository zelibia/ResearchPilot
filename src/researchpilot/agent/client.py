"""统一模型客户端：把会话凭据、provider 与重试策略串起来。

每次调用都从**会话凭据**现取密钥，不在客户端实例上缓存；
错误原样上抛（鉴权、限流、连接、返回不合法分开），界面照实显示。
"""

from __future__ import annotations

import json
import logging
import time

from . import providers as pv
from . import session as session_mod
from .secrets import redact

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(
        self,
        store: session_mod.CredentialStore | None = None,
        provider: pv.LLMProvider | None = None,
        name: str = "llm",
    ) -> None:
        self.store = store or session_mod.store()
        self._provider = provider
        self.name = name

    # ------------------------------------------------------------ 状态

    @property
    def available(self) -> bool:
        return self.store.current() is not None

    def describe(self) -> str:
        return self.store.status()["label"]

    def status(self) -> dict:
        return self.store.status()

    def _provider_for(self, credentials: session_mod.Credentials) -> pv.LLMProvider:
        return self._provider or pv.get_provider(credentials.provider)

    # ------------------------------------------------------------ 调用

    def test_connection(self, *, timeout: float | None = None) -> pv.ProbeResult:
        """真实连通性测试：会发一次最小请求。失败抛真实错误。"""
        credentials = self.store.require()
        config = credentials.config(**({"timeout": timeout} if timeout else {}))
        provider = self._provider_for(credentials)
        result = provider.probe(config, api_key=credentials.api_key)
        self.store.record_probe(result)
        return result

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict | None = None,
        structured: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        thinking: str | None = None,
        model: str | None = None,
        retries: int = 1,
    ) -> pv.LLMResponse:
        """一次补全。

        `structured` 决定是否要求 JSON 输出：默认"给了 schema 才算要"。
        连通性测试与普通对话不要传它——它们需要的是模型说人话。
        """
        credentials = self.store.require()
        provider = self._provider_for(credentials)
        wants_json = bool(json_schema) if structured is None else bool(structured)
        overrides: dict = {}
        if temperature is not None:
            overrides["temperature"] = temperature
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens
        if timeout is not None:
            overrides["timeout"] = timeout
        if model:
            overrides["model"] = model

        json_mode = credentials.config().json_mode
        thinking_value = thinking or credentials.config().thinking
        attempts = 0
        last_error: Exception | None = None
        while attempts <= retries:
            attempts += 1
            config = credentials.config(
                json_mode=json_mode, thinking=thinking_value, **overrides
            )
            try:
                return provider.chat(
                    config, api_key=credentials.api_key,
                    system=system, user=user, json_schema=json_schema,
                    structured=wants_json,
                )
            except pv.LLMAuthError:
                raise                      # 密钥问题重试没有意义
            except pv.LLMUnavailable:
                raise
            except pv.LLMTimeoutError:
                raise                      # 超时重试只会让等待时间翻倍
            except pv.LLMError as exc:
                last_error = exc
                message = redact(str(exc), (credentials.api_key,))
                # 模型不支持 response_format：退回普通文本，只退一次
                if json_mode != "off" and ("response_format" in message or "HTTP 400" in message):
                    log.warning("模型不支持 JSON 结构约束，退回普通输出")
                    json_mode = "off"
                    attempts -= 1
                    continue
                # 个别服务不认识 thinking 字段：去掉它再试一次
                if thinking_value != "omit" and "thinking" in message.lower():
                    log.warning("服务端不接受 thinking 参数，去掉后重试")
                    thinking_value = "omit"
                    attempts -= 1
                    continue
                if attempts > retries:
                    raise
                log.warning("模型调用失败，准备重试（第 %d 次）：%s", attempts, message)
                time.sleep(min(2.0 * attempts, 5.0))
        raise last_error or pv.LLMError("模型调用失败")

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict | None = None,
        **kwargs,
    ) -> tuple[dict, pv.LLMResponse]:
        """要求结构化输出并解析。解析失败**不返回半成品**。"""
        response = self.complete(
            system, user, json_schema=json_schema, structured=True, **kwargs
        )
        raw = extract_json(response.text)
        if raw is None:
            raise pv.LLMBadResponseError(
                "模型输出不是合法 JSON。原始输出前 300 字："
                f"{response.text[:300]}"
            )
        return raw, response

    def stream(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict | None = None,
        structured: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ):
        """流式调用：逐段产出 ``LLMStreamEvent``（思考与正文分开）。

        流式**不重试**：已经吐出来的内容不能重放，重试只会得到重复文本。
        失败时抛的还是那几个明确类型，调用方自己保管已经收到的部分。
        """
        credentials = self.store.require()
        provider = self._provider_for(credentials)
        wants_json = bool(json_schema) if structured is None else bool(structured)
        overrides: dict = {}
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens
        if temperature is not None and credentials.config().thinking != "enabled":
            # 思考模式下不带采样参数（DeepSeek 的思考模式不支持它们）
            overrides["temperature"] = temperature
        config = credentials.config(**overrides)
        return provider.stream(
            config, api_key=credentials.api_key, system=system, user=user,
            json_schema=json_schema, structured=wants_json,
        )


def extract_json(text: str) -> dict | None:
    """从模型输出里取 JSON。容忍 ```json 围栏与前后解释文字。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw[:4].lower() == "json":
                raw = raw[4:]
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # 退一步：截取第一个 { 到最后一个 } 之间
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None

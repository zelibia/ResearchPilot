"""侧栏「用户设置 → AI 模型」：BYOK 连接界面。

界面约定（遵守《界面规范 v1.0》）：

- 只显示服务商、模型、连接状态与**密钥掩码**，任何地方都不显示完整密钥；
- 失败时显示服务端返回的真实原因，不写"AI 不可用"这种笼统话；
- 不连接模型不影响其他功能，界面上如实说明。

密钥只进 `st.session_state`（服务端、本会话内存），不落库、不落盘、不入日志。

**控件命名空间**：同一个会话里，侧栏设置面板与正文里的"需要连接"提示可能同时
出现（用户一边打开设置、一边点开一篇文献）。两处表单用不同的 `namespace`
前缀各自的 key，避免 Streamlit 抛 DuplicateElementKey；凭据与连接状态仍然是
同一份会话状态，不因实例不同而分叉。
"""

from __future__ import annotations

import streamlit as st

from ..agent import providers as pv
from ..agent import session as session_mod
from ..agent.client import LLMClient
from ..agent.secrets import mask_key

CUSTOM_MODEL = "自定义…"
SETTINGS_NAMESPACE = "settings"      # 侧栏「用户设置」里的那一份
INLINE_NAMESPACE = "inline"          # 正文里"需要连接"时就地展开的那一份

_PRIVACY_NOTE = (
    "分析时会把文献正文节选发送到你选择的模型服务；"
    "密钥只保存在本会话的服务端内存中，不写入数据库或日志。"
)


def _key(namespace: str, name: str) -> str:
    """控件 key 一律带命名空间前缀，防止两处表单撞车。"""
    return f"llm_{namespace}_{name}"


def _message_key(namespace: str) -> str:
    return f"_llm_message_{namespace}"


def _connect(namespace: str, **kwargs) -> None:
    """连接并立刻做一次真实连通性测试；结果或错误都如实显示。"""
    store = session_mod.store()
    message_key = _message_key(namespace)
    try:
        credentials = store.connect(**kwargs)
    except pv.LLMConfigError as exc:
        st.session_state[message_key] = ("error", str(exc))
        return

    client = LLMClient(store)
    try:
        result = client.test_connection()
    except pv.LLMAuthError as exc:
        # 只有真正的鉴权失败才清除密钥——它确定是密钥的问题
        store.disconnect()
        st.session_state[message_key] = ("error", str(exc))
        return
    except pv.LLMError as exc:
        # 超时、限流、格式错误等**不是密钥失效**：保留凭据，让用户改完直接重试
        st.session_state[message_key] = (
            "error",
            f"{exc}\n\n已保留本会话的密钥，修好上述原因后可直接点「重新测试」。",
        )
        return
    st.session_state[message_key] = (
        "ok",
        f"已连接 {credentials.base_url} · {result.model}"
        f"（连通性测试 {result.elapsed_s}s，返回「{result.detail}」）",
    )


def _disconnect(namespace: str) -> None:
    session_mod.store().disconnect()
    st.session_state[_message_key(namespace)] = ("info", "已断开并清除本会话中的密钥")


def _retest(namespace: str) -> None:
    """用**当前会话已保存的凭据**重测，不需要重新输入密钥。"""
    store = session_mod.store()
    message_key = _message_key(namespace)
    try:
        result = LLMClient(store).test_connection()
    except pv.LLMAuthError as exc:
        store.disconnect()
        st.session_state[message_key] = ("error", str(exc))
    except pv.LLMError as exc:
        st.session_state[message_key] = (
            "error", f"{exc}\n\n已保留本会话的密钥，修好上述原因后可直接再测。"
        )
    else:
        st.session_state[message_key] = (
            "ok", f"连接正常：{result.model}　{result.elapsed_s}s　返回「{result.detail}」"
        )


def render(namespace: str = SETTINGS_NAMESPACE) -> None:
    """渲染连接面板。`namespace` 决定控件 key 前缀，两处实例互不干扰。"""
    store = session_mod.store()
    status = store.status()

    message = st.session_state.pop(_message_key(namespace), None)
    if message:
        kind, text = message
        {"ok": st.success, "error": st.error, "info": st.info}[kind](text)

    if status["connected"]:
        st.markdown(f"**已连接**　{status['label']}")
        st.caption(f"密钥：{status['key']}　·　地址：{status['base_url']}")
    else:
        st.markdown("**未连接**")

    with st.expander("连接 / 更换模型", expanded=not status["connected"]):
        provider = st.selectbox(
            "服务商", list(pv.KNOWN_HOSTS.values()) or ["DeepSeek"],
            key=_key(namespace, "provider"),
            help="第一版支持 DeepSeek（OpenAI 兼容接口）",
        )
        base_url = st.text_input(
            "服务地址", value=status["base_url"], key=_key(namespace, "base_url"),
            help="默认 https://api.deepseek.com",
        )
        options = [*pv.DEEPSEEK_MODELS, CUSTOM_MODEL]
        default_index = (
            options.index(status["model"]) if status["model"] in options else 0
        )
        choice = st.selectbox("模型", options, index=default_index,
                              key=_key(namespace, "model"))
        model = choice
        if choice == CUSTOM_MODEL:
            model = st.text_input("模型名称", key=_key(namespace, "model_custom"),
                                  placeholder="填写服务商文档里的模型标识")
        api_key = st.text_input(
            "API Key", type="password", key=_key(namespace, "api_key"),
            placeholder="仅保存在本会话内存中", help=_PRIVACY_NOTE,
        )
        if api_key:
            # 只显示首尾各 4 位：用户一眼就能看出自己粘进来的是不是密钥
            # （服务端 401 里回显的也是同样的后 4 位）
            st.caption(f"将使用：{mask_key(api_key)}　·　长度 {len(api_key)}")
            problem = session_mod.key_shape_problem(api_key, base_url=base_url)
            if problem:
                st.warning(problem)
        allow_custom = st.checkbox(
            "我确认使用自建或第三方地址", key=_key(namespace, "allow_custom"),
            help="默认只允许已验证的服务商地址；自建服务（如本机 Ollama）需自行确认",
        )
        st.caption(_PRIVACY_NOTE)

        columns = st.columns([1, 1])
        if columns[0].button("连接并测试", type="primary",
                             key=_key(namespace, "connect_btn")):
            _connect(
                namespace,
                provider=_provider_key(provider),
                base_url=base_url,
                model=model,
                api_key=api_key,
                allow_custom_url=bool(allow_custom),
            )
            st.rerun()
        if status["connected"] and columns[1].button(
            "断开", key=_key(namespace, "disconnect_btn")
        ):
            _disconnect(namespace)
            st.rerun()

        if status["connected"]:
            if st.button("重新测试连接", key=_key(namespace, "retest_btn")):
                _retest(namespace)
                st.rerun()

    probe = store.last_probe()
    if probe:
        st.caption(
            f"最近一次连通性测试：{probe['model']}　{probe['elapsed_s']}s　"
            f"返回「{probe['detail']}」"
        )

    _thinking_block(store, namespace)


EFFORT_LABELS = {"": "服务端默认", "low": "低", "medium": "中", "high": "高"}


def _thinking_block(store, namespace: str) -> None:
    """深度思考设置。**只对声明支持的服务地址显示**，其余情况说明原因。

    DeepSeek 的思考模式会在正式回答之前先产出 reasoning_content；它不是答案，
    也不是证据——界面会把两者分开显示（见阅读导览的「查看 AI 思考过程」）。
    """
    if store.current() is None:
        return
    if not store.supports_thinking():
        st.caption(
            "深度思考：当前服务地址不支持这项参数（只有 DeepSeek 官方地址支持），"
            "因此这里不显示该设置。"
        )
        return

    credentials = store.current()
    enabled = st.checkbox(
        "深度思考", value=credentials.thinking == "enabled",
        key=_key(namespace, "thinking"),
        help="开启后模型会先推理再回答；思考内容会单独展示，不作为原文证据。",
    )
    effort = credentials.reasoning_effort
    if enabled:
        options = ["", "low", "medium", "high"]
        index = options.index(effort) if effort in options else 2
        effort = st.selectbox(
            "思考强度", options, index=index,
            format_func=lambda item: EFFORT_LABELS.get(item, item),
            key=_key(namespace, "reasoning_effort"),
            help="由服务端解释；不同模型支持的程度可能不同。",
        )
    if (enabled, effort) != (credentials.thinking == "enabled", credentials.reasoning_effort):
        store.set_thinking(enabled, effort)
        st.caption("已更新到本会话（不需要重新连接）。")


def _provider_key(label: str) -> str:
    """把展示名映射回 provider 注册表里的名字。"""
    for key, name in pv.KNOWN_HOSTS.items():
        if name == label:
            return pv.DEFAULT_PROVIDER
    return pv.DEFAULT_PROVIDER


def connected() -> bool:
    """当前会话是否已连上模型（含服务端 `.env` 兜底）。"""
    return session_mod.store().current() is not None


def client() -> LLMClient:
    """按当前会话凭据建一个模型客户端。没连接时调用方自己会失败得很清楚。"""
    return LLMClient()


def session_credentials():
    """当前生效的凭据（含超时等非敏感配置）。没有则返回 None。"""
    return session_mod.store().current()


def require_connection(purpose: str) -> bool:
    """AI 功能的统一入口：没连接就把连接界面显示出来，并返回 False。

    调用方在返回 False 时**不要**产出任何 AI 内容——不连接就没有 AI 结果，
    这是 C3 的底线。
    """
    if session_mod.store().current() is not None:
        return True
    st.info(f"{purpose}需要先连接 AI 模型。")
    # 正文里的这一份用独立命名空间，避免和侧栏设置面板撞 key；
    # 两处读写的是同一个会话状态，连接后两边都会显示已连接。
    with st.expander("在这里连接 AI 模型", expanded=True):
        render(namespace=INLINE_NAMESPACE)
    return False


def status_line() -> str:
    """顶栏用的单行模型状态。全应用只应在这里报一次连接状态。"""
    status = session_mod.store().status()
    if not status.get("connected"):
        return "AI 模型：未连接（左下角「用户设置」里连接）"
    where = "服务端配置" if status.get("source") == "env" else "本会话"
    return f"AI 模型：{status.get('label', '已连接')}（{where}）"

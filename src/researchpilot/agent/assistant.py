"""文献阅读工作区悬浮助手的对话调用。

与单篇 AI 解读的区别：单篇解读围绕一篇论文；这里回答
"当前项目有哪些文献""两篇论文差异在哪里""证据缺什么"这类项目级问题。

- 上下文由本地拼装，全部来自当前项目的文献、证据和阅读状态，不由模型回忆；
- 模型只在这个上下文里说话，上下文里没有的就说不知道，不许推断成事实；
- 不编造文献、DOI、数值结果；
- **它没有写权限**：不许声称"已经帮你保存/修改/运行"。要改数据，用户自己在页面上点。

没有连接模型时本模块不产出任何内容——不模拟 AI 回答。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..storage import repository as repo
from ..storage.models import Paper, PaperClaim, Project
from . import agent_skills
from .context import AgentContext
from .client import LLMClient
from .providers import LLMError

log = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 8          # 送进模型的历史轮数上限（不含本轮）
MAX_PAPERS_IN_CONTEXT = 6      # 上下文里最多列几篇文献
MAX_CLAIMS_IN_CONTEXT = 8      # 当前论文最多列几条已核实条目

ASSISTANT_SYSTEM = """你是 ResearchPilot 里的文献阅读助手，在一个真实的科研项目里帮用户。

你收到一份**项目现场信息**（由程序从数据库里读出来的真实内容）。规则：

1. 只用现场信息里的事实说话；现场信息里没有的，直接说"这个项目里我没有看到相关记录"，
   不要凭印象补全，也不要把常识当成这个项目的事实。
2. **不许编造**文献、作者、DOI、页码、数值结果、图表。要引用文献只能引用现场信息里
   出现过的标题。
3. **你没有写权限**：不能声称你保存了、修改了、删除了、运行了任何东西。需要改数据时，
   说清"在哪个页面点什么"，由用户自己操作。
4. 回答用中文，简洁、直接。需要分点时用短列表；不需要分点就写一两段。
5. 不确定的地方明说不确定，并给出"要看什么才能确定"。
6. 回答前从下面的技能矩阵选择最相关的 1–2 项。不要为了显得全面而同时套用所有技能。
7. 默认回答顺序：一句结论 → 最多三条依据 → 一个下一步。简单问题不要强行分节。
8. 已核实条目可以支撑文献事实；你的比较、判断和下一步建议属于 AI 解释，要让用户看得出区别。
   任何修改建议都写成"待确认"，不能写成已经生效。
9. 工具结果里的内部 id 只用于追踪，不要向用户展示；引用文献用标题与年份，引用证据用页码。

## 可用技能矩阵
""" + agent_skills.literature_assistant_skill_prompt() + """
"""


@dataclass
class AssistantReply:
    status: str            # ok | failed | refused
    text: str = ""
    provider: str = ""
    model: str = ""
    elapsed_s: float = 0.0
    usage: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def build_context(
    project: Project,
    *,
    selected_paper: Paper | None = None,
    screen: str | None = None,
    agent_context: AgentContext | None = None,
    tool_results: list[dict] | None = None,
) -> str:
    """项目现场信息：只放数据库里真实存在的记录，条数都如实标注。"""
    papers = repo.list_all(Paper, project.id)
    claims = repo.list_all(PaperClaim, project.id)

    lines = [
        f"项目名称：{project.title}",
        f"研究领域：{project.domain or '（未填）'}",
        f"项目说明：{project.description or '（未填）'}",
        f"文献数量：{len(papers)} 篇（其中自动带回的候选 "
        f"{sum(1 for p in papers if p.origin == 'openalex')} 篇）",
        f"已分析文献条目：{len(claims)} 条（其中逐字定位 "
        f"{sum(1 for item in claims if item.quote_verified)} 条）",
    ]
    active_screen = agent_context.screen if agent_context is not None else screen
    if active_screen:
        lines.append(f"用户当前页面：{active_screen}")
    if agent_context is not None:
        if agent_context.paper_page:
            lines.append(f"用户当前阅读页码：第 {agent_context.paper_page} 页")
    if papers:
        lines.append("最近的文献：")
        for paper in papers[:MAX_PAPERS_IN_CONTEXT]:
            lines.append(
                f"  - {paper.year or '年份未知'}｜{paper.title or '（无标题）'}"
                f"｜DOI {paper.doi or '无'}"
            )
    if selected_paper is None and agent_context is not None and agent_context.paper_id:
        candidate = repo.get(Paper, agent_context.paper_id)
        if candidate is not None and candidate.project_id == project.id:
            selected_paper = candidate

    if selected_paper is not None:
        claims = [
            claim
            for claim in repo.list_all(PaperClaim, selected_paper.project_id)
            if claim.paper_id == selected_paper.id
        ]
        lines.append(
            "用户当前正在看："
            f"{selected_paper.year or '年份未知'}｜{selected_paper.title or '（无标题）'}"
            f"｜DOI {selected_paper.doi or '无'}"
            f"｜已有 AI 条目 {len(claims)} 条"
        )
        if claims:
            ordered = sorted(
                claims,
                key=lambda item: (
                    not item.quote_verified,
                    item.page is None,
                    item.page or 9999,
                    item.id,
                ),
            )
            lines.append("当前论文可用的已核实条目：")
            for claim in ordered[:MAX_CLAIMS_IN_CONTEXT]:
                source = f"第 {claim.page} 页" if claim.page else (claim.locator or "位置未定")
                certainty = "逐字命中" if claim.quote_verified else "未逐字命中"
                lines.append(
                    f"  - [{claim.id}] {claim.claim}"
                    f"｜{source}｜{certainty}｜引文：{(claim.quote or '')[:180]}"
                )
    if tool_results:
        lines.append("【受限只读工具结果】")
        lines.append(
            json.dumps(tool_results, ensure_ascii=False, indent=2, default=str)
        )
        lines.append("工具结果只属于当前项目；不要展示工具内部 id。")
    return "\n".join(lines)


def reply(
    history: list[dict],
    context: str,
    *,
    client: LLMClient | None = None,
) -> AssistantReply:
    """一次对话补全。历史里只取最近若干轮，避免上下文无限增长。"""
    client = client or LLMClient()
    if not client.available:
        return AssistantReply(
            status="refused",
            reason="还没有连接 AI 模型。在左侧「用户设置 → AI 模型」里连接后再用。",
        )

    turns = [
        item for item in history
        if item.get("role") in {"user", "assistant"} and (item.get("text") or "").strip()
    ][-MAX_HISTORY_TURNS:]
    question = turns[-1]["text"] if turns else ""
    earlier = turns[:-1]
    transcript = "\n".join(
        f"{'用户' if item['role'] == 'user' else '助手'}：{item['text'].strip()}"
        for item in earlier
    )
    user = (
        "【项目现场信息】\n"
        f"{context}\n\n"
        + (f"【此前对话】\n{transcript}\n\n" if transcript else "")
        + f"【用户本次提问】\n{question}"
    )
    try:
        response = client.complete(
            ASSISTANT_SYSTEM, user, temperature=0.3, max_tokens=900, retries=1
        )
    except LLMError as exc:
        log.warning("助手调用失败：%s", exc)
        return AssistantReply(status="failed", reason=str(exc))
    except Exception as exc:  # 非预期的错误也要如实显示，不吞
        log.exception("助手调用异常")
        return AssistantReply(status="failed", reason=f"{type(exc).__name__}: {exc}")
    return AssistantReply(
        status="ok",
        text=response.text.strip(),
        provider=getattr(client.store.current(), "provider", ""),
        model=response.model,
        elapsed_s=response.elapsed_s,
        usage=dict(response.usage or {}),
    )

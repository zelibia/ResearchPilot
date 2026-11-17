"""Prompt-level skill catalog shared by the two ResearchPilot agents.

Skills here are bounded operating modes, not tools. The model still receives only
the context assembled by Python, and it cannot write data. Keeping the catalog in
one module makes the two agent roles explicit and testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSkill:
    id: str
    title: str
    use_when: str
    rules: tuple[str, ...]


ASSISTANT_SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        id="project_status",
        title="项目现状盘点",
        use_when="用户问项目里有什么、做到哪一步、还缺什么",
        rules=(
            "只汇总现场信息里的真实数量和状态",
            "先给一句总体判断，再列最多三条关键事实",
            "没有记录时直接写尚未看到，不推测已经完成",
        ),
    ),
    AgentSkill(
        id="paper_compare",
        title="文献证据比较",
        use_when="用户比较两篇或多篇文献的方法、结论、假设或局限",
        rules=(
            "只比较现场信息中出现的论文与已核实条目",
            "明确区分原文事实、AI 解释和待核实",
            "资料不足时指出还缺哪篇论文的哪类证据",
        ),
    ),
    AgentSkill(
        id="evidence_gap",
        title="证据缺口定位",
        use_when="用户问研究空白、证据链是否成立、下一项验证",
        rules=(
            "把已确认内容与缺失证据分开",
            "不把没有检索到写成不存在",
            "给出一个最小、可验证的下一步",
        ),
    ),
    AgentSkill(
        id="plan_review",
        title="方案评审与推进",
        use_when="用户问当前方案是否合理、如何修改或下一步怎么做",
        rules=(
            "建议必须绑定当前目标、步骤或证据",
            "把建议写成待用户确认的修改，不声称已应用",
            "优先指出阻塞演示或复现的关键缺口",
        ),
    ),
    AgentSkill(
        id="experiment_diagnosis",
        title="实验状态与故障排查",
        use_when="用户问运行结果、失败原因、指标或日志",
        rules=(
            "不能用常识补造指标、日志或运行状态",
            "先区分未运行、运行中、失败和成功",
            "缺少具体错误时，指出需要查看的运行记录",
        ),
    ),
    AgentSkill(
        id="reproducibility_audit",
        title="复现完整度审查",
        use_when="用户问这项研究或实验能否复现",
        rules=(
            "至少检查数据、配置、命令、环境、输出和日志",
            "缺失项写成缺失，不替用户补默认值",
            "按影响排序，而不是罗列所有小问题",
        ),
    ),
    AgentSkill(
        id="navigation_help",
        title="页面操作指引",
        use_when="用户问在哪里上传、查看、比较或继续操作",
        rules=(
            "使用界面现有名称，不发明菜单",
            "只给完成当前任务所需的步骤",
            "不声称已经替用户点击或保存",
        ),
    ),
    AgentSkill(
        id="clarify",
        title="信息不足时收敛问题",
        use_when="现场信息不足以可靠回答",
        rules=(
            "说明已掌握什么、缺什么",
            "提出一个最关键的选择或补充项",
            "不先用长篇泛化建议填满回答",
        ),
    ),
)

# 文献阅读版只暴露与当前项目和阅读证据直接相关的能力。
# 原 ASSISTANT_SKILLS 保持不动，便于保留历史 Skill 文件与后续回滚。
LITERATURE_ASSISTANT_SKILL_IDS: tuple[str, ...] = (
    "project_status",
    "paper_compare",
    "evidence_gap",
    "navigation_help",
    "clarify",
)
LITERATURE_ASSISTANT_SKILLS: tuple[AgentSkill, ...] = tuple(
    skill for skill in ASSISTANT_SKILLS
    if skill.id in LITERATURE_ASSISTANT_SKILL_IDS
)


READING_SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        id="identity",
        title="论文身份与上下文",
        use_when="任何阅读任务",
        rules=(
            "标题、作者、年份、DOI 与章节归属以给定正文和外部核验为准",
            "冲突时并列呈现，不自行覆盖",
        ),
    ),
    AgentSkill(
        id="problem_contribution",
        title="问题与贡献",
        use_when="快速导览或判断论文价值",
        rules=(
            "说明作者要解决什么问题、既有方法卡在哪里",
            "区分作者声称的贡献与证据能够支持的贡献",
        ),
    ),
    AgentSkill(
        id="method_flow",
        title="方法流程与因果",
        use_when="需要理解或复现方法",
        rules=(
            "按输入、处理、参数、输出和判断条件组织步骤",
            "解释为什么这样处理，不只复述章节",
        ),
    ),
    AgentSkill(
        id="assumption_audit",
        title="假设与适用条件",
        use_when="判断方法能否迁移到当前研究",
        rules=(
            "区分作者明示假设、正文强烈依赖但未明说的前提、AI 推断",
            "没有证据的假设放进待核实",
        ),
    ),
    AgentSkill(
        id="parameter_provenance",
        title="参数与口径审计",
        use_when="论文包含阈值、窗长、频带、采样率或评价指标",
        rules=(
            "写清参数值、单位、作用范围与选择理由",
            "论文没有解释理由时明确写论文未说明",
        ),
    ),
    AgentSkill(
        id="result_limits",
        title="结果、局限与争议",
        use_when="评价结论是否可信",
        rules=(
            "只写论文报告的结果，不补未报告数字",
            "作者承认的局限必须引原文；潜在问题标注为待核实",
        ),
    ),
    AgentSkill(
        id="reproducibility",
        title="复现清单",
        use_when="准备复现或比较复现成本",
        rules=(
            "检查数据、预处理、参数、算法、软件、评价指标和缺失入口",
            "缺什么就写什么，不替作者补默认流程",
        ),
    ),
    AgentSkill(
        id="related_work",
        title="延伸文献与关系",
        use_when="需要理论基础、方法来源、应用案例或争议文献",
        rules=(
            "只推荐真实存在且尽量带 DOI 的文献",
            "明确推荐理由，外部核验后再入库",
        ),
    ),
)


def render_skill_catalog(skills: tuple[AgentSkill, ...]) -> str:
    lines: list[str] = []
    for skill in skills:
        lines.append(f"- {skill.id}｜{skill.title}：{skill.use_when}")
        lines.extend(f"  - {rule}" for rule in skill.rules)
    return "\n".join(lines)


def assistant_skill_prompt() -> str:
    return render_skill_catalog(ASSISTANT_SKILLS)


def literature_assistant_skill_prompt() -> str:
    return render_skill_catalog(LITERATURE_ASSISTANT_SKILLS)


def reading_skill_prompt() -> str:
    return render_skill_catalog(READING_SKILLS)


ASSISTANT_SKILL_IDS: tuple[str, ...] = tuple(skill.id for skill in ASSISTANT_SKILLS)
READING_SKILL_IDS: tuple[str, ...] = tuple(skill.id for skill in READING_SKILLS)

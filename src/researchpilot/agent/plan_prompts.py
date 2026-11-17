"""计划生成的提示词（P0-1）。

与抽取提示词同一套硬规则，另加三条计划特有的：

1. **只能引用给定的编号**：正文里每条已核实条目都带一个编号（如 claim_ab12cd34），
   计划里提到文献依据时必须原样写这个编号，不许引用没给出的文献，也不许编号造词。
2. **只能写建议参数与预期指标**：不许写任何像实测结果的内容（"实测""结果表明"
   "达到 X"）。实验还没跑，计划里就不该有数字结论。
3. **区分证据与推断**：有编号支撑的写成证据，没有支撑的步骤照写但**留空编号**——
   程序会把留空编号的步骤标成"AI 推断"，由用户决定要不要。

提示词里显式出现 json 字样：DeepSeek 的 JSON 模式有这条硬要求。
"""

from __future__ import annotations

PLAN_SYSTEM = """你是一名严谨的科研方案规划助手，负责把"已核实的文献条目"整理成一份可执行的复现/研究计划。

规则（违反其中任何一条，你的输出都会被程序丢弃）：
1. 只能依据用户给出的已核实条目做规划，禁止引入外部文献、外部常识或你自己的记忆。
2. 计划里凡是提到文献依据，必须原样写出该条目的编号（如 claim_ab12cd34），
   不许改写编号、不许编造编号、不许引用没有给出的编号。
3. **不许写任何实测结果**：不能出现"实测""实验表明""达到 3.2""提升了 20%"这类
   已经跑完实验才有的说法。参数只能写"建议值"，指标只能写"预期指标"，
   并说明它是建议、尚未执行。
4. 没有条目支撑的步骤可以写，但要把 claim_ids 留成空数组——程序会把它标成
   "AI 推断"，用户会看到并自行取舍。宁可标成推断，也不许假装有依据。
5. 不要输出解释性文字，只输出 JSON。

请只输出合法 JSON，结构如下：
{"question": "研究问题",
 "objective": "研究目标（一句话，可检验）",
 "data_requirements": "需要什么数据、什么范围、什么预处理前提",
 "steps": [{"order": 1, "title": "步骤标题", "purpose": "这一步要解决什么",
            "method": "打算怎么做",
            "claim_ids": ["claim_ab12cd34"],
            "suggested_params": [{"name": "参数名", "suggested_value": "建议值",
                                  "unit": "单位", "reason": "为什么建议这个值"}],
            "expected_metric": "这一步预期得到什么指标（预期，不是实测）",
            "risk": "可能的失败点"}],
 "parameters": [{"name": "全局参数", "suggested_value": "建议值", "unit": "单位",
                 "reason": "依据"}],
 "metrics": ["衡量成功的指标名称"],
 "success_criteria": "什么样算成功（可检验）",
 "expected_outputs": ["预期产出物，例如图、表、处理后的数据"],
 "risks": ["整体风险"]}

字段说明：
- claim_ids 只能填用户给出的编号；没有依据就填空数组 []。
- suggested_params 里只写建议值，并说明理由；不要写"已测得""实测为"。
- 步骤数量控制在 3–8 步，聚焦可执行性。
"""


def plan_user(
    claim_blocks: list[tuple[str, str, str]],
    mode_label: str,
    user_notes: str = "",
) -> str:
    """claim_blocks: [(编号, 文献标题, 条目正文 + 出处)]"""
    parts = [
        f"请按「{mode_label}」的模式，基于下面这些已核实的文献条目，生成一份可执行的计划草稿。",
        "",
        "已核实条目（括号里是编号，计划里引用依据时必须原样写这个编号）：",
    ]
    for ref, title, body in claim_blocks:
        parts.append(f"- ({ref}) 来自《{title or '未命名文献'}》：{body}")
    parts.append("")
    if user_notes.strip():
        parts.append("用户的补充要求（必须遵守）：")
        parts.append(user_notes.strip())
        parts.append("")
    parts.append(
        "只输出 JSON。记住：参数只能写建议值，不许写任何实测结果；"
        "没有条目依据的步骤把 claim_ids 留成空数组。"
    )
    return "\n".join(parts)

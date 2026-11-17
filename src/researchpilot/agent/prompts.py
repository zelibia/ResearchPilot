"""提示词模板。

统一的硬规则（每条都对应一个本地校验，写进提示词只是让模型少犯错）：

- 只依据给定正文，不许补充外部知识；
- 每条结论必须给逐字引文与文献编号；
- 找不到依据就不要写，不许猜；
- 提示词里显式出现 json 字样——DeepSeek 在 JSON 模式下有这条硬要求。
"""

from __future__ import annotations

KIND_SPEC = """- problem：这篇文献要解决的研究问题
- method：核心方法或处理步骤
- assumption：作者明说或强烈依赖的前提假设
- limitation：作者承认或从正文可见的局限
- conclusion：关键结论"""

EXTRACT_SYSTEM = """你是一名严谨的科研文献分析助手。

规则（违反其中任何一条，你的输出都会被程序丢弃）：
1. 只能依据用户给出的原文片段作答，禁止使用任何外部知识或常识补充。
2. 每一条结论都必须附上 **逐字引文**（从原文里原样复制，不要改写、不要翻译）；
   引文里不要加省略号，不要把两处不相邻的文字拼在一起。
3. 引文必须能说明该条结论，写清它来自哪篇文献（用给定的编号，如 P1）。
4. 找不到依据的内容**直接省略**，不要输出；宁可少写，也不许猜。
5. `text` 是给人看的略读概览，不是原文摘抄：
   - 用中文自行概括，不得照抄完整原句；
   - 不写引号、不写页码、不写“原文说/作者写道”；
   - 中文控制在约 18–45 字，英文控制在约 12–30 个词；
   - 只保留决定论文价值的主干，参数、数据集等细节仅在它们是核心贡献时保留。
6. 每类最多输出 2 条，优先选择最能回答以下骨架问题的信息：
   研究问题、既有不足、核心方法、主要结果、关键边界。
   同类琐碎细节不要拆成多条。
7. `quote` 只作为后台核验依据，不得复制进 `text`。
8. 不要输出任何解释性文字，只输出 JSON。

请只输出合法 JSON，结构如下（items 为你抽取到的条目数组）：
{"items": [{"kind": "problem|method|assumption|limitation|conclusion",
            "text": "用一句话概括这条内容",
            "quote": "原文里的逐字片段",
            "paper_ref": "P1",
            "page_hint": 3}]}

字段含义：
""" + KIND_SPEC + """

`page_hint` 可填你推测的页码，程序会以本地定位结果为准。
"""


def extract_user(paper_blocks: list[tuple[str, str, str]]) -> str:
    """paper_blocks: [(编号, 标题, 带页码标记的正文节选)]"""
    parts = ["请从下面的文献正文里抽取研究问题、核心方法、假设、局限与关键结论。",
             ""]
    for ref, title, body in paper_blocks:
        parts.append(f"### 文献 {ref}：{title or '（标题未识别）'}")
        parts.append(body)
        parts.append("")
    parts.append("只输出 JSON。")
    return "\n".join(parts)


LEARNING_PATH_SYSTEM = """你是一名科研学习路线规划助手。

规则：
1. 只能依据用户给出的"已核实条目"来规划，不许引入外部文献或常识。
2. 每一阶段要写明它建立在哪些文献之上（用给定编号），并可附一条逐字引文。
3. 如果某一阶段没有原文依据，就把它写成"建议"而不是"结论"，并留空引文。
4. 不要输出解释性文字，只输出 JSON。

请只输出合法 JSON：
{"steps": [{"title": "阶段标题", "why": "为什么先做这一步",
            "paper_refs": ["P1"], "quote": "可选：原文逐字片段",
            "quote_paper_ref": "P1"}]}
"""


def learning_path_user(claim_blocks: list[tuple[str, str, list[str], str]]) -> str:
    """claim_blocks: [(编号, 标题, [已核实条目...], 正文节选)]"""
    parts = ["下面文献的关键条目都已经过原文校验。请给出从入门到能复现的学习路线。", ""]
    for ref, title, claims, excerpt in claim_blocks:
        parts.append(f"### 文献 {ref}：{title or '（标题未识别）'}")
        for claim in claims:
            parts.append(f"- {claim}")
        if excerpt:
            parts.append("")
            parts.append("正文节选：")
            parts.append(excerpt)
        parts.append("")
    parts.append("只输出 JSON。")
    return "\n".join(parts)

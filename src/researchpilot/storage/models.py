"""Pydantic 数据模型 —— 对应 PRD v1.2 第 7 节的 11 个数据对象。

Project / Paper / PaperClaim / Method / Goal / PlanVersion /
ExperimentRun / LabNote / Hypothesis / Artifact / GoalAssessment

另有 v1.3 关系层增量（WorkflowNode / MethodRelation），见文件末尾。

约定：所有模型都可以 `model_dump(mode="json")` 直接落盘，字段名即 SQLite 列名。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Status(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class NoteKind(str, Enum):
    NOTE = "note"
    ERROR = "error"
    CHANGE = "change"
    OBSERVATION = "observation"


class HypothesisStatus(str, Enum):
    IDEA = "idea"
    TO_VERIFY = "to_verify"
    VERIFIED = "verified"
    REFUTED = "refuted"


class PlanMode(str, Enum):
    REPRODUCE = "reproduce"        # A 原文复现
    IMPROVE = "improve"            # B 文献改进对比
    OWN = "own"                    # C 自主研究设想


class AssessmentStatus(str, Enum):
    DONE = "done"
    PARTIAL = "partial"
    NOT_DONE = "not_done"
    FAILED = "failed"


class PlanDraftStatus(str, Enum):
    """计划草稿的生命周期。

    草稿不是结论：``DRAFT`` 只在规划页里存在，既不进导出也不进"目标"统计；
    只有用户确认之后才写成 ``PlanVersion`` + ``Goal``。
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"


class StepStatus(str, Enum):
    """步骤的人工状态。

    **它不是执行结果**：真实跑没跑过只看 ``ExperimentRun.status``。
    用户把某一步勾成"已完成"，这只表示他认为这一步做完了，不代表引擎跑过。
    """

    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    SKIPPED = "skipped"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SuggestionStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class DecisionKind(str, Enum):
    USER_IDEA = "user_idea"            # 用户提出的想法
    AI_SUGGESTION = "ai_suggestion"    # AI 给的建议
    AI_OBJECTION = "ai_objection"      # AI 的质疑或反驳
    USER_DECISION = "user_decision"    # 用户拍板的结论


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"     # 只是提出，尚未采纳
    ACCEPTED = "accepted"     # 已采纳
    REJECTED = "rejected"     # 已否决（保留记录，不删除）


class Certainty(str, Enum):
    STATED = "stated"        # 原文明确
    IMPLIED = "implied"      # 原文暗示
    INFERRED = "inferred"    # 模型推断


class VerificationStatus(str, Enum):
    """元数据核查状态。

    关键约定：出现冲突时保留原始值并标成 CONFLICT，**不自动覆盖**。
    自动"纠正"会让用户失去判断依据，也掩盖了 PDF 元数据不可信这件事。
    """

    UNVERIFIED = "unverified"  # 还没做过外部核查
    VERIFIED = "verified"      # 与外部权威元数据一致
    PARTIAL = "partial"        # 部分字段一致
    CONFLICT = "conflict"      # 存在冲突，保留双方值待人工确认


class Project(BaseModel):
    id: str
    title: str
    domain: str = "地震环境噪声"
    description: str = ""
    status: Status = Status.ACTIVE
    created_at: str = Field(default_factory=_now)


class Paper(BaseModel):
    id: str
    project_id: str
    title: str = ""
    authors: str = ""
    # 作者修正：原始解析值保留在 authors_raw，确认来源与时间另记
    authors_raw: str = ""
    author_source: str = ""
    author_confirmed_at: str = ""
    author_candidates: list = Field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str = ""
    verified: bool = False
    source_urls: list[str] = Field(default_factory=list)
    local_path: str = ""
    sha256: str = ""
    page_count: int = 0
    parse_warnings: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    # --- v1.4 元数据核查增量（旧数据没有这些字段，取默认值即可）---
    external_ids: dict[str, str] = Field(default_factory=dict)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verified_at: str = ""
    metadata_checks: list[dict[str, Any]] = Field(default_factory=list)
    external_metadata: dict[str, Any] = Field(default_factory=dict)
    # 每个猜测字段来自哪里（embedded=PDF 内嵌元数据 / first_page=首页正文）。
    # 冲突时能说清"这个值本来是怎么来的"，用户才知道该信哪一边。
    guess_sources: dict[str, str] = Field(default_factory=dict)
    # 这条记录是怎么来的：upload=用户上传；openalex=拉引用关系时自动导入的候选文献。
    # 下游要能区分"我读过的论文"和"系统顺手带回来的候选"，不能混为一谈。
    origin: str = "upload"
    # --- v1.6 标题质量增量 ---
    # 原始解析值：一旦写入就不再改动，任何"修正"都必须保留它。
    title_raw: str = ""
    # 当前标题的依据：embedded / first_page / filename / body /
    # external:crossref / external:openalex / manual
    title_source: str = ""
    # 人工采用过哪些标题（含来源与说明），用于追溯"标题是什么时候换的"
    title_candidates: list[dict[str, Any]] = Field(default_factory=list)
    title_confirmed_at: str = ""
    # --- 文献整理增量：项目内分类与整理备注 ---
    # 这些字段只用于文献库管理，不参与元数据核查或论文事实判断。
    library_categories: list[str] = Field(default_factory=list)
    library_note: str = ""


class PaperClaim(BaseModel):
    id: str
    project_id: str = ""     # 缺失会让按项目查询与项目导出漏掉这些条目
    paper_id: str
    claim: str
    locator: str = ""
    certainty: Certainty = Certainty.INFERRED
    # --- C3-B：AI 抽取结果必须带出处与校验状态 ---
    kind: str = ""                      # problem | method | assumption | limitation | conclusion
    quote: str = ""                     # 原文逐字片段（校验的对象）
    page: int | None = None             # 本地定位出的页码（不采信模型自报）
    quote_verified: bool = False        # 引文是否在原文里精确命中
    created_by: str = "manual"          # llm | manual
    model: str = ""                     # 产出该条目的 provider/model 标识（不含密钥）
    created_at: str = Field(default_factory=_now)


class Method(BaseModel):
    id: str
    project_id: str
    name: str
    source_paper_ids: list[str] = Field(default_factory=list)
    limitations: str = ""
    notes: str = ""


class Goal(BaseModel):
    id: str
    project_id: str
    statement: str
    metric: str = ""
    acceptance: str = ""
    initial_status: str = "未开始"
    created_at: str = Field(default_factory=_now)


class PlanVersion(BaseModel):
    id: str
    project_id: str
    mode: PlanMode
    version: int = 1
    # steps 的历史类型是 "一句话一步" 的字符串列表；v1.6 起改成结构化的
    # 步骤字典（含证据与建议参数）。用 list[Any] 才能同时读旧记录与新记录，
    # 旧记录不会被这次增量改坏。
    steps: list[Any] = Field(default_factory=list)
    goal_ids: list[str] = Field(default_factory=list)
    method_ids: list[str] = Field(default_factory=list)
    data_requirements: str = ""
    risks: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    notes: str = ""
    # --- v1.6 计划生成增量（P0-1）：可追溯与来源区分 ---
    question: str = ""
    objective: str = ""
    success_criteria: str = ""
    expected_outputs: list[str] = Field(default_factory=list)
    # 每条文献证据都能追到具体 paper_claim（含逐字引文与页码）
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 只允许"建议参数/预期指标"，不允许看起来像实测结果的内容
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str = "user"          # llm | user
    source_draft_id: str = ""
    confirmed_at: str = ""
    # --- v1.7 科研协作增量（P0-A/B/C）---
    # 同一研究主题的多次修订共用一个 series_id；另起新方案 = 新的 series，
    # 版本号在系列内自增，不同主题不会混进同一条修订历史。
    series_id: str = ""
    series_title: str = ""
    derived_from_version_id: str = ""
    created_at: str = Field(default_factory=_now)


class PlanDraft(BaseModel):
    """AI 生成、用户尚未确认的方案草稿（P0-1）。

    与 ``PlanVersion`` 的分工是硬性的：

    - 草稿可以改、可以反复生成，**确认之前不算结论**；
    - 草稿不进项目导出，也不计入"已定目标"统计；
    - 草稿里的每条文献证据都指向 ``PaperClaim``，逐字引文与页码取自本地库，
      不采信模型的转述——这样"证据可追溯"不靠模型的自觉。
    """

    id: str
    project_id: str
    mode: PlanMode = PlanMode.REPRODUCE
    status: PlanDraftStatus = PlanDraftStatus.DRAFT
    question: str = ""
    objective: str = ""
    data_requirements: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    success_criteria: str = ""
    expected_outputs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 用户补充的设定与要求：界面上标"用户设定"，与 AI 推断分开
    user_notes: str = ""
    # 本地校验发现的可疑表述（例如看起来像实测结果的措辞），如实展示
    warnings: list[str] = Field(default_factory=list)
    # 模型引用但本地对不上的编号：丢弃，但记下原因供界面说明
    dropped: list[dict[str, Any]] = Field(default_factory=list)
    claim_count: int = 0        # 生成时用户勾选的已核实条目数
    evidence_count: int = 0     # 草稿里挂了出处的条目数
    ai_step_count: int = 0      # 没有出处、标为 AI 推断的步骤数
    origin: str = "llm"         # llm | user
    provider: str = ""
    model: str = ""
    generated_at: str = ""
    revision: int = 0           # 每次保存修改 +1（界面控件 key 用它刷新）
    created_at: str = Field(default_factory=_now)
    updated_at: str = ""
    confirmed_at: str = ""
    confirmed_version_id: str = ""
    # --- v1.7 科研协作增量（P0-A/B/C）---
    series_id: str = ""
    series_title: str = ""
    derived_from_version_id: str = ""


class DiscussionMessage(BaseModel):
    """一轮科研对话的一条消息（P0-B）。

    纪律：

    - 助手消息永远带 ``labels`` 里的"AI 讨论，未核实"，**不能**当成已验证的科研
      事实，也不能作为计划里的文献证据（证据只认 ``PaperClaim``）；
    - ``context`` 记下这轮对话依据了什么：项目、系列、草稿/版本、引用的条目编号、
      当前步骤。没有上下文的回答不许落库；
    - AI 提的修改放在 ``suggestions`` 里，状态默认 ``pending``：
      **只有用户点"应用到草稿"才会改计划**，AI 不能自动覆盖。
    """

    id: str
    project_id: str
    series_id: str = ""
    draft_id: str = ""                 # 这轮讨论挂在哪份草稿上（可为空）
    role: MessageRole = MessageRole.USER
    content: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    error: str = ""
    created_at: str = Field(default_factory=_now)


class DecisionRecord(BaseModel):
    """重要科研决策台账（P0-B）。

    记录"谁提出的、采纳没有、依据哪些条目、最后落成哪个版本"。
    驳回也保留（``status=rejected``）——那本身就是"这条建议被看过了"的证据。
    """

    id: str
    project_id: str
    series_id: str = ""
    kind: DecisionKind = DecisionKind.USER_IDEA
    text: str = ""
    status: DecisionStatus = DecisionStatus.PROPOSED
    evidence_claim_ids: list[str] = Field(default_factory=list)
    source_message_id: str = ""
    draft_id: str = ""
    plan_version_id: str = ""
    note: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = ""
    decided_at: str = ""


class PlanStepState(BaseModel):
    """某一步的人工进度与备忘（P0-C）。

    按 ``(项目, 系列, 步骤序号)`` 存：同一研究主题的历次修订共用进度，
    切换项目或另起新方案都不会串。

    ``engine_stage`` 只允许填真实存在的计算阶段；为空表示这一步没有可用引擎
    （例如频散测量），界面上就不能给它一个"运行"按钮。
    ``run_id`` 指向真实的 ``ExperimentRun``；引擎是否成功由那条 run 的状态决定，
    与用户自己勾的完成状态是两码事。
    """

    id: str
    project_id: str
    series_id: str
    order: int = 0
    status: StepStatus = StepStatus.TODO
    todo: str = ""
    note: str = ""
    engine_stage: str = ""     # qc | preprocess | xcorr | stack | 空
    run_id: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = ""


class ExperimentRun(BaseModel):
    id: str
    project_id: str
    plan_version_id: str = ""
    engine: str = ""
    engine_version: str = ""
    dataset_id: str = ""
    stations: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    time_window: str = ""
    data_hash: str = ""
    command: str = ""
    config_path: str = ""
    log_path: str = ""
    work_dir: str = ""
    status: RunStatus = RunStatus.PENDING
    started_at: str = ""
    ended_at: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # --- M2-02B：运行追溯所需的补充字段（全部带默认值，旧记录照常读） ---
    exit_code: int | None = None
    interpreter: str = ""                       # 计算解释器路径
    dependencies: dict = Field(default_factory=dict)   # 依赖版本快照
    script_hashes: dict = Field(default_factory=dict)  # 四个脚本的 sha256
    inputs: list[str] = Field(default_factory=list)    # 输入文件清单（相对路径）
    pid: int = 0                                # 后台工作进程 pid（用于取消）
    pgid: int = 0                               # 其进程组 id；终止按组进行，避免留孤儿
    cancel_requested: bool = False
    duration_s: float = 0.0
    summary: dict = Field(default_factory=dict)        # 四段指标与产物摘要
    created_at: str = Field(default_factory=_now)


class LabNote(BaseModel):
    id: str
    project_id: str
    run_id: str = ""
    kind: NoteKind = NoteKind.NOTE
    text: str = ""
    timestamp: str = Field(default_factory=_now)


class Hypothesis(BaseModel):
    id: str
    project_id: str
    text: str
    status: HypothesisStatus = HypothesisStatus.IDEA
    evidence_run_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)


class Artifact(BaseModel):
    id: str
    project_id: str
    run_id: str = ""
    type: Literal["figure", "data", "report", "log", "other"] = "other"
    path: str = ""
    sha256: str = ""
    created_at: str = Field(default_factory=_now)


class GoalAssessment(BaseModel):
    id: str
    goal_id: str
    status: AssessmentStatus
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    next_step: str = ""
    created_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# v1.3 关系层增量
# 纯增量：不改动上面 11 个对象的任何字段，旧数据无需重写。
# ---------------------------------------------------------------------------


class NodeKind(str, Enum):
    """复现流程节点的类型。"""

    DATA = "data"              # 数据准备与质控
    PREPROCESS = "preprocess"  # 预处理
    COMPUTE = "compute"        # 计算（互相关、叠加等）
    VALIDATE = "validate"      # 目标验证
    OUTCOME = "outcome"        # 成果输出


class RelationType(str, Enum):
    """两个方法之间关系的类型。

    区分依据是 PRD §8.2「新论文不一定优于旧论文」：只有 CITES 是外部 API
    能直接给出的真实数据，其余四种都属于判断，必须由人工或模型提出并带出处。
    """

    CITES = "cites"        # B 引用了 A
    EXTENDS = "extends"    # B 在 A 的基础上扩展
    COMPARES = "compares"  # B 与 A 做了对比
    REPLACES = "replaces"  # B 声称替代 A
    DIVERGES = "diverges"  # B 提出不同做法，未必更优


class RelationSource(str, Enum):
    """一条关系从哪来，决定它是否可信。"""

    OPENALEX = "openalex"  # 外部 API 的真实引用记录
    USER = "user"          # 用户录入
    LLM = "llm"            # 模型草拟，默认未核实


class QueryKind(str, Enum):
    """外部查询的类型。C1 用到 DOI 查询，引用类查询在 C2 落地。"""

    DOI_LOOKUP = "doi_lookup"        # Crossref：DOI → 权威元数据
    WORK_LOOKUP = "work_lookup"      # OpenAlex：工作记录
    REFERENCES = "references"        # OpenAlex：参考文献
    CITATIONS = "citations"          # OpenAlex：施引论文
    RELATED = "related"              # OpenAlex：相关文献


class QueryStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ReviewStatus(str, Enum):
    """人工裁决状态。

    驳回**不是删除**：记录留在库里，标成 rejected，作为"这条关系被看过了、
    并被认为不成立/不相关"的证据。静默删除会让人再也说不清当时为什么不采用。
    """

    PENDING = "pending"      # 待人工裁决
    CONFIRMED = "confirmed"  # 人工确认
    REJECTED = "rejected"    # 人工驳回（保留记录）


class PaperRelation(BaseModel):
    """两篇文献之间的一条有方向的关系（C2 增量）。

    **方向约定**：`from_paper_id` 是施动方。
    - `cites`：from 引用了 to。参考文献边是 (本文 → 被引文献)；
      施引边是 (施引文献 → 本文)。
    - 语义类（extends / compares / replaces / diverges）：from 相对 to 的关系。

    两个状态位含义不同，不要混：
    - `verified`：**证据是否充分**。只有来源是 OpenAlex 的真实引用边，
      或经过人工确认的边，才允许为真。
    - `review_status`：**人工裁决**。决定这条边能不能进入下游综述与图谱。
    """

    id: str
    project_id: str
    from_paper_id: str
    to_paper_id: str
    relation_type: RelationType = RelationType.CITES
    source: RelationSource = RelationSource.USER
    provider: str = ""            # crossref / openalex / 空（人工录入）
    external_id: str = ""         # 边的外部标识（对方那条记录的 ID，如 W2139713088）
    locator: str = ""             # 证据位置，如 "OpenAlex referenced_works" / "第 7 页"
    source_record_id: str = ""    # 指向 SourceRecord，可追到查询时间与原始响应
    certainty: Certainty = Certainty.INFERRED
    verified: bool = False
    review_status: ReviewStatus = ReviewStatus.PENDING
    review_note: str = ""
    # 图谱工作区的用户分组标签。只影响节点摆放，不改变 from/to 引用方向，
    # 也不改写 relation_type 所表达的学术关系。
    graph_group: str = ""
    reviewed_at: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_relation(self) -> "PaperRelation":
        if not self.from_paper_id or not self.to_paper_id:
            raise ValueError("关系的两端都必须给出论文 ID")
        if self.from_paper_id == self.to_paper_id:
            raise ValueError("关系不能自环：from_paper_id 与 to_paper_id 相同")
        if self.graph_group not in {"", "method_source", "later_extension", "related"}:
            raise ValueError("未知的图谱分组")
        if (
            self.source is RelationSource.OPENALEX
            and self.relation_type is not RelationType.CITES
        ):
            raise ValueError(
                "openalex 只能提供真实的引用边（cites）；扩展、对比、替代、"
                "提出不同方法属于判断，必须由用户或模型提出并标注出处"
            )
        if self.review_status is ReviewStatus.REJECTED and self.verified:
            raise ValueError("被人工驳回的关系不得标为已验证")
        if self.verified:
            has_evidence = bool(self.external_id or self.locator)
            if not has_evidence:
                raise ValueError("标为已验证的关系必须给出外部 ID 或证据位置")
            if (
                self.source is not RelationSource.OPENALEX
                and self.review_status is not ReviewStatus.CONFIRMED
            ):
                raise ValueError(
                    "非 openalex 来源的关系必须经人工确认（review_status=confirmed）"
                    "才能标为已验证"
                )
        return self

    @property
    def downstream_ready(self) -> bool:
        """能否进入下游的综述与图谱：已验证且未被驳回。"""
        return self.verified and self.review_status is not ReviewStatus.REJECTED


class SourceRecord(BaseModel):
    """一次外部查询的来源记录。

    对应要求「所有外部查询记录来源、查询时间、外部 ID 和核实状态」。
    缓存文件路径一并记下，便于事后复核对方到底返回了什么。
    """

    id: str
    project_id: str
    paper_id: str = ""
    provider: str = ""                 # crossref / openalex
    query_kind: QueryKind
    query_value: str = ""              # 发起查询用的值（DOI / OpenAlex ID）
    external_id: str = ""              # 对方返回的 ID
    fetched_at: str = ""               # 数据抓取时间（可能是缓存里的旧时间）
    status: QueryStatus = QueryStatus.ERROR
    from_cache: bool = False
    stale: bool = False                # 用了过期缓存
    http_status: int = 0
    cache_path: str = ""
    result_count: int = 0
    note: str = ""
    created_at: str = Field(default_factory=_now)


class WorkflowNode(BaseModel):
    """复现流程图上的一个节点：把「状态」与「证据」绑在一起。

    节点不靠创建时间排序，用显式的 ``order``；不用标题做关联，用稳定的 ``key``，
    这样改标题、改措辞都不会打断已有的引用与实验记录的挂接。
    """

    id: str
    project_id: str
    plan_version_id: str = ""
    key: str
    title: str
    kind: NodeKind = NodeKind.COMPUTE
    order: int = 0
    status: RunStatus = RunStatus.PENDING
    method_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    note_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=_now)

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("WorkflowNode.key 不能为空")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError(
                "WorkflowNode.key 只能用小写字母、数字与下划线，且以字母开头"
            )
        return value


class MethodRelation(BaseModel):
    """两个方法之间的一条有类型、带出处的边。

    校验规则对应增量设计 v1.3 第 4 节的 RB-01 / RB-02 / RB-03，
    把「未核实的关系不得显示为已证实的科研结论」落在数据层，
    而不是靠每个界面各自自觉。
    """

    id: str
    project_id: str
    from_method_id: str
    to_method_id: str
    relation_type: RelationType
    evidence_paper_id: str = ""
    locator: str = ""
    source: RelationSource = RelationSource.USER
    certainty: Certainty = Certainty.INFERRED
    verified: bool = False
    note: str = ""
    created_at: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def _check_evidence_and_source(self) -> "MethodRelation":
        if not self.from_method_id or not self.to_method_id:
            raise ValueError("关系的两端都必须给出方法 ID")
        if self.from_method_id == self.to_method_id:
            raise ValueError("关系不能自环：from_method_id 与 to_method_id 相同")
        if self.verified and not (self.evidence_paper_id and self.locator.strip()):
            raise ValueError(
                "标记为已核实的关系必须同时给出 evidence_paper_id 与 locator"
            )
        if (
            self.source is RelationSource.OPENALEX
            and self.relation_type is not RelationType.CITES
        ):
            raise ValueError(
                "openalex 只能提供真实的引用边（cites）；扩展、对比、替代、"
                "提出不同方法属于判断，必须由用户或模型提出并标注出处与不确定度"
            )
        return self

    @property
    def claimable(self) -> bool:
        """是否可以对外作为「已证实的科研结论」呈现。"""
        return self.verified and bool(
            self.evidence_paper_id and self.locator.strip()
        )

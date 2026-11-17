"""仓储层：模型对象与 SQLite 之间的读写。

上层（ui / planning / exports）一律通过本模块访问数据库，不直接写 SQL。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping, TypeVar

from pydantic import BaseModel

from . import database
from .models import (
    Artifact,
    Certainty,
    DecisionRecord,
    DecisionStatus,
    DiscussionMessage,
    ExperimentRun,
    Goal,
    GoalAssessment,
    Hypothesis,
    LabNote,
    Method,
    MethodRelation,
    Paper,
    PaperClaim,
    PaperRelation,
    PlanDraft,
    PlanDraftStatus,
    PlanStepState,
    PlanVersion,
    Project,
    ReviewStatus,
    RelationSource,
    RelationType,
    SourceRecord,
    WorkflowNode,
)

T = TypeVar("T", bound=BaseModel)

_TABLE_BY_CLASS: dict[type[BaseModel], str] = {
    Project: "projects",
    Paper: "papers",
    PaperClaim: "paper_claims",
    Method: "methods",
    Goal: "goals",
    PlanVersion: "plan_versions",
    ExperimentRun: "experiment_runs",
    LabNote: "lab_notes",
    Hypothesis: "hypotheses",
    Artifact: "artifacts",
    GoalAssessment: "goal_assessments",
    # v1.3 关系层增量
    WorkflowNode: "workflow_nodes",
    MethodRelation: "method_relations",
    # v1.4 文献核查增量
    SourceRecord: "source_records",
    # v1.5 文献关系增量（C2）
    PaperRelation: "paper_relations",
    # v1.6 计划生成增量（P0-1）
    PlanDraft: "plan_drafts",
    # v1.7 科研协作增量（P0-A/B/C）
    DiscussionMessage: "discussion_messages",
    DecisionRecord: "decision_records",
    PlanStepState: "plan_step_states",
}

# 不参与项目导出/导入的表。
# 计划草稿是**工作过程中的临时笔记**，不是项目结论：确认之前不能进
# "项目归档"、也不能被当成研究目标。把它们排出导出是"未确认草稿不进导出"
# 这条规则的落地点，而不是靠导出时再去过滤某几个字段。
#
# v1.7 追加：对话记录与步骤备忘同样是工作过程数据，不进归档；
# 决策台账则**只导出已采纳的**（未采纳的 AI 建议不是项目结论）。
EXPORT_EXCLUDED_TABLES: frozenset[str] = frozenset(
    {"plan_drafts", "discussion_messages", "plan_step_states"}
)


def _export_filter(table: str, obj: BaseModel) -> bool:
    """某张表里哪些记录可以进项目归档。默认全部进。"""
    if table == "decision_records":
        return obj.status is DecisionStatus.ACCEPTED
    return True


def new_id(prefix: str) -> str:
    """可读的短 ID，例如 proj_3f9a1c2b。"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    """当前时间（秒精度，本地时区）。全项目写入时间戳统一走这里。"""
    return datetime.now().isoformat(timespec="seconds")


def _table(model_cls: type[BaseModel]) -> str:
    try:
        return _TABLE_BY_CLASS[model_cls]
    except KeyError as exc:  # pragma: no cover - 编程错误，不是运行期输入错误
        raise KeyError(f"{model_cls.__name__} 未登记到 repository._TABLE_BY_CLASS") from exc


def save(obj: T) -> T:
    """插入或更新。返回原对象，便于链式使用。"""
    table = _table(type(obj))
    payload = obj.model_dump_json()
    project_id = getattr(obj, "project_id", None) or (
        obj.id if isinstance(obj, Project) else None
    )
    created_at = getattr(obj, "created_at", "") or ""
    with database.session() as conn:
        conn.execute(
            f"INSERT INTO {table} (id, project_id, created_at, payload) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, "
            f"created_at=excluded.created_at, payload=excluded.payload",
            (obj.id, project_id, created_at, payload),
        )
    return obj


def get(model_cls: type[T], obj_id: str) -> T | None:
    with database.session() as conn:
        row = conn.execute(
            f"SELECT payload FROM {_table(model_cls)} WHERE id = ?", (obj_id,)
        ).fetchone()
    return model_cls.model_validate_json(row["payload"]) if row else None


def list_all(model_cls: type[T], project_id: str | None = None) -> list[T]:
    sql = f"SELECT payload FROM {_table(model_cls)}"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    sql += " ORDER BY created_at DESC"
    with database.session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [model_cls.model_validate_json(r["payload"]) for r in rows]


def list_by_ids(model_cls: type[T], ids: Iterable[str]) -> list[T]:
    ids = [i for i in ids if i]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with database.session() as conn:
        rows = conn.execute(
            f"SELECT payload FROM {_table(model_cls)} WHERE id IN ({marks})", tuple(ids)
        ).fetchall()
    return [model_cls.model_validate_json(r["payload"]) for r in rows]


def delete(model_cls: type[BaseModel], obj_id: str) -> None:
    with database.session() as conn:
        conn.execute(f"DELETE FROM {_table(model_cls)} WHERE id = ?", (obj_id,))


def export_project_json(project_id: str) -> dict[str, list[dict]]:
    """把项目相关的全部对象导出成一份 JSON 快照（PRD 要求的可移植包核心）。

    键名与表名一致，内容是该项目的全部对象。这里直接遍历模型注册表，
    新增实体时不必再改本函数：早先的版本是手写键值对，漏掉了
    ``paper_claims`` 与 ``goal_assessments``，导出归档会静默丢数据。
    """
    snapshot: dict[str, list[dict]] = {}
    for model_cls, table in _TABLE_BY_CLASS.items():
        if table in EXPORT_EXCLUDED_TABLES:
            continue
        if model_cls is Project:
            # Project 的 project_id 列存放自己的 id，这里仍按主键显式过滤，
            # 避免历史数据里 project_id 缺失时把别的项目带进来。
            objects = [p for p in list_all(Project) if p.id == project_id]
        else:
            objects = list_all(model_cls, project_id)
        snapshot[table] = [
            obj.model_dump(mode="json") for obj in objects if _export_filter(table, obj)
        ]
    return snapshot


def export_project_json_text(project_id: str) -> str:
    return json.dumps(export_project_json(project_id), ensure_ascii=False, indent=2)


_MODEL_BY_TABLE: dict[str, type[BaseModel]] = {
    table: model_cls
    for model_cls, table in _TABLE_BY_CLASS.items()
    if table not in EXPORT_EXCLUDED_TABLES
}


def import_project_json(snapshot: Mapping[str, Any]) -> dict[str, int]:
    """把 `export_project_json()` 生成的快照写回数据库。

    按主键 upsert：同一个快照重复导入不会产生重复行，也不会因为 ID 相同而报错。
    快照里出现不认识的表名时直接报错，不静默忽略——静默忽略会让人以为导入成功了，
    而实际上那部分数据被丢掉了。
    """
    # 快照来自本函数导出时不会带草稿表；万一别处给了一份带草稿的快照，
    # 这里忽略它们而不是报错——草稿不是项目归档的一部分。
    unknown = sorted(set(snapshot) - set(_MODEL_BY_TABLE) - EXPORT_EXCLUDED_TABLES)
    if unknown:
        raise ValueError(f"快照里有无法识别的表：{unknown}（可能来自更高版本）")

    counts: dict[str, int] = {}
    for table, model_cls in _MODEL_BY_TABLE.items():
        rows = snapshot.get(table) or []
        if not isinstance(rows, list):
            raise ValueError(f"快照里的 {table} 应当是列表，实际是 {type(rows).__name__}")
        for payload in rows:
            save(model_cls.model_validate(payload))
        counts[table] = len(rows)
    return counts


# ---------------------------------------------------------------------------
# v1.4 文献核查：幂等去重与外部来源记录
# ---------------------------------------------------------------------------


def find_paper_by_sha256(project_id: str, sha256: str) -> Paper | None:
    """同一份文件在同一个项目里只应有一条 Paper 记录。"""
    if not sha256:
        return None
    for paper in list_all(Paper, project_id):
        if paper.sha256 and paper.sha256 == sha256:
            return paper
    return None


def list_source_records(
    project_id: str, paper_id: str | None = None, provider: str | None = None
) -> list[SourceRecord]:
    """外部查询来源记录，最近的在前。"""
    records = list_all(SourceRecord, project_id)
    if paper_id is not None:
        records = [r for r in records if r.paper_id == paper_id]
    if provider is not None:
        records = [r for r in records if r.provider == provider]
    return sorted(records, key=lambda r: (r.created_at, r.id), reverse=True)


# ---------------------------------------------------------------------------
# v1.5 文献关系（C2）：方向、证据、人工裁决
# ---------------------------------------------------------------------------


def find_paper_by_openalex_id(project_id: str, openalex_id: str) -> Paper | None:
    """按 OpenAlex ID 找项目内的文献（用于拉引用关系时避免重复导入）。"""
    if not openalex_id:
        return None
    for paper in list_all(Paper, project_id):
        if paper.external_ids.get("openalex") == openalex_id:
            return paper
    return None


def _assert_endpoints_in_project(relation: PaperRelation) -> None:
    """项目隔离：关系的两端必须存在，且都属于这条关系声明的项目。

    这是"项目间数据隔离"的强制点。只靠查询过滤是不够的——
    写入时就得挡住跨项目的边，否则脏数据一旦落库，后面的过滤只是在补救。
    """
    for paper_id in (relation.from_paper_id, relation.to_paper_id):
        paper = get(Paper, paper_id)
        if paper is None:
            raise ValueError(f"关系端点不存在：{paper_id}")
        if paper.project_id != relation.project_id:
            raise ValueError(
                f"跨项目关系被拒绝：{paper_id} 属于 {paper.project_id}，"
                f"不属于 {relation.project_id}"
            )


def add_paper_relation(relation: PaperRelation) -> tuple[PaperRelation, bool]:
    """写入一条文献关系。同一组 (from, to, 类型) 已存在时返回已有记录。

    第二个返回值表示"本次是否真的新增"。
    """
    _assert_endpoints_in_project(relation)
    existing = find_paper_relation(
        relation.project_id, relation.from_paper_id, relation.to_paper_id,
        relation.relation_type,
    )
    if existing is not None:
        return existing, False
    return save(relation), True


def find_paper_relation(
    project_id: str, from_paper_id: str, to_paper_id: str, relation_type: RelationType
) -> PaperRelation | None:
    matches = list_paper_relations(
        project_id,
        from_paper_id=from_paper_id,
        to_paper_id=to_paper_id,
        relation_type=relation_type,
    )
    return matches[0] if matches else None


def get_directed_edge(project_id: str, from_paper_id: str, to_paper_id: str) -> list[PaperRelation]:
    """两篇论文之间的全部有向边（不区分类型）。"""
    return list_paper_relations(
        project_id, from_paper_id=from_paper_id, to_paper_id=to_paper_id
    )


def list_paper_relations(
    project_id: str,
    *,
    paper_id: str | None = None,
    direction: str = "both",
    from_paper_id: str | None = None,
    to_paper_id: str | None = None,
    relation_type: RelationType | None = None,
    source: RelationSource | None = None,
    review_status: ReviewStatus | None = None,
    verified: bool | None = None,
    downstream_only: bool = False,
) -> list[PaperRelation]:
    """按条件查关系。`paper_id` + `direction` 是最常用的入口。"""
    if direction not in {"out", "in", "both"}:
        raise ValueError("direction 只能是 out / in / both")

    relations = list_all(PaperRelation, project_id)   # 已按 project_id 过滤
    if paper_id is not None:
        if direction == "out":
            relations = [r for r in relations if r.from_paper_id == paper_id]
        elif direction == "in":
            relations = [r for r in relations if r.to_paper_id == paper_id]
        else:
            relations = [
                r for r in relations
                if paper_id in (r.from_paper_id, r.to_paper_id)
            ]
    if from_paper_id is not None:
        relations = [r for r in relations if r.from_paper_id == from_paper_id]
    if to_paper_id is not None:
        relations = [r for r in relations if r.to_paper_id == to_paper_id]
    if relation_type is not None:
        relations = [r for r in relations if r.relation_type is relation_type]
    if source is not None:
        relations = [r for r in relations if r.source is source]
    if review_status is not None:
        relations = [r for r in relations if r.review_status is review_status]
    if verified is not None:
        relations = [r for r in relations if r.verified is verified]
    if downstream_only:
        relations = [r for r in relations if r.downstream_ready]
    return sorted(relations, key=lambda r: (r.created_at, r.id), reverse=True)


def review_paper_relation(
    relation: PaperRelation, decision: ReviewStatus, note: str = ""
) -> PaperRelation:
    """人工确认 / 驳回一条关系。

    - 驳回：`verified` 置 false，但记录保留（不删除），`review_note` 记原因。
    - 确认：只有存在证据（外部 ID 或证据位置）时才置 `verified=True`；
      没有证据时保持未验证，由界面提示补证据——不给人一个"点一下就核实了"的假动作。
    """
    now = datetime.now().isoformat(timespec="seconds")
    relation.review_status = decision
    relation.review_note = note
    relation.reviewed_at = now
    relation.updated_at = now
    if decision is ReviewStatus.REJECTED:
        relation.verified = False
    elif decision is ReviewStatus.CONFIRMED and (relation.external_id or relation.locator):
        relation.verified = True

    # 变更后重新走一遍模型校验，确保"驳回 + 已验证"这类矛盾状态进不了库
    validated = PaperRelation.model_validate(relation.model_dump())
    return save(validated)


def list_relations_for_source_record(project_id: str, source_record_id: str) -> list[PaperRelation]:
    """某次外部查询带回来的全部关系（用于追溯这一批数据的来源）。"""
    return [
        r for r in list_all(PaperRelation, project_id)
        if r.source_record_id == source_record_id
    ]


# ---------------------------------------------------------------------------
# v1.3 关系层：方法关系的增删查，以及「未核实不得当结论」的唯一出口
# ---------------------------------------------------------------------------

_RELATION_LABEL: dict[RelationType, str] = {
    RelationType.CITES: "引用",
    RelationType.EXTENDS: "扩展",
    RelationType.COMPARES: "对比",
    RelationType.REPLACES: "替代",
    RelationType.DIVERGES: "提出不同方法",
}

_RELATION_SOURCE_LABEL: dict[RelationSource, str] = {
    RelationSource.OPENALEX: "OpenAlex 引用记录",
    RelationSource.USER: "用户录入",
    RelationSource.LLM: "模型推断",
}

_CERTAINTY_LABEL: dict[Certainty, str] = {
    Certainty.STATED: "原文明确",
    Certainty.IMPLIED: "原文暗示",
    Certainty.INFERRED: "模型推断",
}


def list_workflow_nodes(
    project_id: str, plan_version_id: str | None = None
) -> list[WorkflowNode]:
    """按显式 order 排序返回流程节点（不用创建时间推断顺序）。"""
    nodes = list_all(WorkflowNode, project_id)
    if plan_version_id is not None:
        nodes = [n for n in nodes if n.plan_version_id == plan_version_id]
    return sorted(nodes, key=lambda n: (n.order, n.key))


def find_node_by_key(
    project_id: str, key: str, plan_version_id: str = ""
) -> WorkflowNode | None:
    for node in list_workflow_nodes(project_id, plan_version_id or None):
        if node.key == key and node.plan_version_id == plan_version_id:
            return node
    return None


def add_workflow_node(node: WorkflowNode) -> tuple[WorkflowNode, bool]:
    """写入流程节点。同一 (计划版本, key) 已存在时返回已有节点，不重复插入。

    第二个返回值表示「本次是否真的新增」，便于调用方如实地报「已存在」而不是「已保存」。
    """
    existing = find_node_by_key(node.project_id, node.key, node.plan_version_id)
    if existing is not None:
        return existing, False
    return save(node), True


def list_relations(
    project_id: str,
    *,
    from_method_id: str | None = None,
    to_method_id: str | None = None,
    relation_type: RelationType | None = None,
    verified: bool | None = None,
    source: RelationSource | None = None,
) -> list[MethodRelation]:
    """按条件筛选方法关系。所有条件都是可选的，默认返回项目下全部关系。"""
    relations = list_all(MethodRelation, project_id)
    if from_method_id is not None:
        relations = [r for r in relations if r.from_method_id == from_method_id]
    if to_method_id is not None:
        relations = [r for r in relations if r.to_method_id == to_method_id]
    if relation_type is not None:
        relations = [r for r in relations if r.relation_type is relation_type]
    if source is not None:
        relations = [r for r in relations if r.source is source]
    if verified is not None:
        relations = [r for r in relations if r.claimable is verified]
    return sorted(relations, key=lambda r: r.created_at, reverse=True)


def find_relation(
    project_id: str,
    from_method_id: str,
    to_method_id: str,
    relation_type: RelationType,
) -> MethodRelation | None:
    """查找同一组 (起点, 终点, 类型) 的边，用于去重。"""
    matches = list_relations(
        project_id,
        from_method_id=from_method_id,
        to_method_id=to_method_id,
        relation_type=relation_type,
    )
    return matches[0] if matches else None


def add_relation(relation: MethodRelation) -> tuple[MethodRelation, bool]:
    """写入一条方法关系；同一组 (起点, 终点, 类型) 已存在时返回已有记录。

    第二个返回值表示「本次是否真的新增」。重复录入同一条边不会产生第二条记录，
    否则图上会出现重边，看起来像两条独立证据。
    """
    existing = find_relation(
        relation.project_id,
        relation.from_method_id,
        relation.to_method_id,
        relation.relation_type,
    )
    if existing is not None:
        return existing, False
    return save(relation), True


def relations_of_method(
    project_id: str, method_id: str, direction: str = "both"
) -> list[MethodRelation]:
    """找出与某个方法相关的边。direction 取 out / in / both。"""
    if direction not in {"out", "in", "both"}:
        raise ValueError("direction 只能是 out / in / both")
    relations = list_all(MethodRelation, project_id)
    if direction == "out":
        return [r for r in relations if r.from_method_id == method_id]
    if direction == "in":
        return [r for r in relations if r.to_method_id == method_id]
    return [
        r
        for r in relations
        if method_id in (r.from_method_id, r.to_method_id)
    ]


def unverified_relations(project_id: str) -> list[MethodRelation]:
    """尚未核实的关系。这些只能在界面上列为「待核实线索」，不能作为结论。"""
    return list_relations(project_id, verified=False)


def relation_counts(project_id: str) -> dict[str, int]:
    relations = list_all(MethodRelation, project_id)
    verified = sum(1 for r in relations if r.claimable)
    return {
        "total": len(relations),
        "verified": verified,
        "unverified": len(relations) - verified,
    }


# ---------------------------------------------------------------------------
# v1.6 计划生成（P0-1）：草稿与已核实条目的读取helper
# ---------------------------------------------------------------------------


def list_drafts(
    project_id: str, status: PlanDraftStatus | None = None
) -> list[PlanDraft]:
    """项目下的计划草稿，最近的在前。按状态过滤是可选的。"""
    drafts = list_all(PlanDraft, project_id)
    if status is not None:
        drafts = [d for d in drafts if d.status is status]
    return drafts


def claims_of_project(project_id: str) -> list[PaperClaim]:
    """项目下全部已分析条目。

    历史记录里 ``PaperClaim.project_id`` 可能为空（该字段是后补的），所以这里
    以"条目所属文献是否属于本项目"为准，而不是只看 project_id 列。按 project_id
    单独过滤会漏掉这些条目，规划页就选不到它们。
    """
    paper_ids = {paper.id for paper in list_all(Paper, project_id)}
    return [claim for claim in list_all(PaperClaim) if claim.paper_id in paper_ids]


# ---------------------------------------------------------------------------
# v1.7 科研协作（P0-A/B/C）：对话、决策台账、步骤状态
# 所有读取都强制带 project_id，跨项目的记录在查询层就拿不到。
# ---------------------------------------------------------------------------


def list_messages(
    project_id: str, *, series_id: str | None = None, draft_id: str | None = None
) -> list[DiscussionMessage]:
    """项目下的对话记录，时间正序（读起来像聊天）。"""
    messages = [
        item for item in list_all(DiscussionMessage, project_id)
        if item.project_id == project_id
    ]
    if series_id is not None:
        messages = [item for item in messages if item.series_id == series_id]
    if draft_id is not None:
        messages = [item for item in messages if item.draft_id == draft_id]
    return sorted(messages, key=lambda item: (item.created_at, item.id))


def list_decisions(
    project_id: str,
    *,
    series_id: str | None = None,
    status: DecisionStatus | None = None,
) -> list[DecisionRecord]:
    """项目下的决策台账，最近的在前。"""
    decisions = [
        item for item in list_all(DecisionRecord, project_id)
        if item.project_id == project_id
    ]
    if series_id is not None:
        decisions = [item for item in decisions if item.series_id == series_id]
    if status is not None:
        decisions = [item for item in decisions if item.status is status]
    return sorted(decisions, key=lambda item: (item.created_at, item.id), reverse=True)


def list_step_states(project_id: str, series_id: str | None = None) -> list[PlanStepState]:
    states = [
        item for item in list_all(PlanStepState, project_id)
        if item.project_id == project_id
    ]
    if series_id is not None:
        states = [item for item in states if item.series_id == series_id]
    return sorted(states, key=lambda item: item.order)


def find_step_state(project_id: str, series_id: str, order: int) -> PlanStepState | None:
    for state in list_step_states(project_id, series_id):
        if state.order == order:
            return state
    return None


def describe_relation(
    relation: MethodRelation, from_name: str = "", to_name: str = ""
) -> str:
    """把一条边渲染成一句可读的话，并强制带上核实状态。

    这是「未核实的关系不得显示为已证实的科研结论」这条规则的唯一出口。
    界面只允许用本函数生成关系描述，不允许自己拼字符串——拼字符串就绕过了标记。
    """
    left = from_name or relation.from_method_id
    right = to_name or relation.to_method_id
    label = _RELATION_LABEL[relation.relation_type]

    if relation.claimable:
        where = " ".join(
            part for part in (relation.evidence_paper_id, relation.locator) if part
        )
        return f"{left} —{label}→ {right}（已核实，出处：{where}）"

    clues = [
        f"来源：{_RELATION_SOURCE_LABEL[relation.source]}",
        f"证据强度：{_CERTAINTY_LABEL[relation.certainty]}",
    ]
    hint = " ".join(
        part for part in (relation.evidence_paper_id, relation.locator) if part
    )
    if hint:
        clues.append(f"线索：{hint}")
    return f"{left} —{label}→ {right}（未核实，{'；'.join(clues)}；核实前不作为结论）"

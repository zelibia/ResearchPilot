"""SQLite 持久化。

设计取舍：每个对象一张表，主键 `id`，整条记录以 JSON 存在 `payload` 列，
同时把常用的 `project_id` / `created_at` 提成独立列做索引。

这样做的好处是模型字段变动不需要迁移脚本，且任何一张表都能直接导出成 JSON，
符合 PRD 里"项目可整体导出归档"和"一个项目一套 JSON 快照"的要求。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..config import PATHS

# 当前 schema 版本。加表时在这里递增，并在 migrate() 的 note 里写清加了什么。
# v1.3-1：新增 workflow_nodes、method_relations、schema_meta
# v1.4-1：新增 source_records（外部查询来源记录）
# v1.5-1：新增 paper_relations（文献间关系，含引用方向与人工裁决）
# v1.6-1：新增 plan_drafts（AI 计划草稿；确认后才写成 plan_versions）
# v1.7-1：新增 discussion_messages / decision_records / plan_step_states（科研协作）
SCHEMA_VERSION = "1.7-1"

# 所有表结构一致：id 主键 + project_id / created_at 两个查询列 + payload(JSON)。
# 只对真实存在的列建索引；paper_id、run_id 这类字段在 payload 里，当前数据量不需要索引。
OBJECT_TABLES: tuple[str, ...] = (
    "projects",
    "papers",
    "paper_claims",
    "methods",
    "goals",
    "plan_versions",
    "experiment_runs",
    "lab_notes",
    "hypotheses",
    "artifacts",
    "goal_assessments",
    # v1.3 关系层增量
    "workflow_nodes",
    "method_relations",
    # v1.4 文献核查增量
    "source_records",
    # v1.5 文献关系增量（C2）
    "paper_relations",
    # v1.6 计划生成增量（P0-1）
    "plan_drafts",
    # v1.7 科研协作增量（P0-A/B/C）
    "discussion_messages",
    "decision_records",
    "plan_step_states",
)

# 保留旧名，供冒烟脚本与外部引用
TABLES: dict[str, tuple[str, ...]] = {
    name: (("created_at",) if name == "projects" else ("project_id", "created_at"))
    for name in OBJECT_TABLES
}

SCHEMA = "".join(
    f"""
CREATE TABLE IF NOT EXISTS {_table} (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    created_at TEXT,
    payload TEXT NOT NULL
);
"""
    for _table in OBJECT_TABLES
)

for _table, _cols in TABLES.items():
    for _col in _cols:
        SCHEMA += f"CREATE INDEX IF NOT EXISTS idx_{_table}_{_col} ON {_table}({_col});\n"

# 迁移记录表：只记「这台机器什么时候补齐到哪一版」，不含业务数据。
SCHEMA += """
CREATE TABLE IF NOT EXISTS schema_meta (
    schema_version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note TEXT
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """打开连接并确保表结构存在。"""
    path = db_path or PATHS.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def table_names(db_path: Path | None = None) -> list[str]:
    """列出库里的表名。库不存在时返回空列表，不创建文件。"""
    path = Path(db_path) if db_path else PATHS.db_path
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return [row[0] for row in rows]
    finally:
        conn.close()


def migrate(db_path: Path | None = None, note: str = "") -> dict[str, Any]:
    """幂等地把数据库补齐到当前 schema，并记录版本。

    本项目的存储设计是「一类对象一张表 + payload JSON」，因此迁移只有一件事：
    把缺的表建出来。旧表、旧列、旧 payload 一律不改，所以不需要反向迁移脚本。
    连续执行两次，第二次的 ``created_tables`` 为空。
    """
    path = Path(db_path) if db_path else PATHS.db_path
    existed_before = path.exists()
    before = set(table_names(path))

    conn = connect(path)  # connect() 执行 SCHEMA，缺的表在这里补齐
    try:
        after = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        created = sorted(after - before)
        conn.execute(
            "INSERT INTO schema_meta (schema_version, applied_at, note) VALUES (?, ?, ?) "
            "ON CONFLICT(schema_version) DO UPDATE SET applied_at=excluded.applied_at, "
            "note=excluded.note",
            (
                SCHEMA_VERSION,
                datetime.now().isoformat(timespec="seconds"),
                note or f"新增 {len(created)} 张表",
            ),
        )
        conn.commit()
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in sorted(after)
        }
        recorded = conn.execute(
            "SELECT schema_version, applied_at, note FROM schema_meta "
            "ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return {
        "db_path": str(path),
        "existed_before": existed_before,
        "created_tables": created,
        "schema_version": SCHEMA_VERSION,
        "recorded": dict(recorded) if recorded else {},
        "table_counts": counts,
    }

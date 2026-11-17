"""Idempotent P0-04 schema supplements.

P0-01 owns the annotations table.  This module adds only the P0-04 audit,
idempotency, undo and FTS structures.  It deliberately tolerates an existing
installation whose base tables were created before v1.16.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .orm import AnnotationOperationBatch  # noqa: F401 - ensures metadata is loaded


def _add_missing_columns(engine: Engine) -> list[str]:
    """Add P0-04 columns when an early design DDL already created the table."""

    added: list[str] = []
    inspector = sa.inspect(engine)
    if "annotation_operation_batches" not in inspector.get_table_names():
        return added
    existing = {
        str(column["name"])
        for column in inspector.get_columns("annotation_operation_batches")
    }
    definitions = {
        "before_json": "TEXT NOT NULL DEFAULT '{}'",
        "client_mutation_id": "TEXT NOT NULL DEFAULT ''",
        "undone_at": "TEXT",
    }
    with engine.begin() as conn:
        for name, definition in definitions.items():
            if name in existing:
                continue
            conn.exec_driver_sql(
                f"ALTER TABLE annotation_operation_batches "
                f"ADD COLUMN {name} {definition}"
            )
            added.append(name)
    return added


def _object_names(engine: Engine) -> tuple[set[str], set[str]]:
    inspector = sa.inspect(engine)
    return set(inspector.get_table_names()), set(inspector.get_view_names())


def _create_compatibility_view(engine: Engine) -> None:
    tables, views = _object_names(engine)
    if "annotation_operation_logs" in tables or "annotation_operation_logs" in views:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE VIEW annotation_operation_logs AS
            SELECT
                id,
                batch_id,
                annotation_id,
                document_id,
                action,
                actor_id,
                before_json,
                after_json,
                created_at
            FROM annotation_audit
            """
        )


def deploy_annotation_schema(engine: Engine) -> dict[str, Any]:
    """Deploy P0-04 FTS/triggers and compatibility objects.

    FTS5 trigram is the formal search path.  If a SQLite build lacks either
    extension, deployment still succeeds and the service uses a LIKE fallback
    while reporting ``fts5_available=false`` in health metadata.
    """

    added_columns = _add_missing_columns(engine)
    fts_available = False
    fts_error = ""
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS annotation_fts USING fts5(
                    annotation_id UNINDEXED,
                    document_id UNINDEXED,
                    quote,
                    comment_markdown,
                    tokenize = 'trigram'
                )
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_insert
                AFTER INSERT ON annotations
                WHEN NEW.deleted_at IS NULL
                BEGIN
                    INSERT INTO annotation_fts(
                        rowid, annotation_id, document_id, quote, comment_markdown
                    )
                    VALUES (
                        NEW.rowid,
                        NEW.id,
                        NEW.document_id,
                        NEW.quote,
                        NEW.comment_markdown
                    );
                END
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_update
                AFTER UPDATE OF quote, comment_markdown, deleted_at ON annotations
                BEGIN
                    DELETE FROM annotation_fts WHERE rowid = OLD.rowid;
                    INSERT INTO annotation_fts(
                        rowid, annotation_id, document_id, quote, comment_markdown
                    )
                    SELECT
                        NEW.rowid,
                        NEW.id,
                        NEW.document_id,
                        NEW.quote,
                        NEW.comment_markdown
                    WHERE NEW.deleted_at IS NULL;
                END
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_annotation_fts_delete
                AFTER DELETE ON annotations
                BEGIN
                    DELETE FROM annotation_fts WHERE rowid = OLD.rowid;
                END
                """
            )
            conn.exec_driver_sql("DELETE FROM annotation_fts")
            conn.exec_driver_sql(
                """
                INSERT INTO annotation_fts(
                    rowid, annotation_id, document_id, quote, comment_markdown
                )
                SELECT
                    rowid, id, document_id, quote, comment_markdown
                FROM annotations
                WHERE deleted_at IS NULL
                """
            )
        fts_available = True
    except sa.exc.DatabaseError as exc:
        fts_error = str(exc)

    _create_compatibility_view(engine)
    return {
        "annotation_schema": "1.16.1-p0-04",
        "fts5_trigram": fts_available,
        "fts_error": fts_error,
        "added_columns": added_columns,
    }


def fts5_trigram_available(engine: Engine) -> bool:
    tables, _ = _object_names(engine)
    if "annotation_fts" not in tables:
        return False
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "SELECT count(*) FROM annotation_fts WHERE annotation_fts MATCH '\"rp_probe\"'"
            ).scalar_one()
        return True
    except sa.exc.DatabaseError:
        return False

"""P0-06 current official summary revision guard."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine


def deploy_summary_schema(engine: Engine) -> dict[str, Any]:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS trg_project_summary_current_revision_guard
            BEFORE UPDATE OF current_official_revision_id, current_revision_no
            ON project_summaries
            WHEN NEW.current_official_revision_id IS NOT NULL
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'current summary revision must belong to the summary and be official'
                )
                WHERE NOT EXISTS (
                    SELECT 1 FROM summary_revisions r
                    WHERE r.id = NEW.current_official_revision_id
                      AND r.summary_id = NEW.id
                      AND r.revision_no = NEW.current_revision_no
                      AND r.status = 'official'
                );
            END
            """
        )
    return {"summary_schema": "1.16.1-p0-06", "official_guard": True}

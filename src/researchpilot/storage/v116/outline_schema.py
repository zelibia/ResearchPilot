"""Idempotent P0-05 schema supplements layered on P0-01."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine


CURRENT_REVISION_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_reading_outline_current_revision_guard
BEFORE UPDATE OF current_revision_id, current_revision_no ON reading_outlines
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'current outline revision must belong to the outline and be applied'
    )
    WHERE NOT EXISTS (
        SELECT 1
        FROM outline_revisions r
        WHERE r.id = NEW.current_revision_id
          AND r.outline_id = NEW.id
          AND r.revision_no = NEW.current_revision_no
          AND r.status = 'applied'
    );
END
"""


def deploy_outline_schema(engine: Engine) -> dict[str, Any]:
    """Create the current-pointer integrity trigger.

    Tables are owned by SQLAlchemy metadata so P0-01's additive deployment
    remains the single schema path.  The trigger mirrors P0-05 design DDL and
    prevents callers from pointing current at a candidate or another outline.
    """

    with engine.begin() as conn:
        conn.exec_driver_sql(CURRENT_REVISION_GUARD)
    return {
        "outline_schema": "1.16.1-p0-05",
        "current_revision_guard": True,
    }

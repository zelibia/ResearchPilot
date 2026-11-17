"""Programmatic deployment for P0-02 screening tables."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from ..storage.v116.db import (
    SCHEMA_MIGRATIONS_TABLE,
    engine_for,
    session_factory,
)
from ..storage.v116.orm import now_iso
from .orm import ScreeningBase

SCREENING_SCHEMA_VERSION = "1.16.2-p0-02"


def deploy_screening_schema(engine: Engine, *, note: str = "") -> dict:
    tables = set(sa.inspect(engine).get_table_names())
    required = {
        "projects",
        "library_documents",
        "project_documents",
        "file_assets",
        SCHEMA_MIGRATIONS_TABLE,
    }
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(f"P0-01 schema missing before P0-02 deploy: {missing}")

    before = set(tables)
    ScreeningBase.metadata.create_all(engine)
    after = set(sa.inspect(engine).get_table_names())
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE}(version, applied_at, note) "
                "VALUES (:version, :applied_at, :note) "
                "ON CONFLICT(version) DO UPDATE SET "
                "applied_at=excluded.applied_at, note=excluded.note"
            ),
            {
                "version": SCREENING_SCHEMA_VERSION,
                "applied_at": now_iso(),
                "note": note or "P0-02 screening inbox",
            },
        )
    return {
        "schema_version": SCREENING_SCHEMA_VERSION,
        "created_tables": sorted(
            (after - before) & set(ScreeningBase.metadata.tables)
        ),
        "table_count": len(after),
    }


def screening_engine(db_path: str, *, readonly: bool = False) -> Engine:
    return engine_for(db_path, readonly=readonly)


__all__ = [
    "SCREENING_SCHEMA_VERSION",
    "deploy_screening_schema",
    "screening_engine",
    "session_factory",
]

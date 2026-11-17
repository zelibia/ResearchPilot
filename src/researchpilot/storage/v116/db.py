"""SQLAlchemy engine, schema deployment, and runtime settings for v1.16."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .orm import Base, now_iso

SCHEMA_VERSION = "1.16.1"
SCHEMA_MIGRATIONS_TABLE = "v116_schema_migrations"
RUNTIME_SETTINGS_TABLE = "v116_runtime_settings"
MODEL_VERSION_VALUES = {"v15", "v116_shadow", "v116"}


def engine_for(db_path: str | Path, *, readonly: bool = False) -> Engine:
    path = Path(db_path).expanduser().resolve()
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(path)
        url = sa.engine.URL.create(
            "sqlite",
            database=f"file:{path}",
            query={"mode": "ro", "uri": "true"},
        )
        connect_args: dict = {}
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        url = sa.engine.URL.create("sqlite", database=str(path))
        connect_args = {"check_same_thread": False}
    engine = sa.create_engine(
        url,
        connect_args=connect_args,
        future=True,
    )

    @sa.event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _metadata_tables() -> set[str]:
    return set(Base.metadata.tables)


def deploy_schema(engine: Engine, *, note: str = "") -> dict:
    """Create all v1.16 P0-01 tables incrementally and record the version.

    Legacy tables such as ``projects`` and ``paper_claims`` must already exist.
    Their absence is a migration preflight error rather than something this
    deployment silently invents.
    """
    inspector = sa.inspect(engine)
    existing = set(inspector.get_table_names())
    required_legacy = {"projects", "papers", "paper_claims"}
    missing = sorted(required_legacy - existing)
    if missing:
        raise RuntimeError(f"legacy tables missing before v116 deploy: {missing}")

    before = set(existing)
    Base.metadata.create_all(engine)
    after = set(sa.inspect(engine).get_table_names())
    created = sorted((after - before) & _metadata_tables())

    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {RUNTIME_SETTINGS_TABLE} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            sa.text(
                f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE}(version, applied_at, note) "
                "VALUES (:version, :applied_at, :note) "
                "ON CONFLICT(version) DO UPDATE SET "
                "applied_at=excluded.applied_at, note=excluded.note"
            ),
            {"version": SCHEMA_VERSION, "applied_at": now_iso(), "note": note},
        )
        conn.execute(
            sa.text(
                f"INSERT INTO {RUNTIME_SETTINGS_TABLE}(key, value, updated_at) "
                "VALUES ('literature_model_version', 'v15', :updated_at) "
                "ON CONFLICT(key) DO NOTHING"
            ),
            {"updated_at": now_iso()},
        )
    # P0-04 supplements are deliberately deployed without adding a second
    # schema-version row.  Existing P0-01 acceptance expects one canonical
    # version while P0-04 remains an additive, idempotent extension.
    from .annotation_schema import deploy_annotation_schema
    from .outline_schema import deploy_outline_schema
    from .summary_schema import deploy_summary_schema

    annotation_report = deploy_annotation_schema(engine)
    outline_report = deploy_outline_schema(engine)
    summary_report = deploy_summary_schema(engine)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_tables": created,
        "table_count": len(after),
        "annotation_schema": annotation_report,
        "outline_schema": outline_report,
        "summary_schema": summary_report,
    }


def current_schema_versions(engine: Engine) -> list[str]:
    inspector = sa.inspect(engine)
    if SCHEMA_MIGRATIONS_TABLE not in inspector.get_table_names():
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY applied_at")
        )
        return [str(row[0]) for row in rows]


def get_model_version(engine: Engine) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                f"SELECT value FROM {RUNTIME_SETTINGS_TABLE} "
                "WHERE key='literature_model_version'"
            )
        ).fetchone()
    value = str(row[0]) if row else os.environ.get("LITERATURE_MODEL_VERSION", "v15")
    return value if value in MODEL_VERSION_VALUES else "v15"


def set_model_version(engine: Engine, value: str) -> None:
    if value not in MODEL_VERSION_VALUES:
        raise ValueError(f"unknown literature model version: {value}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {RUNTIME_SETTINGS_TABLE}(key, value, updated_at) "
                "VALUES ('literature_model_version', :value, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at"
            ),
            {"value": value, "updated_at": now_iso()},
        )


def legacy_payload_digest(engine: Engine, tables: tuple[str, ...]) -> dict[str, str]:
    """Hash legacy table payload streams without modifying them."""
    import hashlib

    inspector = sa.inspect(engine)
    existing = set(inspector.get_table_names())
    result: dict[str, str] = {}
    with engine.connect() as conn:
        for table in tables:
            if table not in existing:
                result[table] = ""
                continue
            columns = {item["name"] for item in inspector.get_columns(table)}
            if "payload" in columns:
                rows = conn.execute(
                    sa.text(f"SELECT id, payload FROM {table} ORDER BY id")
                )
                digest = hashlib.sha256()
                count = 0
                for row in rows:
                    digest.update(str(row[0]).encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(str(row[1]).encode("utf-8"))
                    digest.update(b"\n")
                    count += 1
                result[table] = f"{count}:{digest.hexdigest()}"
            else:
                ordered_columns = sorted(columns)
                selected = ", ".join(f'"{name}"' for name in ordered_columns)
                rows = conn.exec_driver_sql(
                    f'SELECT {selected} FROM "{table}"'
                )
                digest = hashlib.sha256()
                count = 0
                for row in rows:
                    digest.update(
                        json.dumps(
                            list(row),
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8")
                    )
                    digest.update(b"\n")
                    count += 1
                result[table] = f"{count}:{digest.hexdigest()}"
    return result


def dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

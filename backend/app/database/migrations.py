"""Additive schema migration.

The project has no Alembic history: the schema has always been created from the
ORM metadata.  Introducing Alembic now would mean hand-writing a baseline
revision for tables that already exist in developers' databases, so instead this
module performs the one operation that is actually needed and is safe on both
SQLite and PostgreSQL:

* create any table that is in the metadata but not in the database;
* add any column that is in the metadata but not in the table.

It never drops or rewrites anything, so existing development data survives.
Column additions are emitted as ``ALTER TABLE ... ADD COLUMN`` with a literal
default, which SQLite supports for non-constant-free defaults only when the
default is a constant -- all the added columns use constants.

What this deliberately does *not* do is alter or drop existing columns, add
constraints to populated tables, or reorder anything.  Those need a real
migration tool; if the schema ever needs that, this is the point to adopt one.
"""
from __future__ import annotations

from sqlalchemy import Engine, inspect, text
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.logging import get_logger
from app.database.models import Base

log = get_logger("database.migrations")


def _sql_default(column) -> str | None:
    """Render a constant server-side default for an added column."""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        # A callable default (e.g. utcnow) cannot be expressed in ALTER TABLE;
        # existing rows get NULL, which the ORM handles on read.
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, (list, dict)):
        return "'[]'" if isinstance(value, list) else "'{}'"
    return None


def migrate(engine: Engine | None = None, *, dry_run: bool = False) -> dict:
    """Bring the database in line with the ORM metadata, additively."""
    from app.database import models_intelligence  # noqa: F401  (registers tables)
    from app.database.session import get_engine

    engine = engine or get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    created: list[str] = []
    added_columns: list[str] = []
    created_indexes: list[str] = []

    with engine.begin() as conn:
        for name, table in Base.metadata.tables.items():
            if name not in existing_tables:
                if not dry_run:
                    conn.execute(CreateTable(table))
                    for index in table.indexes:
                        conn.execute(CreateIndex(index))
                created.append(name)
                continue

            present = {c["name"] for c in inspector.get_columns(name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(engine.dialect)
                clause = f'ALTER TABLE "{name}" ADD COLUMN "{column.name}" {col_type}'
                default = _sql_default(column)
                if default is not None:
                    clause += f" DEFAULT {default}"
                if not dry_run:
                    conn.execute(text(clause))
                added_columns.append(f"{name}.{column.name}")

            # indexes added after a table already existed
            existing_indexes = {i["name"] for i in inspector.get_indexes(name)}
            for index in table.indexes:
                if index.name in existing_indexes:
                    continue
                if not dry_run:
                    conn.execute(CreateIndex(index))
                created_indexes.append(index.name or "")

    result = {
        "created_tables": created,
        "added_columns": added_columns,
        "created_indexes": [i for i in created_indexes if i],
        "dry_run": dry_run,
    }
    if created or added_columns or created_indexes:
        log.info("database.migrated", **result)
    return result

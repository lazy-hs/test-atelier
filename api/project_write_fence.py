"""Fail closed when the project deletion write barrier is only partly installed."""

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .db import Base


def assert_project_write_fence(db: Session) -> None:
    tables = Base.metadata.tables
    existing = set(inspect(db.get_bind()).get_table_names())
    if set(tables) - existing:
        raise RuntimeError("项目写入栅栏不完整：数据库表缺失，请核对迁移")

    owned = {table.name for table in tables.values() if "project_id" in table.c}
    descendants = set(owned)
    while True:
        additional = {table.name for table in tables.values() if table.name not in descendants
                      and any(foreign.column.table.name in descendants for foreign in table.foreign_keys)}
        if not additional:
            break
        descendants.update(additional)

    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        expected = {f"ta_project_fence_{table}_{operation}" for table in descendants
                    for operation in ("insert", "update")}
        installed = set(db.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'ta_project_fence_%'"
        )).scalars())
    elif dialect == "postgresql":
        expected = {f"ta_project_fence_{table}" for table in descendants}
        installed = set(db.execute(text("""
            SELECT DISTINCT trigger_name FROM information_schema.triggers
            WHERE trigger_schema = current_schema() AND trigger_name LIKE 'ta_project_fence_%'
        """)).scalars())
    else:
        raise RuntimeError("项目写入栅栏不支持当前数据库")
    if expected - installed:
        raise RuntimeError("项目写入栅栏不完整：触发器缺失，请核对迁移")

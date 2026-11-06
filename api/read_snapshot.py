"""Short-lived, single-request database read snapshots for evidence views."""

from contextlib import contextmanager
from collections.abc import Iterator

from fastapi import HTTPException
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@contextmanager
def consistent_read(engine: Engine) -> Iterator[Session]:
    if engine.dialect.name not in {"sqlite", "postgresql"}:
        raise HTTPException(501, "当前数据库不支持一致性读取")
    with engine.connect() as connection:
        old_uncommitted = None
        try:
            if engine.dialect.name == "postgresql":
                connection.execution_options(isolation_level="REPEATABLE READ")
            else:
                old_uncommitted = connection.exec_driver_sql("PRAGMA read_uncommitted").scalar()
                if old_uncommitted:
                    connection.exec_driver_sql("PRAGMA read_uncommitted=0")
                connection.exec_driver_sql("BEGIN")
            with Session(bind=connection) as db:
                yield db
        finally:
            if old_uncommitted:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA read_uncommitted=1")

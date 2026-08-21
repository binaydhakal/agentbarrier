"""Secret-safe runtime store selection for operational entry points."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agentbarrier.runtime.postgres import PostgresRuntimeStore
from agentbarrier.runtime.protocol import RuntimeStore
from agentbarrier.runtime.store import SQLiteRuntimeStore

_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@contextmanager
def open_runtime_store(
    *,
    database_path: str | Path | None = None,
    postgres_dsn_env: str | None = None,
    postgres_schema: str = "agentbarrier",
    postgres_create_schema: bool = False,
    postgres_migrate: bool = False,
) -> Iterator[RuntimeStore]:
    """Open exactly one configured backend without accepting a PostgreSQL secret on the CLI."""

    configured = int(database_path is not None) + int(postgres_dsn_env is not None)
    if configured != 1:
        raise ValueError("configure exactly one runtime SQLite path or PostgreSQL DSN environment")
    if database_path is not None:
        with SQLiteRuntimeStore(database_path) as store:
            yield store
        return

    environment_name = postgres_dsn_env or ""
    if _ENVIRONMENT_NAME_PATTERN.fullmatch(environment_name) is None:
        raise ValueError("PostgreSQL DSN environment name is invalid")
    dsn = os.environ.get(environment_name)
    if dsn is None:
        raise ValueError(f"environment variable {environment_name!r} is not set")
    with PostgresRuntimeStore(
        dsn,
        schema=postgres_schema,
        create_schema=postgres_create_schema,
        migrate=postgres_migrate,
    ) as store:
        yield store

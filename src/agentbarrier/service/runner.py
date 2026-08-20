"""Operational runner for the authenticated approval API."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from agentbarrier.runtime import SQLiteRuntimeStore
from agentbarrier.service.api import create_approval_app
from agentbarrier.service.auth import StaticBearerAuth


def run_approval_api(
    *,
    database_path: str | Path,
    auth_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Run the approval API with safe loopback defaults until shutdown."""

    if not host.strip():
        raise ValueError("approval API host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("approval API port must be between 1 and 65535")
    auth = StaticBearerAuth.from_file(auth_path)
    with SQLiteRuntimeStore(database_path) as store:
        app = create_approval_app(store=store, auth=auth)
        uvicorn.run(app, host=host, port=port, log_level="info")

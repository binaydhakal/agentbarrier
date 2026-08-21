"""Operational runner for the authenticated approval API."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import anyio
import uvicorn

from agentbarrier.runtime import open_runtime_store
from agentbarrier.service.api import create_approval_app
from agentbarrier.service.auth import StaticBearerAuth
from agentbarrier.service.dashboard import DashboardSessionStore, create_dashboard_app
from agentbarrier.service.webhooks import (
    WebhookConfig,
    WebhookDeliverySnapshot,
    WebhookDeliveryStore,
    WebhookWorker,
)


def run_approval_api(
    *,
    database_path: str | Path | None,
    auth_path: str | Path,
    postgres_dsn_env: str | None = None,
    postgres_schema: str = "agentbarrier",
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Run the approval API with safe loopback defaults until shutdown."""

    if not host.strip():
        raise ValueError("approval API host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("approval API port must be between 1 and 65535")
    auth = StaticBearerAuth.from_file(auth_path)
    with open_runtime_store(
        database_path=database_path,
        postgres_dsn_env=postgres_dsn_env,
        postgres_schema=postgres_schema,
    ) as store:
        app = create_approval_app(store=store, auth=auth)
        uvicorn.run(app, host=host, port=port, log_level="info")


def run_approval_dashboard(
    *,
    database_path: str | Path | None,
    auth_path: str | Path,
    postgres_dsn_env: str | None = None,
    postgres_schema: str = "agentbarrier",
    host: str = "127.0.0.1",
    port: int = 8788,
    public_origin: str | None = None,
    cookie_secure: bool = False,
    session_ttl_seconds: float = 8 * 60 * 60,
) -> None:
    """Run the server-rendered dashboard with loopback-safe defaults."""

    if not host.strip():
        raise ValueError("approval dashboard host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("approval dashboard port must be between 1 and 65535")
    if not _is_loopback_host(host) and (not cookie_secure or public_origin is None):
        raise ValueError(
            "non-loopback dashboard listeners require --cookie-secure and --public-origin"
        )
    if database_path is not None:
        _require_existing_file(database_path, label="runtime database")
    auth = StaticBearerAuth.from_file(auth_path)
    sessions = DashboardSessionStore(ttl_seconds=session_ttl_seconds)
    with open_runtime_store(
        database_path=database_path,
        postgres_dsn_env=postgres_dsn_env,
        postgres_schema=postgres_schema,
    ) as store:
        app = create_dashboard_app(
            store=store,
            auth=auth,
            sessions=sessions,
            cookie_secure=cookie_secure,
            public_origin=public_origin,
        )
        uvicorn.run(app, host=host, port=port, log_level="info")


def run_webhook_worker(
    *,
    database_path: str | Path | None,
    state_path: str | Path,
    config_path: str | Path,
    postgres_dsn_env: str | None = None,
    postgres_schema: str = "agentbarrier",
    once: bool = False,
    poll_interval_seconds: float = 1,
) -> dict[str, int] | None:
    """Run one durable webhook pass or poll continuously until shutdown."""

    if database_path is not None:
        _require_existing_file(database_path, label="runtime database")
    config = WebhookConfig.from_file(config_path)
    with (
        open_runtime_store(
            database_path=database_path,
            postgres_dsn_env=postgres_dsn_env,
            postgres_schema=postgres_schema,
        ) as runtime_store,
        WebhookDeliveryStore(state_path) as delivery_store,
    ):
        worker = WebhookWorker(
            runtime_store=runtime_store,
            delivery_store=delivery_store,
            config=config,
        )
        if once:
            return anyio.run(worker.run_once)
        anyio.run(_run_webhook_forever, worker, poll_interval_seconds)
    return None


async def _run_webhook_forever(worker: WebhookWorker, poll_interval_seconds: float) -> None:
    await worker.run_forever(poll_interval_seconds=poll_interval_seconds)


def webhook_delivery_status(state_path: str | Path) -> tuple[WebhookDeliverySnapshot, ...]:
    """Read durable webhook status without loading endpoint secrets."""

    _require_existing_file(state_path, label="webhook state database")
    with WebhookDeliveryStore(state_path) as store:
        return store.snapshots()


def retry_webhook_delivery(
    state_path: str | Path,
    *,
    endpoint_id: str,
    event_id: str,
) -> WebhookDeliverySnapshot:
    """Requeue one exact dead webhook delivery for another bounded run."""

    _require_existing_file(state_path, label="webhook state database")
    with WebhookDeliveryStore(state_path) as store:
        return store.retry_dead(endpoint_id=endpoint_id, event_id=event_id)


def _require_existing_file(path: str | Path, *, label: str) -> None:
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} does not exist: {resolved}")


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False

"""Operational stdio and Streamable HTTP runners for the MCP gateway."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from starlette.types import ASGIApp

from agentbarrier import __version__
from agentbarrier.mcp.auth import MCPBearerAuthMiddleware
from agentbarrier.mcp.gateway import MCPClientFactory, MCPGateway, argument_idempotency_key
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, RuntimeStore, open_runtime_store
from agentbarrier.service.auth import StaticBearerAuth, hash_bearer_token

DEFAULT_MCP_REQUEST_BYTES = 1024 * 1024
MAX_MCP_REQUEST_BYTES = 16 * 1024 * 1024
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class MCPGatewayConfig:
    """Validated configuration shared by stdio and HTTP gateway runners."""

    policy_path: Path
    database_path: Path | None
    namespace: str
    postgres_dsn_env: str | None = None
    postgres_schema: str = "agentbarrier"
    upstream_url: str | None = None
    upstream_command: str | None = None
    upstream_args: tuple[str, ...] = ()
    upstream_timeout_seconds: float | None = None
    upstream_bearer_token_env: str | None = None
    idempotency_argument: str | None = None
    organization_id: str = "default"
    requested_by: str | None = None

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ValueError("MCP gateway namespace must not be empty")
        if not self.organization_id.strip():
            raise ValueError("MCP gateway organization must not be empty")
        if self.requested_by is not None and not self.requested_by.strip():
            raise ValueError("MCP gateway requester must not be empty when provided")
        if self.organization_id != "default" and self.requested_by is None:
            raise ValueError("an organization-scoped MCP gateway requires --requested-by")
        databases = int(self.database_path is not None) + int(self.postgres_dsn_env is not None)
        if databases != 1:
            raise ValueError("configure exactly one MCP runtime database backend")
        if (
            self.postgres_dsn_env is not None
            and _ENVIRONMENT_NAME_PATTERN.fullmatch(self.postgres_dsn_env) is None
        ):
            raise ValueError("PostgreSQL DSN environment name is invalid")
        targets = int(self.upstream_url is not None) + int(self.upstream_command is not None)
        if targets != 1:
            raise ValueError("configure exactly one of upstream_url or upstream_command")
        if self.upstream_url is not None and not self.upstream_url.strip():
            raise ValueError("upstream_url must not be empty")
        if self.upstream_url is not None:
            _validate_upstream_url(self.upstream_url)
        if self.upstream_command is not None and not self.upstream_command.strip():
            raise ValueError("upstream_command must not be empty")
        if self.upstream_url is not None and self.upstream_args:
            raise ValueError("upstream_args are valid only with upstream_command")
        if self.upstream_bearer_token_env is not None:
            if self.upstream_url is None:
                raise ValueError("upstream bearer authentication requires upstream_url")
            if _ENVIRONMENT_NAME_PATTERN.fullmatch(self.upstream_bearer_token_env) is None:
                raise ValueError("upstream bearer token environment name is invalid")
        if self.upstream_timeout_seconds is not None and self.upstream_timeout_seconds <= 0:
            raise ValueError("upstream_timeout_seconds must be greater than zero")
        if self.idempotency_argument is not None:
            argument_idempotency_key(self.idempotency_argument)


def run_stdio_gateway(config: MCPGatewayConfig) -> None:
    """Serve the policy gateway over stdio until the client disconnects."""

    anyio.run(_serve_stdio_gateway, config)


async def _serve_stdio_gateway(config: MCPGatewayConfig) -> None:
    with open_runtime_store(
        database_path=config.database_path,
        postgres_dsn_env=config.postgres_dsn_env,
        postgres_schema=config.postgres_schema,
    ) as store:
        gateway = _build_gateway(config, store)
        async with stdio_server() as (read_stream, write_stream):
            await gateway.server.run(
                read_stream,
                write_stream,
                gateway.server.create_initialization_options(),
            )


def run_http_gateway(
    config: MCPGatewayConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    auth_path: str | Path | None = None,
    max_request_body_size: int = DEFAULT_MCP_REQUEST_BYTES,
) -> None:
    """Serve the policy gateway over Streamable HTTP until shutdown."""

    if not 1 <= port <= 65535:
        raise ValueError("HTTP gateway port must be between 1 and 65535")
    auth = StaticBearerAuth.from_file(auth_path) if auth_path is not None else None
    with open_runtime_store(
        database_path=config.database_path,
        postgres_dsn_env=config.postgres_dsn_env,
        postgres_schema=config.postgres_schema,
    ) as store:
        gateway = _build_gateway(config, store)
        app = create_http_gateway_app(
            gateway,
            host=host,
            path=path,
            auth=auth,
            max_request_body_size=max_request_body_size,
        )
        uvicorn.run(app, host=host, port=port, log_level="info")


def create_http_gateway_app(
    gateway: MCPGateway,
    *,
    host: str = "127.0.0.1",
    path: str = "/mcp",
    auth: StaticBearerAuth | None = None,
    max_request_body_size: int = DEFAULT_MCP_REQUEST_BYTES,
) -> ASGIApp:
    """Create a validated Streamable HTTP gateway app for a service runner."""

    if not host.strip():
        raise ValueError("HTTP gateway host must not be empty")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("HTTP gateway path must be an absolute path without query or fragment")
    if not 1024 <= max_request_body_size <= MAX_MCP_REQUEST_BYTES:
        raise ValueError(
            f"HTTP gateway request limit must be between 1024 and {MAX_MCP_REQUEST_BYTES} bytes"
        )
    if auth is None and not _is_loopback_host(host):
        raise ValueError("non-loopback MCP HTTP listeners require --auth-config")
    app: ASGIApp = gateway.server.streamable_http_app(
        streamable_http_path=path,
        host=host,
        max_request_body_size=max_request_body_size,
    )
    if auth is not None:
        app = MCPBearerAuthMiddleware(app, auth)
    return app


def _build_gateway(config: MCPGatewayConfig, store: RuntimeStore) -> MCPGateway:
    policy = RuntimePolicy.from_file(config.policy_path)
    barrier = RuntimeBarrier(
        policy=policy,
        store=store,
        namespace=config.namespace,
        organization_id=config.organization_id,
        requested_by=config.requested_by,
    )
    client_factory = _client_factory(config)
    resolver = (
        argument_idempotency_key(config.idempotency_argument)
        if config.idempotency_argument is not None
        else None
    )
    if resolver is not None:
        return MCPGateway(
            barrier=barrier,
            client_factory=client_factory,
            idempotency_resolver=resolver,
            version=__version__,
        )
    return MCPGateway(
        barrier=barrier,
        client_factory=client_factory,
        version=__version__,
    )


def _client_factory(config: MCPGatewayConfig) -> MCPClientFactory:
    if config.upstream_url is not None:
        url = config.upstream_url

        def http_client() -> Client:
            token: str | None = None
            if config.upstream_bearer_token_env is not None:
                token = os.environ.get(config.upstream_bearer_token_env)
                if token is None:
                    raise ValueError(
                        f"environment variable {config.upstream_bearer_token_env!r} is not set"
                    )
                hash_bearer_token(token)
            return Client(
                _streamable_http_transport(
                    url,
                    bearer_token=token,
                    read_timeout_seconds=config.upstream_timeout_seconds,
                ),
                cache=None,
                read_timeout_seconds=config.upstream_timeout_seconds,
            )

        return http_client

    command = config.upstream_command
    if command is None:  # pragma: no cover - configuration validation is exhaustive
        raise RuntimeError("MCP gateway upstream was not configured")
    parameters = StdioServerParameters(command=command, args=list(config.upstream_args))

    def subprocess_client() -> Client:
        return Client(
            stdio_client(parameters),
            cache=None,
            read_timeout_seconds=config.upstream_timeout_seconds,
        )

    return subprocess_client


@asynccontextmanager
async def _streamable_http_transport(
    url: str,
    *,
    bearer_token: str | None,
    read_timeout_seconds: float | None,
) -> AsyncIterator[Any]:
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token is not None else None
    timeout = httpx2.Timeout(30.0, read=read_timeout_seconds or 300.0)
    async with (
        httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        ) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
    ):
        yield streams


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_upstream_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("upstream_url must be a valid HTTP or HTTPS URL") from error
    if parsed.scheme not in {"http", "https"} or hostname is None or port == 0:
        raise ValueError("upstream_url must be a valid HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("upstream_url must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("upstream_url must not contain a fragment")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise ValueError("non-loopback upstream_url must use HTTPS")

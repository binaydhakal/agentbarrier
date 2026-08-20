"""Operational stdio and Streamable HTTP runners for the MCP gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import anyio
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.stdio import stdio_server

from agentbarrier import __version__
from agentbarrier.mcp.gateway import MCPClientFactory, MCPGateway, argument_idempotency_key
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, SQLiteRuntimeStore


@dataclass(frozen=True, slots=True)
class MCPGatewayConfig:
    """Validated configuration shared by stdio and HTTP gateway runners."""

    policy_path: Path
    database_path: Path
    namespace: str
    upstream_url: str | None = None
    upstream_command: str | None = None
    upstream_args: tuple[str, ...] = ()
    upstream_timeout_seconds: float | None = None
    idempotency_argument: str | None = None

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ValueError("MCP gateway namespace must not be empty")
        targets = int(self.upstream_url is not None) + int(self.upstream_command is not None)
        if targets != 1:
            raise ValueError("configure exactly one of upstream_url or upstream_command")
        if self.upstream_url is not None and not self.upstream_url.strip():
            raise ValueError("upstream_url must not be empty")
        if self.upstream_command is not None and not self.upstream_command.strip():
            raise ValueError("upstream_command must not be empty")
        if self.upstream_url is not None and self.upstream_args:
            raise ValueError("upstream_args are valid only with upstream_command")
        if self.upstream_timeout_seconds is not None and self.upstream_timeout_seconds <= 0:
            raise ValueError("upstream_timeout_seconds must be greater than zero")
        if self.idempotency_argument is not None:
            argument_idempotency_key(self.idempotency_argument)


def run_stdio_gateway(config: MCPGatewayConfig) -> None:
    """Serve the policy gateway over stdio until the client disconnects."""

    anyio.run(_serve_stdio_gateway, config)


async def _serve_stdio_gateway(config: MCPGatewayConfig) -> None:
    with SQLiteRuntimeStore(config.database_path) as store:
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
) -> None:
    """Serve the policy gateway over Streamable HTTP until shutdown."""

    if not host.strip():
        raise ValueError("HTTP gateway host must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("HTTP gateway port must be between 1 and 65535")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("HTTP gateway path must be an absolute path without query or fragment")
    with SQLiteRuntimeStore(config.database_path) as store:
        gateway = _build_gateway(config, store)
        app = gateway.server.streamable_http_app(
            streamable_http_path=path,
            host=host,
        )
        uvicorn.run(app, host=host, port=port, log_level="info")


def _build_gateway(config: MCPGatewayConfig, store: SQLiteRuntimeStore) -> MCPGateway:
    policy = RuntimePolicy.from_file(config.policy_path)
    barrier = RuntimeBarrier(
        policy=policy,
        store=store,
        namespace=config.namespace,
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
            return Client(
                url,
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

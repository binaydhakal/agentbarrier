"""MCP runtime gateway integration.

Install ``agentbarrier[mcp]`` to use this optional module.
"""

from agentbarrier.mcp.gateway import (
    AGENTBARRIER_ACTION_META_KEY,
    AGENTBARRIER_ERROR_META_KEY,
    AGENTBARRIER_IDEMPOTENCY_META_KEY,
    MCPClientFactory,
    MCPGateway,
    MCPIdempotencyResolver,
    argument_idempotency_key,
    meta_idempotency_key,
)
from agentbarrier.mcp.runner import (
    DEFAULT_MCP_REQUEST_BYTES,
    MAX_MCP_REQUEST_BYTES,
    MCPGatewayConfig,
    create_http_gateway_app,
    run_http_gateway,
    run_stdio_gateway,
)

__all__ = [
    "AGENTBARRIER_ACTION_META_KEY",
    "AGENTBARRIER_ERROR_META_KEY",
    "AGENTBARRIER_IDEMPOTENCY_META_KEY",
    "DEFAULT_MCP_REQUEST_BYTES",
    "MAX_MCP_REQUEST_BYTES",
    "MCPClientFactory",
    "MCPGateway",
    "MCPGatewayConfig",
    "MCPIdempotencyResolver",
    "argument_idempotency_key",
    "create_http_gateway_app",
    "meta_idempotency_key",
    "run_http_gateway",
    "run_stdio_gateway",
]

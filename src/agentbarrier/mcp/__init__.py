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

__all__ = [
    "AGENTBARRIER_ACTION_META_KEY",
    "AGENTBARRIER_ERROR_META_KEY",
    "AGENTBARRIER_IDEMPOTENCY_META_KEY",
    "MCPClientFactory",
    "MCPGateway",
    "MCPIdempotencyResolver",
    "argument_idempotency_key",
    "meta_idempotency_key",
]

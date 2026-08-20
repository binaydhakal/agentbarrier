"""Policy-enforcing MCP tool gateway built on the official MCP SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from mcp import Client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)

from agentbarrier.errors import ActionBindingError, RuntimeActionError
from agentbarrier.models import JsonValue
from agentbarrier.runtime import RuntimeBarrier

AGENTBARRIER_IDEMPOTENCY_META_KEY = "agentbarrier/idempotencyKey"
"""MCP request metadata key carrying stable business-operation identity."""

AGENTBARRIER_ACTION_META_KEY = "agentbarrier/action"
"""MCP result metadata key describing an enforced runtime action."""

AGENTBARRIER_ERROR_META_KEY = "agentbarrier/error"
"""MCP result metadata key describing a gateway request error."""

MCPClientFactory = Callable[[], Client]
MCPIdempotencyResolver = Callable[
    [str, Mapping[str, JsonValue], Mapping[str, object] | None],
    str,
]


def meta_idempotency_key(
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    meta: Mapping[str, object] | None,
) -> str:
    """Read a stable idempotency key from AgentBarrier MCP request metadata."""

    del tool_name, arguments
    raw = meta.get(AGENTBARRIER_IDEMPOTENCY_META_KEY) if meta is not None else None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"MCP tool calls must include a non-empty {AGENTBARRIER_IDEMPOTENCY_META_KEY!r} "
            "string in params._meta"
        )
    return raw


def argument_idempotency_key(path: str) -> MCPIdempotencyResolver:
    """Build a resolver that reads a dotted path from tool arguments."""

    segments = tuple(path.split("."))
    if not path.strip() or any(not segment for segment in segments):
        raise ValueError("idempotency argument path must contain non-empty segments")

    def resolve(
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        meta: Mapping[str, object] | None,
    ) -> str:
        del tool_name, meta
        value: object = arguments
        for segment in segments:
            if not isinstance(value, Mapping) or segment not in value:
                raise ValueError(f"idempotency argument path {path!r} was not found")
            value = value[segment]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"idempotency argument path {path!r} must resolve to a string")
        return value

    return resolve


@dataclass(frozen=True, slots=True)
class _GatewayState:
    client: Client


class MCPGateway:
    """Expose one upstream MCP server through AgentBarrier runtime enforcement.

    The gateway forwards tool discovery and complete tool results through the official MCP SDK.
    Every ``tools/call`` must carry a stable business idempotency key, either in AgentBarrier's
    request metadata field or through a configured resolver. Calls that require a reviewer return
    an MCP tool error with a durable action identifier and never reach the upstream server.
    """

    def __init__(
        self,
        *,
        barrier: RuntimeBarrier,
        client_factory: MCPClientFactory,
        idempotency_resolver: MCPIdempotencyResolver = meta_idempotency_key,
        name: str = "agentbarrier-gateway",
        version: str = "",
    ) -> None:
        if not name.strip():
            raise ValueError("MCP gateway name must not be empty")
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if not callable(idempotency_resolver):
            raise TypeError("idempotency_resolver must be callable")
        self.barrier = barrier
        self._client_factory = client_factory
        self._idempotency_resolver = idempotency_resolver
        self.server: Server[_GatewayState] = Server(
            name,
            version=version,
            description=(
                "AgentBarrier policy and human-approval gateway for consequential MCP tools."
            ),
            lifespan=self._lifespan,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @asynccontextmanager
    async def _lifespan(self, server: Server[_GatewayState]) -> AsyncIterator[_GatewayState]:
        del server
        client = self._client_factory()
        if not isinstance(client, Client):
            raise TypeError("client_factory must return an mcp.Client")
        async with client:
            yield _GatewayState(client=client)

    async def _list_tools(
        self,
        context: ServerRequestContext[_GatewayState],
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        cursor = params.cursor if params is not None else None
        meta = params.meta if params is not None else None
        return await context.lifespan_context.client.list_tools(cursor=cursor, meta=meta)

    async def _call_tool(
        self,
        context: ServerRequestContext[_GatewayState],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        arguments = self._normalize_arguments(params.arguments)
        meta = cast(Mapping[str, object] | None, params.meta)
        try:
            idempotency_key = self._idempotency_resolver(params.name, arguments, meta)
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError("MCP idempotency resolver must return a non-empty string")
        except (TypeError, ValueError) as error:
            return self._request_error(str(error), code="invalid_idempotency_key")

        async def forward() -> dict[str, Any]:
            result = await context.lifespan_context.client.call_tool(
                params.name,
                dict(arguments),
                progress_callback=context.session.report_progress,
                input_responses=params.input_responses,
                request_state=params.request_state,
                meta=params.meta,
            )
            return result.model_dump(by_alias=True, mode="json", exclude_none=False)

        try:
            payload = await self.barrier.execute_async(
                tool_name=params.name,
                arguments=arguments,
                idempotency_key=idempotency_key,
                operation=forward,
            )
        except ActionBindingError:
            return self._request_error(
                "the idempotency key is already bound to a different exact tool request",
                code="action_binding_error",
            )
        except RuntimeActionError as error:
            return self._action_error(error)
        return CallToolResult.model_validate(payload, by_name=False)

    @staticmethod
    def _normalize_arguments(arguments: dict[str, Any] | None) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], arguments or {})

    @staticmethod
    def _request_error(message: str, *, code: str) -> CallToolResult:
        return CallToolResult(
            content=[
                TextContent(type="text", text=f"AgentBarrier blocked the tool call: {message}")
            ],
            is_error=True,
            _meta={AGENTBARRIER_ERROR_META_KEY: {"code": code}},
        )

    @staticmethod
    def _action_error(error: RuntimeActionError) -> CallToolResult:
        action = error.action
        if action.status.value == "pending":
            message = (
                f"Approval is required for action {action.action_id}. Do not retry until an "
                "operator approves it; then retry with the same arguments and idempotency key."
            )
        else:
            message = f"AgentBarrier blocked action {action.action_id}: {error}"
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            is_error=True,
            _meta={
                AGENTBARRIER_ACTION_META_KEY: {
                    "actionId": action.action_id,
                    "requestDigest": action.request_digest,
                    "status": action.status.value,
                    "policyRule": action.policy_rule,
                    "policyVersion": action.policy_version,
                }
            },
        )

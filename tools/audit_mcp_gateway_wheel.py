"""Audit an installed wheel through a real MCP gateway lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from agentbarrier.mcp import AGENTBARRIER_ACTION_META_KEY, MCPGateway, argument_idempotency_key
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed through the installed MCP gateway."""

    async def exercise() -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        calls: list[dict[str, Any]] = []

        async def list_tools(
            context: ServerRequestContext[dict[str, Any]],
            params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            del context, params
            return ListToolsResult(
                tools=[
                    Tool(
                        name="payments.refund",
                        description="Refund one exact payment request",
                        input_schema={"type": "object"},
                    )
                ]
            )

        async def call_tool(
            context: ServerRequestContext[dict[str, Any]],
            params: CallToolRequestParams,
        ) -> CallToolResult:
            del context
            arguments = params.arguments or {}
            calls.append(arguments)
            return CallToolResult(
                content=[TextContent(type="text", text="refunded")],
                structured_content={"arguments": arguments, "refunded": True},
            )

        upstream = Server(
            "wheel-audit-upstream",
            version="1",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        policy = RuntimePolicy(
            version="mcp-wheel-audit-v1",
            rules=(
                PolicyRule(
                    "review refunds",
                    PolicyEffect.REQUIRE_APPROVAL,
                    tool="payments.refund",
                    approval_ttl_seconds=60,
                ),
            ),
        )
        database = directory / "runtime.db"
        arguments = {"request_id": "refund-wheel-1", "amount": 25}

        with SQLiteRuntimeStore(database) as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(
                    policy=policy,
                    store=store,
                    namespace="mcp-wheel-audit",
                ),
                client_factory=lambda: Client(upstream, cache=None),
                idempotency_resolver=argument_idempotency_key("request_id"),
            )
            async with Client(gateway.server, cache=None) as client:
                listed = await client.list_tools()
                if [tool.name for tool in listed.tools] != ["payments.refund"]:
                    raise AssertionError("installed MCP gateway did not forward tool discovery")

                pending = await client.call_tool("payments.refund", arguments)
                if not pending.is_error or pending.meta is None:
                    raise AssertionError("installed MCP gateway did not hold the action")
                action = pending.meta.get(AGENTBARRIER_ACTION_META_KEY)
                if not isinstance(action, dict) or action.get("status") != "pending":
                    raise AssertionError("installed MCP gateway returned invalid pending metadata")
                action_id = action.get("actionId")
                if not isinstance(action_id, str):
                    raise AssertionError("installed MCP gateway omitted the action identifier")
                if calls:
                    raise AssertionError("MCP upstream executed before approval")

                store.decide(
                    action_id,
                    Decision.APPROVE,
                    decided_by="wheel-audit",
                    reason="clean-install MCP verification",
                )
                executed = await client.call_tool("payments.refund", arguments)
                replayed = await client.call_tool("payments.refund", arguments)

            if executed.model_dump(mode="json", by_alias=True) != replayed.model_dump(
                mode="json", by_alias=True
            ):
                raise AssertionError("installed MCP gateway replay changed the result")
            if calls != [arguments]:
                raise AssertionError(f"expected one MCP upstream effect, observed {len(calls)}")
            runtime_action = store.get_action(action_id)
            if runtime_action.status is not RuntimeStatus.SUCCEEDED:
                raise AssertionError(
                    f"expected succeeded action, observed {runtime_action.status.value}"
                )
            if not store.verify_receipt_chain():
                raise AssertionError("installed MCP gateway receipt chain is invalid")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            expected_events = [
                "approval_requested",
                "approved",
                "execution_started",
                "execution_succeeded",
                "result_replayed",
            ]
            if events != expected_events:
                raise AssertionError(f"unexpected installed MCP gateway events: {events}")

        return {
            "action_id": action_id,
            "effect_count": len(calls),
            "events": events,
            "status": "passed",
        }

    return asyncio.run(exercise())


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(options.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="agentbarrier-mcp-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

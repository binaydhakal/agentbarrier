"""Deterministic stdio MCP server used by gateway end-to-end tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import anyio
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)


def build_server(ledger: Path) -> Server[dict[str, object]]:
    async def list_tools(
        context: ServerRequestContext[dict[str, object]],
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        del context, params
        return ListToolsResult(
            tools=[
                Tool(
                    name="payments.refund",
                    description="Record one deterministic refund effect",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "amount": {"type": "integer"},
                        },
                        "required": ["request_id", "amount"],
                        "additionalProperties": False,
                    },
                )
            ]
        )

    async def call_tool(
        context: ServerRequestContext[dict[str, object]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        del context
        arguments = params.arguments or {}
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"{encoded}\n")
        return CallToolResult(
            content=[TextContent(type="text", text="refund recorded")],
            structured_content={"recorded": arguments},
        )

    return Server(
        "agentbarrier-e2e-upstream",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve(ledger: Path) -> None:
    server = build_server(ledger)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    arguments = parser.parse_args()
    anyio.run(serve, arguments.ledger)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from agentbarrier.mcp import (
    AGENTBARRIER_ACTION_META_KEY,
    AGENTBARRIER_ERROR_META_KEY,
    AGENTBARRIER_IDEMPOTENCY_META_KEY,
    MCPGateway,
    argument_idempotency_key,
)
from agentbarrier.mcp.runner import MCPGatewayConfig
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        "gateway-policy-v1",
        (
            PolicyRule(
                "review large refunds",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
            ),
            PolicyRule("allow small refunds", PolicyEffect.ALLOW, tool="payments.refund"),
            PolicyRule("deny deletes", PolicyEffect.DENY, tool="database.delete"),
        ),
    )


def make_upstream(
    calls: list[tuple[str, dict[str, Any]]],
    progress: bool = False,
) -> Server[dict[str, Any]]:
    async def list_tools(
        context: ServerRequestContext[dict[str, Any]],
        params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        del context, params
        return ListToolsResult(
            tools=[
                Tool(
                    name="payments.refund",
                    description="Issue a refund",
                    input_schema={"type": "object"},
                ),
                Tool(
                    name="database.delete",
                    description="Delete a record",
                    input_schema={"type": "object"},
                ),
            ],
            ttl_ms=500,
            cache_scope="private",
        )

    async def call_tool(
        context: ServerRequestContext[dict[str, Any]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        arguments = params.arguments or {}
        calls.append((params.name, arguments))
        if progress:
            await context.session.report_progress(1, 2, "upstream halfway")
        return CallToolResult(
            content=[TextContent(type="text", text="completed")],
            structured_content={"tool": params.name, "arguments": arguments},
            meta={"upstream/request": "preserved"},
        )

    return Server(
        "test-upstream",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def test_mcp_gateway_forwards_discovery_and_replays_allowed_call(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        upstream = make_upstream(calls)
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(
                    policy=make_policy(),
                    store=store,
                    namespace="support-mcp",
                ),
                client_factory=lambda: Client(upstream, cache=None),
                version="0.5.0.dev0",
            )
            async with Client(gateway.server, cache=None) as client:
                tools = await client.list_tools()
                assert [tool.name for tool in tools.tools] == [
                    "payments.refund",
                    "database.delete",
                ]
                assert tools.ttl_ms == 500
                assert tools.cache_scope == "private"

                meta = {AGENTBARRIER_IDEMPOTENCY_META_KEY: "refund-small-1"}
                arguments = {"request_id": "refund-small-1", "amount": 10}
                first = await client.call_tool("payments.refund", arguments, meta=meta)
                second = await client.call_tool("payments.refund", arguments, meta=meta)

            assert first.is_error is False
            assert first.structured_content == {
                "tool": "payments.refund",
                "arguments": arguments,
            }
            assert first.meta is not None
            assert first.meta["upstream/request"] == "preserved"
            assert second.model_dump(by_alias=True, mode="json") == first.model_dump(
                by_alias=True,
                mode="json",
            )
            assert calls == [("payments.refund", arguments)]
            assert store.list_actions()[0].namespace == "support-mcp"

    asyncio.run(run())


def test_mcp_gateway_holds_for_approval_then_executes_exact_call(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        upstream = make_upstream(calls)
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            meta = {AGENTBARRIER_IDEMPOTENCY_META_KEY: "refund-large-1"}
            arguments = {"request_id": "refund-large-1", "amount": 100}
            async with Client(gateway.server, cache=None) as client:
                pending = await client.call_tool("payments.refund", arguments, meta=meta)
                assert pending.is_error is True
                assert calls == []
                assert pending.meta is not None
                action_meta = pending.meta[AGENTBARRIER_ACTION_META_KEY]
                assert isinstance(action_meta, dict)
                assert action_meta["status"] == "pending"
                action_id = action_meta["actionId"]
                assert isinstance(action_id, str)

                mismatched = await client.call_tool(
                    "payments.refund",
                    {"request_id": "refund-large-1", "amount": 101},
                    meta=meta,
                )
                assert mismatched.is_error is True
                assert mismatched.meta is not None
                assert mismatched.meta[AGENTBARRIER_ERROR_META_KEY] == {
                    "code": "action_binding_error"
                }

                store.decide(action_id, Decision.APPROVE, decided_by="finance-reviewer")
                completed = await client.call_tool("payments.refund", arguments, meta=meta)
                replayed = await client.call_tool("payments.refund", arguments, meta=meta)

            assert completed.is_error is False
            assert replayed.structured_content == completed.structured_content
            assert calls == [("payments.refund", arguments)]

    asyncio.run(run())


def test_mcp_gateway_replays_after_downstream_reconnect(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        upstream = make_upstream(calls)
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            meta = {AGENTBARRIER_IDEMPOTENCY_META_KEY: "reconnect-1"}
            arguments = {"request_id": "reconnect-1", "amount": 10}
            async with Client(gateway.server, cache=None) as first_client:
                first = await first_client.call_tool("payments.refund", arguments, meta=meta)
            async with Client(gateway.server, cache=None) as second_client:
                second = await second_client.call_tool("payments.refund", arguments, meta=meta)

            assert first.structured_content == second.structured_content
            assert calls == [("payments.refund", arguments)]

    asyncio.run(run())


def test_mcp_gateway_holds_duplicate_while_one_worker_executes(tmp_path: Path) -> None:
    async def run() -> None:
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def list_tools(
            context: ServerRequestContext[dict[str, Any]],
            params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            del context, params
            return ListToolsResult(
                tools=[Tool(name="payments.refund", input_schema={"type": "object"})]
            )

        async def call_tool(
            context: ServerRequestContext[dict[str, Any]],
            params: CallToolRequestParams,
        ) -> CallToolResult:
            nonlocal calls
            del context, params
            calls += 1
            started.set()
            await release.wait()
            return CallToolResult(content=[TextContent(type="text", text="completed")])

        upstream = Server(
            "slow-upstream",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            meta = {AGENTBARRIER_IDEMPOTENCY_META_KEY: "concurrent-1"}
            arguments = {"request_id": "concurrent-1", "amount": 10}
            async with Client(gateway.server, cache=None) as client:
                first_task = asyncio.create_task(
                    client.call_tool("payments.refund", arguments, meta=meta)
                )
                await started.wait()
                duplicate = await client.call_tool("payments.refund", arguments, meta=meta)
                assert duplicate.is_error is True
                assert duplicate.meta is not None
                action_meta = duplicate.meta[AGENTBARRIER_ACTION_META_KEY]
                assert isinstance(action_meta, dict)
                assert action_meta["status"] == "executing"

                release.set()
                completed = await first_task

            assert completed.is_error is False
            assert calls == 1

    asyncio.run(run())


def test_mcp_gateway_cancellation_marks_outcome_unknown_and_cancels_upstream(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        started = asyncio.Event()
        upstream_cancelled = asyncio.Event()

        async def list_tools(
            context: ServerRequestContext[dict[str, Any]],
            params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            del context, params
            return ListToolsResult(
                tools=[Tool(name="payments.refund", input_schema={"type": "object"})]
            )

        async def call_tool(
            context: ServerRequestContext[dict[str, Any]],
            params: CallToolRequestParams,
        ) -> CallToolResult:
            del context, params
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upstream_cancelled.set()
                raise
            raise AssertionError("unreachable")

        upstream = Server(
            "cancellable-upstream",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            async with Client(gateway.server, cache=None) as client:
                task = asyncio.create_task(
                    client.call_tool(
                        "payments.refund",
                        {"request_id": "cancel-1", "amount": 10},
                        meta={AGENTBARRIER_IDEMPOTENCY_META_KEY: "cancel-1"},
                    )
                )
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            assert upstream_cancelled.is_set()
            assert store.list_actions()[0].status is RuntimeStatus.UNKNOWN

    asyncio.run(run())


def test_mcp_gateway_preserves_upstream_protocol_error_then_fails_closed(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        calls = 0

        async def list_tools(
            context: ServerRequestContext[dict[str, Any]],
            params: PaginatedRequestParams | None,
        ) -> ListToolsResult:
            del context, params
            return ListToolsResult(
                tools=[Tool(name="payments.refund", input_schema={"type": "object"})]
            )

        async def call_tool(
            context: ServerRequestContext[dict[str, Any]],
            params: CallToolRequestParams,
        ) -> CallToolResult:
            nonlocal calls
            del context, params
            calls += 1
            raise MCPError(code=-32042, message="upstream unavailable", data={"retry": False})

        upstream = Server(
            "failing-upstream",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            meta = {AGENTBARRIER_IDEMPOTENCY_META_KEY: "failure-1"}
            arguments = {"request_id": "failure-1", "amount": 10}
            async with Client(gateway.server, cache=None) as client:
                with pytest.raises(MCPError) as raised:
                    await client.call_tool("payments.refund", arguments, meta=meta)
                assert raised.value.error.code == -32042
                assert raised.value.error.message == "upstream unavailable"
                assert raised.value.error.data == {"retry": False}

                retry = await client.call_tool("payments.refund", arguments, meta=meta)

            assert retry.is_error is True
            assert retry.meta is not None
            action_meta = retry.meta[AGENTBARRIER_ACTION_META_KEY]
            assert isinstance(action_meta, dict)
            assert action_meta["status"] == "unknown"
            assert calls == 1
            assert store.list_actions()[0].status is RuntimeStatus.UNKNOWN

    asyncio.run(run())


def test_mcp_gateway_fails_closed_without_stable_identity_or_on_policy_deny(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        upstream = make_upstream(calls)
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
            )
            async with Client(gateway.server, cache=None) as client:
                missing = await client.call_tool(
                    "payments.refund",
                    {"request_id": "missing", "amount": 10},
                )
                denied = await client.call_tool(
                    "database.delete",
                    {"record_id": "record-1"},
                    meta={AGENTBARRIER_IDEMPOTENCY_META_KEY: "delete-1"},
                )

            assert missing.is_error is True
            assert missing.meta is not None
            assert missing.meta[AGENTBARRIER_ERROR_META_KEY] == {"code": "invalid_idempotency_key"}
            assert denied.is_error is True
            assert denied.meta is not None
            action_meta = denied.meta[AGENTBARRIER_ACTION_META_KEY]
            assert isinstance(action_meta, dict)
            assert action_meta["status"] == "denied"
            assert calls == []
            assert len(store.list_actions()) == 1

    asyncio.run(run())


def test_mcp_gateway_supports_argument_identity_and_forwards_progress(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        updates: list[tuple[float, float | None, str | None]] = []
        upstream = make_upstream(calls, progress=True)
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
                idempotency_resolver=argument_idempotency_key("identity.request_id"),
            )
            async with Client(gateway.server, cache=None) as client:
                result = await client.call_tool(
                    "payments.refund",
                    {"identity": {"request_id": "nested-1"}, "amount": 10},
                    progress_callback=lambda value, total, message: _record_progress(
                        updates, value, total, message
                    ),
                )

            assert result.is_error is False
            assert updates == [(1.0, 2.0, "upstream halfway")]
            assert len(calls) == 1

    asyncio.run(run())


async def _record_progress(
    updates: list[tuple[float, float | None, str | None]],
    value: float,
    total: float | None,
    message: str | None,
) -> None:
    updates.append((value, total, message))


def test_argument_idempotency_key_validates_configuration_and_values() -> None:
    for path in ("", ".request_id", "identity."):
        try:
            argument_idempotency_key(path)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"invalid path {path!r} was accepted")

    resolver = argument_idempotency_key("identity.request_id")
    for arguments in ({}, {"identity": {}}, {"identity": {"request_id": 1}}):
        try:
            resolver("tool", arguments, None)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"invalid arguments {arguments!r} were accepted")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"upstream_url": None}, "exactly one"),
        ({"upstream_command": "server"}, "exactly one"),
        ({"upstream_args": ("--flag",)}, "only with upstream_command"),
        ({"upstream_timeout_seconds": 0}, "greater than zero"),
        ({"namespace": ""}, "namespace"),
        ({"idempotency_argument": ".request_id"}, "non-empty segments"),
    ],
)
def test_mcp_gateway_config_fails_closed(changes: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "policy_path": Path("policy.json"),
        "database_path": Path("runtime.db"),
        "namespace": "gateway",
        "upstream_url": "https://mcp.example.com/mcp",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        MCPGatewayConfig(**values)  # type: ignore[arg-type]


def test_mcp_stdio_gateway_end_to_end_process_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        policy = tmp_path / "policy.json"
        database = tmp_path / "runtime.db"
        ledger = tmp_path / "effects.jsonl"
        policy.write_text(
            json.dumps(
                {
                    "version": "stdio-e2e-v1",
                    "default": "deny",
                    "rules": [
                        {
                            "name": "review large refunds",
                            "effect": "require_approval",
                            "tool": "payments.refund",
                            "conditions": [{"path": "amount", "operator": "gt", "value": 20}],
                        },
                        {
                            "name": "allow small refunds",
                            "effect": "allow",
                            "tool": "payments.refund",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        gateway = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agentbarrier",
                "mcp",
                "stdio",
                "--policy",
                str(policy),
                "--db",
                str(database),
                "--upstream-command",
                sys.executable,
                "--upstream-arg=-m",
                "--upstream-arg=tests.mcp_stdio_fixture_server",
                "--upstream-arg=--ledger",
                f"--upstream-arg={ledger}",
                "--idempotency-argument",
                "request_id",
            ],
        )
        async with Client(
            stdio_client(gateway),
            cache=None,
            read_timeout_seconds=10,
        ) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["payments.refund"]

            small_arguments = {"request_id": "stdio-small", "amount": 10}
            first = await client.call_tool("payments.refund", small_arguments)
            replay = await client.call_tool("payments.refund", small_arguments)
            assert first.structured_content == replay.structured_content

            large_arguments = {"request_id": "stdio-large", "amount": 100}
            pending = await client.call_tool("payments.refund", large_arguments)
            assert pending.is_error is True
            assert pending.meta is not None
            action_meta = pending.meta[AGENTBARRIER_ACTION_META_KEY]
            assert isinstance(action_meta, dict)
            action_id = action_meta["actionId"]
            assert isinstance(action_id, str)
            with SQLiteRuntimeStore(database) as store:
                store.decide(action_id, Decision.APPROVE, decided_by="e2e-reviewer")

            completed = await client.call_tool("payments.refund", large_arguments)
            completed_replay = await client.call_tool("payments.refund", large_arguments)
            assert completed.structured_content == completed_replay.structured_content

        effects = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert effects == [small_arguments, large_arguments]
        with SQLiteRuntimeStore(database) as store:
            assert [action.status for action in store.list_actions()] == [
                RuntimeStatus.SUCCEEDED,
                RuntimeStatus.SUCCEEDED,
            ]
            assert store.verify_receipt_chain() is True

    asyncio.run(run())

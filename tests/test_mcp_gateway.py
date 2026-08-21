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
    InputRequiredResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from starlette.testclient import TestClient

import agentbarrier.mcp.runner as mcp_runner
from agentbarrier import __version__
from agentbarrier.mcp import (
    AGENTBARRIER_ACTION_META_KEY,
    AGENTBARRIER_ERROR_META_KEY,
    AGENTBARRIER_IDEMPOTENCY_META_KEY,
    MCPGateway,
    argument_idempotency_key,
)
from agentbarrier.mcp.runner import (
    DEFAULT_MCP_REQUEST_BYTES,
    MAX_MCP_REQUEST_BYTES,
    MCPGatewayConfig,
    create_http_gateway_app,
)
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
from agentbarrier.service.auth import StaticBearerAuth, hash_bearer_token

MCP_HTTP_TOKEN = "mcp-client-token-0123456789"
MCP_READER_TOKEN = "mcp-reader-token-012345678"


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
                version=__version__,
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


def test_mcp_gateway_fails_closed_on_interactive_upstream_tool(tmp_path: Path) -> None:
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
        ) -> CallToolResult | InputRequiredResult:
            nonlocal calls
            del context, params
            calls += 1
            return InputRequiredResult(
                request_state="upstream-interaction-state",
            )

        upstream = Server(
            "interactive-upstream",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            gateway = MCPGateway(
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                client_factory=lambda: Client(upstream, cache=None),
                idempotency_resolver=argument_idempotency_key("request_id"),
            )
            async with Client(gateway.server, cache=None) as client:
                with pytest.raises(MCPError):
                    await client.call_tool(
                        "payments.refund",
                        {"request_id": "interactive-1", "amount": 10},
                    )

            assert calls == 1
            assert store.list_actions()[0].status is RuntimeStatus.UNKNOWN

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
        (
            {
                "upstream_url": None,
                "upstream_command": "server",
                "upstream_bearer_token_env": "MCP_TOKEN",
            },
            "requires upstream_url",
        ),
        ({"upstream_bearer_token_env": "INVALID-NAME"}, "environment name"),
        ({"upstream_url": "ftp://mcp.example.com/mcp"}, "HTTP or HTTPS"),
        ({"upstream_url": "https://mcp.example.com:0/mcp"}, "HTTP or HTTPS"),
        ({"upstream_url": "http://mcp.example.com/mcp"}, "must use HTTPS"),
        ({"upstream_url": "https://user:secret@mcp.example.com/mcp"}, "credentials"),
        ({"upstream_url": "https://mcp.example.com/mcp#fragment"}, "fragment"),
        ({"upstream_timeout_seconds": 0}, "greater than zero"),
        ({"namespace": ""}, "namespace"),
        ({"organization_id": ""}, "organization"),
        ({"organization_id": "acme"}, "requires --requested-by"),
        ({"requested_by": ""}, "requester"),
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


def test_mcp_gateway_config_allows_plain_http_only_for_loopback() -> None:
    config = MCPGatewayConfig(
        policy_path=Path("policy.json"),
        database_path=Path("runtime.db"),
        namespace="gateway",
        upstream_url="http://127.0.0.1:8766/mcp",
    )
    assert config.upstream_url == "http://127.0.0.1:8766/mcp"


def test_mcp_http_upstream_bearer_comes_from_environment_and_disables_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeHTTPClient:
        def __init__(self, **keywords: object) -> None:
            captured.update(keywords)

        async def __aenter__(self) -> FakeHTTPClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    @mcp_runner.asynccontextmanager
    async def fake_transport(url: str, *, http_client: object) -> Any:
        captured["url"] = url
        captured["http_client"] = http_client
        yield (object(), object(), lambda: None)

    token = "upstream-mcp-token-0123456789"
    monkeypatch.setenv("AGENTBARRIER_UPSTREAM_TOKEN", token)
    monkeypatch.setattr(mcp_runner.httpx2, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(mcp_runner, "streamable_http_client", fake_transport)
    config = MCPGatewayConfig(
        policy_path=tmp_path / "policy.json",
        database_path=tmp_path / "runtime.db",
        namespace="gateway",
        upstream_url="https://mcp.example.com/mcp",
        upstream_timeout_seconds=12,
        upstream_bearer_token_env="AGENTBARRIER_UPSTREAM_TOKEN",
    )

    async def exercise() -> None:
        client = mcp_runner._client_factory(config)()
        async with client.server:  # type: ignore[union-attr]
            pass

    asyncio.run(exercise())
    assert captured["headers"] == {"Authorization": f"Bearer {token}"}
    assert captured["follow_redirects"] is False
    assert captured["url"] == "https://mcp.example.com/mcp"


def test_mcp_http_upstream_bearer_fails_closed_when_environment_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTBARRIER_MISSING_TOKEN", raising=False)
    config = MCPGatewayConfig(
        policy_path=tmp_path / "policy.json",
        database_path=tmp_path / "runtime.db",
        namespace="gateway",
        upstream_url="https://mcp.example.com/mcp",
        upstream_bearer_token_env="AGENTBARRIER_MISSING_TOKEN",
    )
    with pytest.raises(ValueError, match="is not set"):
        mcp_runner._client_factory(config)()


def make_mcp_http_auth() -> StaticBearerAuth:
    return StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "agent-runtime",
                    "token_sha256": hash_bearer_token(MCP_HTTP_TOKEN),
                    "scopes": ["mcp:call"],
                },
                {
                    "subject": "approval-reader",
                    "token_sha256": hash_bearer_token(MCP_READER_TOKEN),
                    "scopes": ["actions:read"],
                },
            ],
        }
    )


def raw_mcp_headers(token: str = MCP_HTTP_TOKEN) -> dict[str, str]:
    return {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def sse_json(response_text: str) -> dict[str, Any]:
    data_lines = [
        line.removeprefix("data: ")
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0])
    assert isinstance(payload, dict)
    return payload


def raw_initialize_message() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "initialize-1",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "raw-conformance", "version": "1"},
        },
    }


def initialize_raw_mcp(client: TestClient) -> tuple[dict[str, str], dict[str, Any]]:
    response = client.post(
        "/mcp",
        headers=raw_mcp_headers(),
        json=raw_initialize_message(),
    )
    assert response.status_code == 200
    session_id = response.headers["mcp-session-id"]
    payload = sse_json(response.text)
    headers = raw_mcp_headers()
    headers["MCP-Session-Id"] = session_id
    headers["MCP-Protocol-Version"] = "2025-11-25"
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code == 202
    return headers, payload


def test_mcp_streamable_http_raw_transport_authenticates_and_replays(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    upstream = make_upstream(calls)
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        gateway = MCPGateway(
            barrier=RuntimeBarrier(policy=make_policy(), store=store, namespace="raw-http"),
            client_factory=lambda: Client(upstream, cache=None),
        )
        app = create_http_gateway_app(
            gateway,
            host="testserver",
            auth=make_mcp_http_auth(),
        )
        with TestClient(app) as client:
            missing = client.post("/mcp", json={})
            assert missing.status_code == 401
            assert missing.json()["error"]["code"] == "missing_bearer_token"
            assert missing.headers["cache-control"] == "no-store"
            assert missing.headers["www-authenticate"].startswith("Bearer")

            insufficient = client.post(
                "/mcp",
                headers=raw_mcp_headers(MCP_READER_TOKEN),
                json={},
            )
            assert insufficient.status_code == 403
            assert insufficient.json()["error"]["code"] == "insufficient_scope"

            headers, initialization = initialize_raw_mcp(client)
            assert initialization["result"]["protocolVersion"] == "2025-11-25"

            listed = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            assert [tool["name"] for tool in sse_json(listed.text)["result"]["tools"]] == [
                "payments.refund",
                "database.delete",
            ]

            arguments = {"request_id": "raw-refund-1", "amount": 10}
            request = {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": "payments.refund",
                    "arguments": arguments,
                    "_meta": {AGENTBARRIER_IDEMPOTENCY_META_KEY: "raw-refund-1"},
                },
            }
            first = client.post("/mcp", headers=headers, json=request)
            request["id"] = "call-2"
            replayed = client.post("/mcp", headers=headers, json=request)

        assert first.status_code == replayed.status_code == 200
        assert sse_json(first.text)["result"]["structuredContent"] == {
            "tool": "payments.refund",
            "arguments": arguments,
        }
        assert (
            sse_json(replayed.text)["result"]["structuredContent"]
            == sse_json(first.text)["result"]["structuredContent"]
        )
        assert calls == [("payments.refund", arguments)]
        assert store.list_actions()[0].status is RuntimeStatus.SUCCEEDED
        assert store.verify_receipt_chain() is True


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({**raw_mcp_headers(), "Accept": "application/json"}, "{}", 406),
        (
            {**raw_mcp_headers(), "Content-Type": "text/plain"},
            json.dumps(raw_initialize_message()),
            400,
        ),
        (raw_mcp_headers(), "{not-json", 400),
        (raw_mcp_headers(), json.dumps({"jsonrpc": "2.0", "id": 1}), 400),
    ],
)
def test_mcp_streamable_http_rejects_malformed_raw_requests(
    tmp_path: Path,
    headers: dict[str, str],
    body: str,
    status: int,
) -> None:
    upstream = make_upstream([])
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        gateway = MCPGateway(
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            client_factory=lambda: Client(upstream, cache=None),
        )
        app = create_http_gateway_app(
            gateway,
            host="testserver",
            auth=make_mcp_http_auth(),
        )
        with TestClient(app) as client:
            response = client.post("/mcp", headers=headers, content=body)
    assert response.status_code == status


def test_mcp_streamable_http_limits_request_size_and_public_binding(tmp_path: Path) -> None:
    upstream = make_upstream([])
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        gateway = MCPGateway(
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            client_factory=lambda: Client(upstream, cache=None),
        )
        with pytest.raises(ValueError, match="require --auth-config"):
            create_http_gateway_app(gateway, host="0.0.0.0")
        for request_limit in (1023, MAX_MCP_REQUEST_BYTES + 1):
            with pytest.raises(ValueError, match="request limit"):
                create_http_gateway_app(gateway, max_request_body_size=request_limit)

        app = create_http_gateway_app(
            gateway,
            host="testserver",
            auth=make_mcp_http_auth(),
            max_request_body_size=1024,
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers=raw_mcp_headers(),
                content=json.dumps({"padding": "x" * 2048}),
            )
    assert response.status_code == 413
    assert DEFAULT_MCP_REQUEST_BYTES == 1024 * 1024


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

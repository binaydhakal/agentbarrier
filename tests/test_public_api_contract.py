from __future__ import annotations

import enum
import inspect
from collections.abc import Callable

import agentbarrier
import agentbarrier.errors as errors
import agentbarrier.mcp as mcp
import agentbarrier.observability as observability
import agentbarrier.runtime as runtime
import agentbarrier.service as service
from agentbarrier.integrations import google_adk, langgraph, openai_agents, pydantic_ai

PUBLIC_EXPORTS = {
    agentbarrier: {
        "ActionRequest",
        "AgentAdapter",
        "AgentBarrierError",
        "AmbiguousEffectError",
        "ApprovalBarrierProfile",
        "AuditEvent",
        "AuditReceipt",
        "Capability",
        "Decision",
        "EffectEvent",
        "EffectPhase",
        "Finding",
        "ReconciliationEvidence",
        "ReconciliationStatus",
        "RunHandle",
        "RunOutcome",
        "RunStatus",
        "RunnerOptions",
        "ScenarioResult",
        "ScenarioStatus",
        "SuiteFailure",
        "SuiteResult",
        "SuiteRunner",
        "UnsupportedCapability",
        "action_digest",
    },
    errors: {
        "ActionBindingError",
        "ActionInProgress",
        "ActionLimitExceeded",
        "ActionLimitValueError",
        "ActionOutcomeUnknown",
        "AdapterContractError",
        "AgentBarrierError",
        "AmbiguousEffectError",
        "ApprovalAuthorizationError",
        "ApprovalExpired",
        "ApprovalRejected",
        "ApprovalRequired",
        "EmergencyPauseActive",
        "FrameworkControlSignalError",
        "InvalidActionState",
        "PolicyDenied",
        "RuntimeActionError",
        "RuntimeBarrierError",
        "RuntimeStoreError",
        "SuiteFailure",
        "UnsupportedCapability",
    },
    runtime: {
        "ArgumentCondition",
        "ClaimOutcome",
        "ConditionOperator",
        "DecisionAuthorization",
        "IdempotencySelector",
        "NoopRuntimeObserver",
        "PolicyDecision",
        "PolicyEffect",
        "PolicyRule",
        "PostgresRuntimeStore",
        "RuntimeAction",
        "RuntimeActionObservation",
        "RuntimeBarrier",
        "RuntimeControlEvent",
        "RuntimeControlReceipt",
        "RuntimeEvent",
        "RuntimeLimit",
        "RuntimeLimitUsage",
        "RuntimeObserver",
        "RuntimePause",
        "RuntimePolicy",
        "RuntimeReceipt",
        "RuntimeReconciliation",
        "RuntimeRequest",
        "RuntimeStatus",
        "RuntimeStore",
        "SQLiteRuntimeStore",
        "open_runtime_store",
    },
    service: {
        "ALL_SERVICE_SCOPES",
        "ApprovalAPI",
        "ApprovalDashboard",
        "DashboardSession",
        "DashboardSessionStore",
        "Principal",
        "SlackConfig",
        "SlackInteractionService",
        "SlackNotificationSnapshot",
        "SlackNotificationStore",
        "SlackReviewer",
        "SlackWorker",
        "StaticBearerAuth",
        "WebhookConfig",
        "WebhookDelivery",
        "WebhookDeliverySnapshot",
        "WebhookDeliveryStore",
        "WebhookEndpoint",
        "WebhookSender",
        "WebhookWorker",
        "build_webhook_body",
        "create_approval_app",
        "create_dashboard_app",
        "create_slack_app",
        "hash_bearer_token",
        "signature_headers",
    },
    mcp: {
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
    },
    observability: {"OpenTelemetryConfig", "OpenTelemetryObserver"},
    openai_agents: {"runtime_function_tool"},
    langgraph: {"runtime_structured_tool", "runtime_tool_node"},
    pydantic_ai: {"runtime_tool"},
    google_adk: {"runtime_function_tool"},
}


SIGNATURES: dict[Callable[..., object], tuple[str, ...]] = {
    runtime.RuntimeBarrier: (
        "*",
        "policy",
        "store",
        "namespace='default'",
        "organization_id='default'",
        "requested_by=None",
        "clock_ns=<callable>",
        "observer=None",
    ),
    runtime.RuntimePolicy: ("version", "rules", "default_effect=PolicyEffect.DENY"),
    runtime.PolicyRule: (
        "name",
        "effect",
        "tool='*'",
        "conditions=()",
        "approval_ttl_seconds=None",
    ),
    runtime.ArgumentCondition: ("path", "operator", "value=True"),
    runtime.SQLiteRuntimeStore: (
        "path",
        "*",
        "clock_ns=<callable>",
        "execution_lease_seconds=300",
    ),
    runtime.PostgresRuntimeStore: (
        "dsn",
        "*",
        "schema='agentbarrier'",
        "create_schema=False",
        "migrate=False",
        "clock_ns=<callable>",
        "execution_lease_seconds=300",
        "lock_timeout_seconds=30",
    ),
    runtime.open_runtime_store: (
        "*",
        "database_path=None",
        "postgres_dsn_env=None",
        "postgres_schema='agentbarrier'",
        "postgres_create_schema=False",
        "postgres_migrate=False",
    ),
    runtime.RuntimeRequest: (
        "*",
        "action_id",
        "namespace",
        "tool_name",
        "arguments",
        "idempotency_key",
        "policy_version",
        "created_at_ns",
        "organization_id='default'",
        "requested_by=None",
    ),
    runtime.DecisionAuthorization: (
        "actor",
        "organization_id",
        "namespaces",
        "decisions",
        "require_separate_approver=False",
        "reviewer_subject=None",
    ),
    observability.OpenTelemetryConfig: (
        "include_action_id=True",
        "include_organization_id=False",
        "include_namespace=True",
        "include_tool_name=True",
        "include_policy_version=False",
        "metric_dimensions=frozenset()",
    ),
    observability.OpenTelemetryObserver: (
        "*",
        "config=None",
        "tracer=None",
        "meter=None",
        "logger=None",
        "clock=<callable>",
    ),
    service.StaticBearerAuth: ("credentials",),
    service.create_approval_app: ("*", "store", "auth"),
    service.create_dashboard_app: (
        "*",
        "store",
        "auth",
        "sessions=None",
        "cookie_secure=True",
        "public_origin=None",
    ),
    service.create_slack_app: (
        "*",
        "runtime_store",
        "notification_store",
        "config",
        "api_caller=None",
        "clock_seconds=<callable>",
        "path='/slack/interactions'",
        "worker=None",
        "poll_interval_seconds=1",
    ),
    service.WebhookEndpoint: (
        "endpoint_id",
        "url",
        "secret",
        "secret_env",
        "events",
        "redact_argument_paths=()",
        "timeout_seconds=10",
        "max_attempts=5",
        "initial_backoff_seconds=1",
        "max_backoff_seconds=60",
        "start_from='beginning'",
    ),
    service.WebhookConfig: ("endpoints",),
    service.WebhookDeliveryStore: (
        "path",
        "*",
        "clock_ns=<callable>",
        "claim_lease_seconds=60",
    ),
    service.WebhookWorker: (
        "*",
        "runtime_store",
        "delivery_store",
        "config",
        "sender=None",
        "clock_ns=<callable>",
        "worker_id=None",
    ),
    service.build_webhook_body: ("receipt", "action", "endpoint"),
    service.signature_headers: ("endpoint", "*", "body", "event_id", "timestamp"),
    mcp.MCPGatewayConfig: (
        "policy_path",
        "database_path",
        "namespace",
        "postgres_dsn_env=None",
        "postgres_schema='agentbarrier'",
        "upstream_url=None",
        "upstream_command=None",
        "upstream_args=()",
        "upstream_timeout_seconds=None",
        "upstream_bearer_token_env=None",
        "idempotency_argument=None",
        "organization_id='default'",
        "requested_by=None",
    ),
    mcp.run_stdio_gateway: ("config",),
    mcp.run_http_gateway: (
        "config",
        "*",
        "host='127.0.0.1'",
        "port=8765",
        "path='/mcp'",
        "auth_path=None",
        "max_request_body_size=1048576",
    ),
    mcp.create_http_gateway_app: (
        "gateway",
        "*",
        "host='127.0.0.1'",
        "path='/mcp'",
        "auth=None",
        "max_request_body_size=1048576",
    ),
    openai_agents.runtime_function_tool: (
        "function",
        "*",
        "barrier",
        "idempotency_key",
        "**function_tool_options",
    ),
    langgraph.runtime_structured_tool: (
        "function",
        "*",
        "barrier",
        "idempotency_key",
        "**structured_tool_options",
    ),
    langgraph.runtime_tool_node: (
        "tools",
        "*",
        "name='tools'",
        "tags=None",
        "messages_key='messages'",
    ),
    pydantic_ai.runtime_tool: (
        "function",
        "*",
        "barrier",
        "idempotency_key",
        "**tool_options",
    ),
    google_adk.runtime_function_tool: (
        "function",
        "*",
        "barrier",
        "idempotency_key",
        "name=None",
        "description=None",
        "require_confirmation=False",
    ),
}


def _default_contract(value: object) -> str:
    if callable(value):
        return "<callable>"
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    return repr(value)


def _parameter_contract(value: Callable[..., object]) -> tuple[str, ...]:
    contract: list[str] = []
    keyword_boundary = False
    for parameter in inspect.signature(value).parameters.values():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not keyword_boundary:
            contract.append("*")
            keyword_boundary = True
        prefix = {
            inspect.Parameter.VAR_POSITIONAL: "*",
            inspect.Parameter.VAR_KEYWORD: "**",
        }.get(parameter.kind, "")
        rendered = f"{prefix}{parameter.name}"
        if parameter.default is not inspect.Parameter.empty:
            rendered += f"={_default_contract(parameter.default)}"
        contract.append(rendered)
    return tuple(contract)


def test_public_modules_export_exact_reviewed_surface() -> None:
    for module, expected in PUBLIC_EXPORTS.items():
        actual = set(module.__all__)
        assert actual == expected, module.__name__
        assert all(hasattr(module, name) for name in actual), module.__name__


def test_critical_public_signatures_match_reviewed_contract() -> None:
    for value, expected in SIGNATURES.items():
        assert _parameter_contract(value) == expected, value.__qualname__

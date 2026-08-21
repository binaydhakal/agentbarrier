from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from agentbarrier import __version__
from agentbarrier.cli import main
from agentbarrier.mcp import runner as mcp_runner
from agentbarrier.runtime import PolicyDecision, PolicyEffect, RuntimeRequest, RuntimeStatus
from agentbarrier.runtime.store import SQLiteRuntimeStore
from agentbarrier.service import runner as service_runner
from agentbarrier.service.auth import hash_bearer_token
from agentbarrier.service.webhooks import WebhookDeliverySnapshot


def test_cli_version_matches_distribution_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert __version__ == version("agentbarrier")
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentbarrier {version('agentbarrier')}"


def test_self_test_cli_writes_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    json_path = tmp_path / "report.json"
    junit_path = tmp_path / "report.xml"
    sarif_path = tmp_path / "report.sarif"
    exit_code = main(
        [
            "self-test",
            "--json",
            str(json_path),
            "--junit",
            str(junit_path),
            "--sarif",
            str(sarif_path),
        ]
    )

    assert exit_code == 0
    assert "11 passed" in capsys.readouterr().out
    assert json.loads(json_path.read_text())["passed"] is True
    assert junit_path.exists()
    assert sarif_path.exists()


def test_verify_cli_loads_class_and_scenario_selection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "verify",
            "agentbarrier.adapters.reference:ReferenceAdapter",
            "--scenario",
            "approval_hold",
        ]
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "approval_hold" in output
    assert "1 passed" in output


def test_scenarios_cli_lists_guarantees(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scenarios"]) == 0
    output = capsys.readouterr().out
    assert "approval_hold" in output
    assert "parallel_barrier" in output
    assert "outcome_ambiguity" in output
    assert "audit_receipts" in output


def test_mcp_stdio_cli_builds_gateway_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[mcp_runner.MCPGatewayConfig] = []
    monkeypatch.setattr(mcp_runner, "run_stdio_gateway", captured.append)
    policy = tmp_path / "policy.json"
    database = tmp_path / "runtime.db"

    assert (
        main(
            [
                "mcp",
                "stdio",
                "--policy",
                str(policy),
                "--db",
                str(database),
                "--namespace",
                "support-gateway",
                "--organization",
                "acme",
                "--requested-by",
                "support-agent",
                "--upstream-command",
                "python",
                "--upstream-arg",
                "server.py",
                "--idempotency-argument",
                "request.id",
            ]
        )
        == 0
    )
    assert captured == [
        mcp_runner.MCPGatewayConfig(
            policy_path=policy,
            database_path=database,
            namespace="support-gateway",
            organization_id="acme",
            requested_by="support-agent",
            upstream_command="python",
            upstream_args=("server.py",),
            idempotency_argument="request.id",
        )
    ]


def test_mcp_http_cli_uses_safe_listen_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[mcp_runner.MCPGatewayConfig, str, int, str, str | None, int]] = []

    def run_http(
        config: mcp_runner.MCPGatewayConfig,
        *,
        host: str,
        port: int,
        path: str,
        auth_path: str | None,
        max_request_body_size: int,
    ) -> None:
        captured.append((config, host, port, path, auth_path, max_request_body_size))

    monkeypatch.setattr(mcp_runner, "run_http_gateway", run_http)
    assert (
        main(
            [
                "mcp",
                "http",
                "--policy",
                str(tmp_path / "policy.json"),
                "--db",
                str(tmp_path / "runtime.db"),
                "--upstream-url",
                "https://mcp.example.com/mcp",
            ]
        )
        == 0
    )
    assert captured[0][1:] == ("127.0.0.1", 8765, "/mcp", None, 1024 * 1024)


def test_mcp_http_cli_forwards_authentication_and_request_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def run_http(config: mcp_runner.MCPGatewayConfig, **keywords: object) -> None:
        captured.append({"config": config, **keywords})

    monkeypatch.setattr(mcp_runner, "run_http_gateway", run_http)
    auth_path = tmp_path / "auth.json"
    assert (
        main(
            [
                "mcp",
                "http",
                "--policy",
                str(tmp_path / "policy.json"),
                "--db",
                str(tmp_path / "runtime.db"),
                "--upstream-url",
                "https://mcp.example.com/mcp",
                "--upstream-bearer-token-env",
                "MCP_UPSTREAM_TOKEN",
                "--auth-config",
                str(auth_path),
                "--max-request-bytes",
                "2097152",
            ]
        )
        == 0
    )
    config = captured[0]["config"]
    assert isinstance(config, mcp_runner.MCPGatewayConfig)
    assert config.upstream_bearer_token_env == "MCP_UPSTREAM_TOKEN"
    assert captured[0]["auth_path"] == str(auth_path)
    assert captured[0]["max_request_body_size"] == 2 * 1024 * 1024


def test_approval_api_cli_uses_safe_listen_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def run_approval_api(**keywords: object) -> None:
        captured.append(keywords)

    monkeypatch.setattr(service_runner, "run_approval_api", run_approval_api)
    database = tmp_path / "runtime.db"
    auth_config = tmp_path / "auth.json"
    assert (
        main(
            [
                "api",
                "--db",
                str(database),
                "--auth-config",
                str(auth_config),
            ]
        )
        == 0
    )
    assert captured == [
        {
            "database_path": str(database),
            "auth_path": str(auth_config),
            "postgres_dsn_env": None,
            "postgres_schema": "agentbarrier",
            "host": "127.0.0.1",
            "port": 8787,
        }
    ]


def test_approval_api_cli_forwards_postgres_environment_name_without_a_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service_runner,
        "run_approval_api",
        lambda **keywords: captured.append(keywords),
    )

    assert (
        main(
            [
                "api",
                "--postgres-dsn-env",
                "AGENTBARRIER_DATABASE_URL",
                "--postgres-schema",
                "agentbarrier_team",
                "--auth-config",
                str(tmp_path / "auth.json"),
            ]
        )
        == 0
    )
    assert captured[0]["database_path"] is None
    assert captured[0]["postgres_dsn_env"] == "AGENTBARRIER_DATABASE_URL"
    assert captured[0]["postgres_schema"] == "agentbarrier_team"
    assert "postgresql://" not in repr(captured)


def test_dashboard_cli_forwards_secure_operational_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def run_approval_dashboard(**keywords: object) -> None:
        captured.append(keywords)

    monkeypatch.setattr(service_runner, "run_approval_dashboard", run_approval_dashboard)
    database = tmp_path / "runtime.db"
    auth_config = tmp_path / "auth.json"
    assert (
        main(
            [
                "dashboard",
                "--db",
                str(database),
                "--auth-config",
                str(auth_config),
                "--host",
                "10.0.0.5",
                "--port",
                "9443",
                "--public-origin",
                "https://review.example.com",
                "--cookie-secure",
                "--session-ttl",
                "3600",
            ]
        )
        == 0
    )
    assert captured == [
        {
            "database_path": str(database),
            "auth_path": str(auth_config),
            "postgres_dsn_env": None,
            "postgres_schema": "agentbarrier",
            "host": "10.0.0.5",
            "port": 9443,
            "public_origin": "https://review.example.com",
            "cookie_secure": True,
            "session_ttl_seconds": 3600.0,
        }
    ]


def test_auth_hash_token_reads_secret_from_environment_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "generated-token-0123456789"
    monkeypatch.setenv("AGENTBARRIER_TEST_TOKEN", token)
    assert main(["auth", "hash-token", "--token-env", "AGENTBARRIER_TEST_TOKEN"]) == 0
    output = capsys.readouterr().out.strip()
    assert output == hash_bearer_token(token)
    assert token not in output

    monkeypatch.delenv("AGENTBARRIER_TEST_TOKEN")
    with pytest.raises(SystemExit) as raised:
        main(["auth", "hash-token", "--token-env", "AGENTBARRIER_TEST_TOKEN"])
    assert raised.value.code == 2


def test_cli_can_force_color(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["self-test", "--scenario", "approval_hold", "--color", "always"]) == 0
    assert "\x1b[32;1mPASS" in capsys.readouterr().out


def test_cli_selects_and_reports_approval_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    json_path = tmp_path / "profile.json"
    assert (
        main(
            [
                "self-test",
                "--scenario",
                "parallel_barrier",
                "--approval-profile",
                "per-action",
                "--json",
                str(json_path),
            ]
        )
        == 0
    )

    assert "approval-profile=per-action" in capsys.readouterr().out
    assert json.loads(json_path.read_text())["approval_profile"] == "per-action"


def test_cli_generates_and_checks_compatibility_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_path = tmp_path / "compatibility.json"
    markdown_path = tmp_path / "compatibility.md"
    markdown_path.write_text(
        "# Matrix\n\n<!-- agentbarrier:compatibility:start -->\n"
        "stale\n<!-- agentbarrier:compatibility:end -->\n"
    )
    arguments = [
        "compatibility",
        "--adapter",
        "reference",
        "--json",
        str(json_path),
        "--markdown",
        str(markdown_path),
        "--strict-missing",
    ]

    assert main(arguments) == 0
    assert main([*arguments, "--check"]) == 0
    evidence = json.loads(json_path.read_text())
    assert evidence["adapters"][0]["key"] == "reference"
    assert evidence["profiles"] == ["run-wide", "per-action"]

    json_path.write_text("{}\n")
    assert main([*arguments, "--check"]) == 1
    assert "compatibility outputs are out of date" in capsys.readouterr().err
    assert json_path.read_text() == "{}\n"


@pytest.mark.parametrize(
    "target",
    [
        "missing-syntax",
        "agentbarrier.adapters.reference:missing",
        "agentbarrier.models:Capability",
    ],
)
def test_verify_cli_rejects_invalid_targets(target: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["verify", target])
    assert exc.value.code == 2


def _pending_runtime_action(path: Path, *, action_id: str = "runtime-action") -> str:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": "refund-1", "amount": 100},
        idempotency_key="refund-1",
        policy_version="1",
        created_at_ns=1,
    )
    with SQLiteRuntimeStore(path) as store:
        store.submit(
            request,
            PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refunds", "1"),
        )
    return request.action_id


def _unknown_runtime_action(path: Path) -> str:
    request = RuntimeRequest(
        action_id="unknown-action",
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": "unknown-refund", "amount": 100},
        idempotency_key="unknown-refund",
        policy_version="1",
        created_at_ns=1,
    )
    with SQLiteRuntimeStore(path) as store:
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "allow", "1"),
        )
        store.claim(action.action_id, request_digest=request.request_digest)
        store.mark_unknown(
            action.action_id,
            request_digest=request.request_digest,
            error="ConnectionError",
        )
    return request.action_id


def test_runtime_approval_cli_lists_shows_approves_and_audits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    action_id = _pending_runtime_action(path)

    assert main(["approvals", "list", "--db", str(path), "--status", "pending", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["action_id"] == action_id
    assert listed[0]["status"] == "pending"

    assert main(["approvals", "show", action_id, "--db", str(path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["arguments"] == {"amount": 100, "request_id": "refund-1"}

    assert (
        main(
            [
                "approvals",
                "approve",
                action_id,
                "--db",
                str(path),
                "--decided-by",
                "alice",
                "--reason",
                "ticket-123",
            ]
        )
        == 0
    )
    assert f"approved {action_id}" in capsys.readouterr().out
    with SQLiteRuntimeStore(path) as store:
        action = store.get_action(action_id)
        assert action.status is RuntimeStatus.APPROVED
        assert action.decided_by == "alice"

    assert main(["audit", "--db", str(path), "--action-id", action_id, "--json"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["chain_valid"] is True
    assert [item["event"] for item in audit["receipts"]] == [
        "approval_requested",
        "approved",
    ]


def test_runtime_approval_cli_rejects_and_handles_empty_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    assert main(["approvals", "list", "--db", str(path)]) == 0
    assert "No runtime actions" in capsys.readouterr().out

    action_id = _pending_runtime_action(path, action_id="reject-me")
    assert (
        main(
            [
                "approvals",
                "reject",
                action_id,
                "--db",
                str(path),
                "--decided-by",
                "bob",
            ]
        )
        == 0
    )
    assert "rejected reject-me" in capsys.readouterr().out


def test_runtime_cli_normalizes_unknown_action_to_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["approvals", "show", "missing", "--db", str(tmp_path / "runtime.db")])
    assert exc.value.code == 2


def test_runtime_cli_reconciles_committed_unknown_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    action_id = _unknown_runtime_action(path)
    assert (
        main(
            [
                "approvals",
                "reconcile",
                action_id,
                "--db",
                str(path),
                "--outcome",
                "committed",
                "--resolved-by",
                "payment-ledger",
                "--reason",
                "transaction exists",
                "--result-json",
                '{"status":"refunded"}',
            ]
        )
        == 0
    )
    assert "reconciled unknown-action as committed" in capsys.readouterr().out
    with SQLiteRuntimeStore(path) as store:
        assert store.get_action(action_id).status is RuntimeStatus.SUCCEEDED


def test_runtime_cli_requires_reconciliation_result(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    action_id = _unknown_runtime_action(path)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "approvals",
                "reconcile",
                action_id,
                "--db",
                str(path),
                "--outcome",
                "committed",
                "--resolved-by",
                "ledger",
                "--reason",
                "present",
            ]
        )
    assert exc.value.code == 2


def test_runtime_controls_cli_manages_pause_limit_and_audit_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "runtime.db"
    assert (
        main(
            [
                "controls",
                "pause",
                "--db",
                str(path),
                "--namespace",
                "billing",
                "--tool",
                "payments.refund",
                "--paused-by",
                "on-call",
                "--reason",
                "provider incident",
            ]
        )
        == 0
    )
    assert "paused namespace=billing tool=payments.refund" in capsys.readouterr().out

    assert (
        main(
            [
                "controls",
                "limit-set",
                "refund-budget",
                "--db",
                str(path),
                "--namespace",
                "billing",
                "--tool",
                "payments.refund",
                "--window-seconds",
                "60",
                "--max-actions",
                "5",
                "--value-argument",
                "amount_cents",
                "--max-value",
                "10000",
                "--updated-by",
                "risk-team",
                "--reason",
                "refund blast radius",
            ]
        )
        == 0
    )
    assert "configured limit refund-budget" in capsys.readouterr().out

    assert main(["controls", "status", "--db", str(path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["control_chain_valid"] is True
    assert status["pauses"][0]["paused_by"] == "on-call"
    assert status["limits"][0]["limit_id"] == "refund-budget"
    assert status["usage"][0]["actions_used"] == 0
    assert [receipt["event"] for receipt in status["control_receipts"]] == [
        "emergency_pause_set",
        "limit_configured",
    ]

    assert (
        main(
            [
                "controls",
                "limit-disable",
                "refund-budget",
                "--db",
                str(path),
                "--updated-by",
                "risk-team",
                "--reason",
                "rollout complete",
            ]
        )
        == 0
    )
    assert "disabled limit refund-budget" in capsys.readouterr().out
    assert (
        main(
            [
                "controls",
                "resume",
                "--db",
                str(path),
                "--namespace",
                "billing",
                "--tool",
                "payments.refund",
                "--resumed-by",
                "on-call",
                "--reason",
                "provider recovered",
            ]
        )
        == 0
    )
    assert "resumed namespace=billing tool=payments.refund" in capsys.readouterr().out
    with SQLiteRuntimeStore(path) as store:
        assert store.list_pauses() == ()
        assert store.list_limits()[0].enabled is False
        assert store.verify_control_receipt_chain()


@pytest.mark.parametrize(
    "arguments",
    [
        ["controls", "status"],
        [
            "controls",
            "resume",
            "--resumed-by",
            "on-call",
            "--reason",
            "recovered",
        ],
        [
            "controls",
            "limit-disable",
            "missing-limit",
            "--updated-by",
            "risk-team",
            "--reason",
            "retired",
        ],
    ],
)
def test_runtime_control_reads_reject_missing_database(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    path = tmp_path / "missing.db"
    arguments.extend(["--db", str(path)])
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2
    assert not path.exists()


def test_runtime_database_cli_reports_migrates_and_backs_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    _pending_runtime_action(path)

    assert main(["database", "status", "--db", str(path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "active_pauses": 0,
        "actions": 1,
        "control_receipt_chain_valid": True,
        "control_receipts": 0,
        "limits": 0,
        "receipt_chain_valid": True,
        "receipts": 1,
        "schema_version": "5",
    }

    assert main(["database", "migrate", "--db", str(path)]) == 0
    assert "schema version 5" in capsys.readouterr().out

    backup = tmp_path / "runtime-backup.db"
    assert main(["database", "backup", "--db", str(path), "--output", str(backup)]) == 0
    assert str(backup) in capsys.readouterr().out
    with SQLiteRuntimeStore(backup) as store:
        assert len(store.list_actions()) == 1
        assert store.verify_receipt_chain()

    with pytest.raises(SystemExit) as exc:
        main(["database", "backup", "--db", str(path), "--output", str(backup)])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["status", "backup"])
def test_runtime_database_cli_rejects_missing_source(tmp_path: Path, command: str) -> None:
    arguments = ["database", command, "--db", str(tmp_path / "missing.db")]
    if command == "backup":
        arguments.extend(["--output", str(tmp_path / "backup.db")])
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2
    assert not (tmp_path / "missing.db").exists()


def test_webhook_run_cli_passes_operational_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[dict[str, object]] = []

    def run_webhook_worker(**values: object) -> dict[str, int]:
        captured.append(values)
        return {"enqueued": 2, "delivered": 1, "retried": 1, "dead": 0}

    monkeypatch.setattr(service_runner, "run_webhook_worker", run_webhook_worker)
    database = tmp_path / "runtime.db"
    state_database = tmp_path / "webhooks.db"
    config = tmp_path / "webhooks.json"
    assert (
        main(
            [
                "webhooks",
                "run",
                "--db",
                str(database),
                "--state-db",
                str(state_database),
                "--config",
                str(config),
                "--once",
                "--poll-interval",
                "0.25",
            ]
        )
        == 0
    )
    assert captured == [
        {
            "database_path": str(database),
            "state_path": str(state_database),
            "config_path": str(config),
            "postgres_dsn_env": None,
            "postgres_schema": "agentbarrier",
            "once": True,
            "poll_interval_seconds": 0.25,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "dead": 0,
        "delivered": 1,
        "enqueued": 2,
        "retried": 1,
    }


def test_webhook_status_cli_omits_payloads_and_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = WebhookDeliverySnapshot(
        delivery_id="delivery-1",
        endpoint_id="operations",
        receipt_sequence=4,
        event_id="runtime-receipt-4",
        event_type="approved",
        status="delivered",
        attempts=1,
        next_attempt_at_ns=10,
        last_status_code=204,
        last_error=None,
        delivered_at_ns=20,
    )
    monkeypatch.setattr(
        service_runner,
        "webhook_delivery_status",
        lambda _path: (snapshot,),
    )

    state_database = tmp_path / "webhooks.db"
    assert main(["webhooks", "status", "--state-db", str(state_database), "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == [
        {
            "attempts": 1,
            "delivered_at_ns": 20,
            "delivery_id": "delivery-1",
            "endpoint_id": "operations",
            "event_id": "runtime-receipt-4",
            "event_type": "approved",
            "last_error": None,
            "last_status_code": 204,
            "next_attempt_at_ns": 10,
            "receipt_sequence": 4,
            "status": "delivered",
        }
    ]
    assert "secret" not in output.lower()


def test_webhook_retry_cli_requires_exact_dead_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = WebhookDeliverySnapshot(
        delivery_id="delivery-1",
        endpoint_id="operations",
        receipt_sequence=4,
        event_id="runtime-receipt-4",
        event_type="approved",
        status="pending",
        attempts=0,
        next_attempt_at_ns=30,
        last_status_code=503,
        last_error="HTTPError",
        delivered_at_ns=None,
    )
    captured: list[tuple[str, str, str]] = []

    def retry_webhook_delivery(
        path: str,
        *,
        endpoint_id: str,
        event_id: str,
    ) -> WebhookDeliverySnapshot:
        captured.append((path, endpoint_id, event_id))
        return snapshot

    monkeypatch.setattr(service_runner, "retry_webhook_delivery", retry_webhook_delivery)
    state_database = tmp_path / "webhooks.db"
    assert (
        main(
            [
                "webhooks",
                "retry",
                "runtime-receipt-4",
                "--endpoint",
                "operations",
                "--state-db",
                str(state_database),
            ]
        )
        == 0
    )
    assert captured == [(str(state_database), "operations", "runtime-receipt-4")]
    assert "requeued runtime-receipt-4 for endpoint operations" in capsys.readouterr().out


def test_webhook_status_cli_rejects_missing_state_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing-webhooks.db"
    with pytest.raises(SystemExit) as exc:
        main(["webhooks", "status", "--state-db", str(missing)])
    assert exc.value.code == 2
    assert not missing.exists()

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agentbarrier import __version__
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import StaticBearerAuth, create_approval_app, hash_bearer_token

READER_TOKEN = "reader-token-012345678901"
REVIEWER_TOKEN = "reviewer-token-0123456789"
AUDITOR_TOKEN = "auditor-token-0123456789"


def make_auth() -> StaticBearerAuth:
    return StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "reader-service",
                    "token_sha256": hash_bearer_token(READER_TOKEN),
                    "scopes": ["actions:read"],
                },
                {
                    "subject": "reviewer@example.com",
                    "token_sha256": hash_bearer_token(REVIEWER_TOKEN),
                    "scopes": ["actions:read", "actions:decide"],
                },
                {
                    "subject": "audit-service",
                    "token_sha256": hash_bearer_token(AUDITOR_TOKEN),
                    "scopes": ["audit:read"],
                },
            ],
        }
    )


def submit_pending(
    store: SQLiteRuntimeStore,
    *,
    action_id: str,
    created_at_ns: int,
) -> RuntimeRequest:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": action_id, "amount": 100},
        idempotency_key=action_id,
        policy_version="api-policy-v1",
        created_at_ns=created_at_ns,
    )
    store.submit(
        request,
        PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refunds", "api-policy-v1"),
    )
    return request


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_approval_api_readiness_openapi_and_security_headers(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        TestClient(create_approval_app(store=store, auth=make_auth())) as client,
    ):
        ready = client.get("/health/ready", headers={"X-Request-Id": "request-123"})
        schema = client.get("/openapi.json")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "schema_version": "4",
        "version": __version__,
    }
    assert ready.headers["x-request-id"] == "request-123"
    assert ready.headers["cache-control"] == "no-store"
    assert ready.headers["x-content-type-options"] == "nosniff"
    assert ready.headers["content-security-policy"] == "default-src 'none'"
    document = schema.json()
    assert document["openapi"] == "3.1.0"
    assert document["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert document["paths"]["/v1/actions"]["get"]["x-agentbarrier-scope"] == ("actions:read")


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic abcdefghijklmnop",
        "Bearer short",
        "Bearer unknown-token-0123456789",
        "Bearer token with spaces 1234",
    ],
)
def test_approval_api_rejects_missing_malformed_and_unknown_tokens(
    tmp_path: Path,
    authorization: str | None,
) -> None:
    headers = {"Authorization": authorization} if authorization is not None else {}
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        TestClient(create_approval_app(store=store, auth=make_auth())) as client,
    ):
        response = client.get("/v1/actions", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    payload = response.json()["error"]
    assert payload["code"] in {"missing_bearer_token", "invalid_bearer_token"}
    assert payload["request_id"] == response.headers["x-request-id"]
    assert "token" not in payload["message"].lower() or "bearer token" in payload["message"].lower()


def test_approval_api_enforces_scopes_and_authenticated_reviewer_identity(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, action_id="approval-1", created_at_ns=1)
        app = create_approval_app(store=store, auth=make_auth())
        with TestClient(app) as client:
            listed = client.get("/v1/actions", headers=bearer(READER_TOKEN))
            denied = client.post(
                "/v1/actions/approval-1/approve",
                headers=bearer(READER_TOKEN),
            )
            hidden = client.get("/v1/actions", headers=bearer(AUDITOR_TOKEN))
            unknown_identity = client.post(
                "/v1/actions/approval-1/approve",
                headers={**bearer(REVIEWER_TOKEN), "Content-Type": "application/json"},
                json={"decided_by": "attacker"},
            )
            approved = client.post(
                "/v1/actions/approval-1/approve",
                headers=bearer(REVIEWER_TOKEN),
                json={"reason": "ticket-123"},
            )

        action = store.get_action("approval-1")

    assert listed.status_code == 200
    assert listed.json()["data"][0]["arguments"] == {
        "amount": 100,
        "request_id": "approval-1",
    }
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"
    assert 'scope="actions:decide"' in denied.headers["www-authenticate"]
    assert hidden.status_code == 403
    assert unknown_identity.status_code == 400
    assert unknown_identity.json()["error"]["code"] == "unknown_body_fields"
    assert approved.status_code == 200
    assert action.status is RuntimeStatus.APPROVED
    assert action.decided_by == "reviewer@example.com"
    assert action.decision_reason == "ticket-123"


def test_approval_api_decisions_are_idempotent_and_conflicts_are_explicit(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, action_id="decision-1", created_at_ns=1)
        with TestClient(create_approval_app(store=store, auth=make_auth())) as client:
            first = client.post(
                "/v1/actions/decision-1/approve",
                headers=bearer(REVIEWER_TOKEN),
                json={"reason": "reviewed"},
            )
            replay = client.post(
                "/v1/actions/decision-1/approve",
                headers=bearer(REVIEWER_TOKEN),
                json={"reason": "ignored-on-replay"},
            )
            conflict = client.post(
                "/v1/actions/decision-1/reject",
                headers=bearer(REVIEWER_TOKEN),
            )
            missing = client.post(
                "/v1/actions/not-found/reject",
                headers=bearer(REVIEWER_TOKEN),
            )

        receipts = store.receipts(action_id="decision-1")

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert [receipt.event.value for receipt in receipts] == ["approval_requested", "approved"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "action_state_conflict"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "action_not_found"


def test_approval_api_paginates_actions_and_audit_receipts(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        for index in range(3):
            submit_pending(store, action_id=f"page-{index}", created_at_ns=index + 1)
        with TestClient(create_approval_app(store=store, auth=make_auth())) as client:
            first = client.get(
                "/v1/actions?status=pending&limit=2",
                headers=bearer(READER_TOKEN),
            )
            cursor = first.json()["next_cursor"]
            second = client.get(
                f"/v1/actions?status=pending&limit=2&after={cursor}",
                headers=bearer(READER_TOKEN),
            )
            audit_first = client.get(
                "/v1/audit?limit=2",
                headers=bearer(AUDITOR_TOKEN),
            )
            after_sequence = audit_first.json()["next_sequence"]
            audit_second = client.get(
                f"/v1/audit?limit=2&after_sequence={after_sequence}",
                headers=bearer(AUDITOR_TOKEN),
            )

    assert [item["action_id"] for item in first.json()["data"]] == ["page-0", "page-1"]
    assert cursor == "page-1"
    assert [item["action_id"] for item in second.json()["data"]] == ["page-2"]
    assert second.json()["next_cursor"] is None
    assert [item["sequence"] for item in audit_first.json()["data"]] == [1, 2]
    assert [item["sequence"] for item in audit_second.json()["data"]] == [3]
    assert audit_second.json()["chain_valid"] is True


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "expected_status", "expected_code"),
    [
        ("GET", "/missing", {}, None, 404, "route_not_found"),
        ("POST", "/v1/actions", bearer(REVIEWER_TOKEN), None, 405, "method_not_allowed"),
        (
            "GET",
            "/v1/actions?unknown=true",
            bearer(READER_TOKEN),
            None,
            400,
            "unknown_query_parameters",
        ),
        (
            "GET",
            "/v1/actions?limit=1&limit=2",
            bearer(READER_TOKEN),
            None,
            400,
            "duplicate_query_parameter",
        ),
        (
            "GET",
            "/v1/actions?limit=101",
            bearer(READER_TOKEN),
            None,
            400,
            "invalid_limit",
        ),
        (
            "GET",
            "/v1/actions?after=unknown",
            bearer(READER_TOKEN),
            None,
            400,
            "invalid_cursor",
        ),
        (
            "POST",
            "/v1/actions/input-1/approve",
            {**bearer(REVIEWER_TOKEN), "Content-Type": "text/plain"},
            b"{}",
            415,
            "unsupported_media_type",
        ),
        (
            "POST",
            "/v1/actions/input-1/approve",
            {**bearer(REVIEWER_TOKEN), "Content-Type": "application/json"},
            b"{bad",
            400,
            "invalid_json",
        ),
        (
            "POST",
            "/v1/actions/input-1/approve",
            {**bearer(REVIEWER_TOKEN), "Content-Type": "application/json"},
            b"[]",
            400,
            "invalid_body",
        ),
        (
            "POST",
            "/v1/actions/input-1/approve",
            {**bearer(REVIEWER_TOKEN), "Content-Type": "application/json"},
            b'{"reason":""}',
            400,
            "invalid_reason",
        ),
        (
            "POST",
            "/v1/actions/input-1/approve",
            {**bearer(REVIEWER_TOKEN), "Content-Type": "application/json"},
            b"x" * (16 * 1024 + 1),
            413,
            "body_too_large",
        ),
    ],
)
def test_approval_api_returns_stable_errors_for_adversarial_requests(
    tmp_path: Path,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    expected_status: int,
    expected_code: str,
) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, action_id="input-1", created_at_ns=1)
        with TestClient(create_approval_app(store=store, auth=make_auth())) as client:
            response = client.request(method, path, headers=headers, content=body)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_approval_api_replaces_unsafe_request_id(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        TestClient(create_approval_app(store=store, auth=make_auth())) as client,
    ):
        response = client.get(
            "/v1/actions",
            headers={**bearer(READER_TOKEN), "X-Request-Id": "bad id\nvalue"},
        )

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert request_id != "bad id\nvalue"
    assert len(request_id) == 36

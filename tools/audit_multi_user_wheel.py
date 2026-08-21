"""Audit an installed wheel's organization isolation and independent approval lifecycle."""

from __future__ import annotations

import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from agentbarrier.errors import ApprovalRequired
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import StaticBearerAuth, create_approval_app, hash_bearer_token

ALICE_TOKEN = "wheel-alice-token-012345678901"
BOB_TOKEN = "wheel-bob-token-01234567890123"
OTHER_TOKEN = "wheel-other-token-012345678901"


def authorization() -> StaticBearerAuth:
    return StaticBearerAuth.from_mapping(
        {
            "version": "2",
            "organizations": [
                {
                    "id": "acme",
                    "namespaces": ["billing"],
                    "require_separate_approver": True,
                },
                {
                    "id": "other",
                    "namespaces": ["support"],
                    "require_separate_approver": True,
                },
            ],
            "roles": [
                {
                    "id": "reviewer",
                    "scopes": ["actions:read", "actions:decide", "audit:read"],
                    "decisions": ["approve", "reject"],
                }
            ],
            "tokens": [
                {
                    "subject": "alice",
                    "kind": "user",
                    "organization": "acme",
                    "roles": ["reviewer"],
                    "token_sha256": hash_bearer_token(ALICE_TOKEN),
                },
                {
                    "subject": "bob",
                    "kind": "user",
                    "organization": "acme",
                    "roles": ["reviewer"],
                    "token_sha256": hash_bearer_token(BOB_TOKEN),
                },
                {
                    "subject": "mallory",
                    "kind": "user",
                    "organization": "other",
                    "roles": ["reviewer"],
                    "token_sha256": hash_bearer_token(OTHER_TOKEN),
                },
            ],
        }
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def run_audit(directory: Path) -> None:
    effects: list[str] = []
    policy = RuntimePolicy(
        version="multi-user-wheel-v1",
        rules=(
            PolicyRule(
                "independent review",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
            ),
        ),
    )
    with SQLiteRuntimeStore(directory / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=policy,
            store=store,
            namespace="billing",
            organization_id="acme",
            requested_by="alice",
        )

        def refund(request_id: str) -> dict[str, str]:
            effects.append(request_id)
            return {"request_id": request_id, "status": "refunded"}

        protected = barrier.protect(
            refund,
            tool_name="payments.refund",
            idempotency_key="request_id",
        )
        try:
            protected("refund-1")
        except ApprovalRequired as pending:
            action_id = pending.action.action_id
        else:  # pragma: no cover - audit failure path
            raise AssertionError("organization action did not require approval")

        with TestClient(create_approval_app(store=store, auth=authorization())) as client:
            other = client.get("/v1/actions", headers=bearer(OTHER_TOKEN))
            self_review = client.post(
                f"/v1/actions/{action_id}/approve",
                headers=bearer(ALICE_TOKEN),
            )
            approval = client.post(
                f"/v1/actions/{action_id}/approve",
                headers=bearer(BOB_TOKEN),
                json={"reason": "independent installed-wheel review"},
            )

        if other.status_code != 200 or other.json()["data"] != []:
            raise AssertionError("cross-organization action isolation failed")
        if self_review.status_code != 403:
            raise AssertionError("requester self-review was not denied")
        if self_review.json()["error"]["code"] != "separation_of_duties":
            raise AssertionError("self-review returned the wrong denial")
        if approval.status_code != 200:
            raise AssertionError("independent reviewer could not approve")
        result = protected("refund-1")
        action = store.get_action(action_id)
        if result != {"request_id": "refund-1", "status": "refunded"}:
            raise AssertionError("approved organization action returned the wrong result")
        if effects != ["refund-1"] or action.status is not RuntimeStatus.SUCCEEDED:
            raise AssertionError("approved organization action did not execute exactly once")
        if action.organization_id != "acme" or action.requested_by != "alice":
            raise AssertionError("organization or requester attribution was lost")
        if action.decided_by != "bob" or not store.verify_receipt_chain():
            raise AssertionError("independent reviewer or receipt integrity was lost")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agentbarrier-multi-user-wheel-") as temporary:
        run_audit(Path(temporary))
    print("installed wheel multi-user authorization audit passed")

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agentbarrier.errors import ApprovalAuthorizationError
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    DecisionAuthorization,
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import StaticBearerAuth, create_approval_app, hash_bearer_token

ALICE_TOKEN = "alice-reviewer-token-0123456789"
BOB_TOKEN = "bob-reviewer-token-012345678901"
CAROL_TOKEN = "carol-reviewer-token-0123456789"


def v2_auth() -> StaticBearerAuth:
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
                    "id": "beta",
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
                    "subject": "carol",
                    "kind": "user",
                    "organization": "beta",
                    "roles": ["reviewer"],
                    "token_sha256": hash_bearer_token(CAROL_TOKEN),
                },
            ],
        }
    )


def submit_pending(
    store: SQLiteRuntimeStore,
    *,
    action_id: str,
    organization_id: str = "acme",
    namespace: str = "billing",
    requested_by: str = "alice",
) -> RuntimeRequest:
    request = RuntimeRequest(
        action_id=action_id,
        organization_id=organization_id,
        requested_by=requested_by,
        namespace=namespace,
        tool_name="payments.refund",
        arguments={"request_id": action_id, "amount": 100},
        idempotency_key=action_id,
        policy_version="multi-user-v1",
        created_at_ns=1,
    )
    store.submit(
        request,
        PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refund", "multi-user-v1"),
    )
    return request


def test_v2_auth_resolves_roles_and_organization_constraints() -> None:
    principal = v2_auth().authenticate(f"Bearer {BOB_TOKEN}")

    assert principal.subject == "bob"
    assert principal.organization_id == "acme"
    assert principal.kind == "user"
    assert principal.roles == frozenset({"reviewer"})
    assert principal.namespaces == frozenset({"billing"})
    assert principal.decisions == frozenset({Decision.APPROVE, Decision.REJECT})
    assert principal.require_separate_approver is True
    authorization = principal.decision_authorization()
    assert authorization.organization_id == "acme"
    assert authorization.actor == "bob"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"organizations": []}, "organizations must not be empty"),
        (
            {
                "organizations": [
                    {"id": "acme", "namespaces": ["shared"]},
                    {"id": "beta", "namespaces": ["shared"]},
                ]
            },
            "more than one organization",
        ),
        (
            {"roles": [{"id": "bad", "scopes": ["actions:decide"], "decisions": []}]},
            "exactly when",
        ),
    ],
)
def test_v2_auth_rejects_unsafe_configuration(change: dict[str, object], message: str) -> None:
    document: dict[str, object] = {
        "version": "2",
        "organizations": [{"id": "acme", "namespaces": ["billing"]}],
        "roles": [{"id": "reader", "scopes": ["actions:read"], "decisions": []}],
        "tokens": [
            {
                "subject": "alice",
                "kind": "user",
                "organization": "acme",
                "roles": ["reader"],
                "token_sha256": hash_bearer_token(ALICE_TOKEN),
            }
        ],
    }
    document.update(change)

    with pytest.raises(ValueError, match=message):
        StaticBearerAuth.from_mapping(document)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unexpected": True}, "unknown auth document keys"),
        ({"roles": "bad"}, "roles must be a list"),
        (
            {
                "roles": [
                    {"id": "reader", "scopes": ["actions:read"], "decisions": []},
                    {"id": "reader", "scopes": ["actions:read"], "decisions": []},
                ]
            },
            "duplicate role",
        ),
        (
            {"roles": [{"id": "bad", "scopes": ["unknown"], "decisions": []}]},
            "unknown AgentBarrier",
        ),
        (
            {"roles": [{"id": "bad", "scopes": [], "decisions": ["allow"]}]},
            "approve.*reject",
        ),
        (
            {
                "tokens": [
                    {
                        "subject": "alice",
                        "kind": "robot",
                        "organization": "acme",
                        "roles": ["reader"],
                        "token_sha256": hash_bearer_token(ALICE_TOKEN),
                    }
                ]
            },
            "user.*service",
        ),
        (
            {
                "tokens": [
                    {
                        "subject": "alice",
                        "kind": "user",
                        "organization": "missing",
                        "roles": ["reader"],
                        "token_sha256": hash_bearer_token(ALICE_TOKEN),
                    }
                ]
            },
            "unknown organization",
        ),
        (
            {
                "tokens": [
                    {
                        "subject": "alice",
                        "kind": "user",
                        "organization": "acme",
                        "roles": ["missing"],
                        "token_sha256": hash_bearer_token(ALICE_TOKEN),
                    }
                ]
            },
            "unknown roles",
        ),
    ],
)
def test_v2_auth_rejects_malformed_roles_and_tokens(
    change: dict[str, object], message: str
) -> None:
    document: dict[str, object] = {
        "version": "2",
        "organizations": [{"id": "acme", "namespaces": ["billing"]}],
        "roles": [{"id": "reader", "scopes": ["actions:read"], "decisions": []}],
        "tokens": [
            {
                "subject": "alice",
                "kind": "user",
                "organization": "acme",
                "roles": ["reader"],
                "token_sha256": hash_bearer_token(ALICE_TOKEN),
            }
        ],
    }
    document.update(change)

    with pytest.raises((TypeError, ValueError), match=message):
        StaticBearerAuth.from_mapping(document)


def test_store_enforces_multi_user_authorization_inside_decision_transaction(
    tmp_path: Path,
) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, action_id="self-review")
        submit_pending(store, action_id="wrong-org")
        submit_pending(store, action_id="wrong-namespace")
        submit_pending(store, action_id="wrong-decision")

        with pytest.raises(ApprovalAuthorizationError, match="own action") as self_review:
            store.decide_authorized(
                "self-review",
                Decision.APPROVE,
                authorization=DecisionAuthorization(
                    actor="alice",
                    organization_id="acme",
                    namespaces=frozenset({"billing"}),
                    decisions=frozenset({Decision.APPROVE}),
                    require_separate_approver=True,
                ),
            )
        assert self_review.value.code == "separation_of_duties"

        cases = (
            (
                "wrong-org",
                DecisionAuthorization(
                    "bob", "beta", frozenset({"billing"}), frozenset({Decision.APPROVE})
                ),
                Decision.APPROVE,
                "organization_mismatch",
            ),
            (
                "wrong-namespace",
                DecisionAuthorization(
                    "bob", "acme", frozenset({"support"}), frozenset({Decision.APPROVE})
                ),
                Decision.APPROVE,
                "namespace_forbidden",
            ),
            (
                "wrong-decision",
                DecisionAuthorization(
                    "bob", "acme", frozenset({"billing"}), frozenset({Decision.APPROVE})
                ),
                Decision.REJECT,
                "decision_forbidden",
            ),
        )
        for action_id, authorization, decision, code in cases:
            with pytest.raises(ApprovalAuthorizationError) as denied:
                store.decide_authorized(
                    action_id,
                    decision,
                    authorization=authorization,
                )
            assert denied.value.code == code

        approved = store.decide_authorized(
            "self-review",
            Decision.APPROVE,
            authorization=DecisionAuthorization(
                "bob",
                "acme",
                frozenset({"billing"}),
                frozenset({Decision.APPROVE}),
                require_separate_approver=True,
            ),
        )
        assert approved.status is RuntimeStatus.APPROVED
        assert approved.decided_by == "bob"


def test_approval_api_isolates_organizations_and_blocks_self_approval(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        acme_request = submit_pending(store, action_id="acme-refund")
        submit_pending(
            store,
            action_id="beta-refund",
            organization_id="beta",
            namespace="support",
            requested_by="carol",
        )
        with TestClient(create_approval_app(store=store, auth=v2_auth())) as client:
            listed = client.get("/v1/actions", headers={"Authorization": f"Bearer {ALICE_TOKEN}"})
            hidden = client.get(
                "/v1/actions/beta-refund",
                headers={"Authorization": f"Bearer {ALICE_TOKEN}"},
            )
            cross_org = client.post(
                "/v1/actions/beta-refund/approve",
                headers={"Authorization": f"Bearer {ALICE_TOKEN}"},
            )
            self_review = client.post(
                "/v1/actions/acme-refund/approve",
                headers={"Authorization": f"Bearer {ALICE_TOKEN}"},
            )
            approved = client.post(
                "/v1/actions/acme-refund/approve",
                headers={"Authorization": f"Bearer {BOB_TOKEN}"},
                json={"reason": "independent review"},
            )
            audit = client.get("/v1/audit", headers={"Authorization": f"Bearer {BOB_TOKEN}"})

        assert [item["action_id"] for item in listed.json()["data"]] == ["acme-refund"]
        payload = listed.json()["data"][0]
        assert payload["organization_id"] == "acme"
        assert payload["requested_by"] == "alice"
        assert hidden.status_code == cross_org.status_code == 404
        assert self_review.status_code == 403
        assert self_review.json()["error"]["code"] == "separation_of_duties"
        assert approved.status_code == 200
        assert store.get_action(acme_request.action_id).decided_by == "bob"
        assert {item["action_id"] for item in audit.json()["data"]} == {"acme-refund"}


def test_request_digest_binds_nonlegacy_organization_and_requester() -> None:
    def request(organization_id: str, requested_by: str) -> RuntimeRequest:
        return RuntimeRequest(
            action_id="action",
            organization_id=organization_id,
            requested_by=requested_by,
            namespace="billing",
            tool_name="payments.refund",
            arguments={"amount": 10},
            idempotency_key="refund",
            policy_version="1",
            created_at_ns=1,
        )

    alice = request("acme", "alice")
    bob = request("acme", "bob")
    beta = request("beta", "alice")

    assert len({alice.request_digest, bob.request_digest, beta.request_digest}) == 3


@pytest.mark.parametrize(
    ("organization_id", "requested_by", "message"),
    [
        ("", "alice", "organization_id"),
        ("a" * 129, "alice", "organization_id"),
        ("acme", "bad\nsubject", "requested_by"),
    ],
)
def test_runtime_request_rejects_invalid_production_identity(
    organization_id: str, requested_by: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimeRequest(
            action_id="action",
            organization_id=organization_id,
            requested_by=requested_by,
            namespace="billing",
            tool_name="payments.refund",
            arguments={},
            idempotency_key="refund",
            policy_version="1",
            created_at_ns=1,
        )


def test_decision_authorization_rejects_non_decision_values() -> None:
    with pytest.raises(TypeError, match="Decision values"):
        DecisionAuthorization(
            actor="bob",
            organization_id="acme",
            namespaces=frozenset({"billing"}),
            decisions=frozenset({"approve"}),  # type: ignore[arg-type]
        )

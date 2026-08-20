from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentbarrier.errors import (
    ActionBindingError,
    ActionInProgress,
    ActionOutcomeUnknown,
    ApprovalExpired,
    ApprovalRejected,
    ApprovalRequired,
    InvalidActionState,
    PolicyDenied,
)
from agentbarrier.models import Decision
from agentbarrier.runtime import PolicyDecision, PolicyEffect, RuntimeRequest, RuntimeStatus
from agentbarrier.runtime.models import ClaimOutcome
from agentbarrier.runtime.store import SQLiteRuntimeStore


class Clock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += int(seconds * 1_000_000_000)


def make_request(
    clock: Clock,
    *,
    action_id: str = "action-1",
    amount: int = 50,
    key: str = "refund-1",
    version: str = "1",
) -> RuntimeRequest:
    return RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": key, "amount": amount},
        idempotency_key=key,
        policy_version=version,
        created_at_ns=clock(),
    )


def approval(version: str = "1", ttl: float | None = None) -> PolicyDecision:
    return PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refunds", version, ttl)


def test_store_approves_executes_and_replays_exactly_once(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        pending = store.submit(request, approval())
        assert pending.status is RuntimeStatus.PENDING
        assert store.submit(request, approval()).action_id == pending.action_id
        with pytest.raises(ApprovalRequired):
            store.claim(pending.action_id, request_digest=request.request_digest)

        approved = store.decide(
            pending.action_id,
            Decision.APPROVE,
            decided_by="alice",
            reason="ticket-123",
        )
        assert approved.status is RuntimeStatus.APPROVED
        assert approved.decided_by == "alice"
        assert (
            store.decide(pending.action_id, Decision.APPROVE, decided_by="alice").status
            is RuntimeStatus.APPROVED
        )

        claim = store.claim(pending.action_id, request_digest=request.request_digest)
        assert claim.outcome is ClaimOutcome.EXECUTE
        with pytest.raises(ActionInProgress):
            store.claim(pending.action_id, request_digest=request.request_digest)

        complete = store.complete(
            pending.action_id,
            request_digest=request.request_digest,
            result={"refunded": True},
        )
        assert complete.status is RuntimeStatus.SUCCEEDED
        assert complete.result == {"refunded": True}
        assert complete.result_available

        replay = store.claim(pending.action_id, request_digest=request.request_digest)
        assert replay.outcome is ClaimOutcome.REPLAY
        assert replay.result == {"refunded": True}
        assert [receipt.event.value for receipt in store.receipts(action_id=pending.action_id)] == [
            "approval_requested",
            "approved",
            "execution_started",
            "execution_succeeded",
            "result_replayed",
        ]
        assert store.verify_receipt_chain()


def test_store_rejects_idempotency_binding_changes(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        store.submit(request, approval())
        with pytest.raises(ActionBindingError, match="idempotency"):
            store.submit(make_request(clock, action_id="action-2", amount=51), approval())
        with pytest.raises(ActionBindingError, match="digest"):
            store.claim(request.action_id, request_digest="wrong")


def test_store_distinguishes_policy_denial_and_human_rejection(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        denied_request = make_request(clock, action_id="denied", key="denied")
        denied = store.submit(
            denied_request,
            PolicyDecision(PolicyEffect.DENY, "blocked", "1"),
        )
        assert denied.status is RuntimeStatus.DENIED
        with pytest.raises(PolicyDenied):
            store.claim(denied.action_id, request_digest=denied_request.request_digest)

        rejected_request = make_request(clock, action_id="rejected", key="rejected")
        pending = store.submit(rejected_request, approval())
        rejected = store.decide(
            pending.action_id,
            Decision.REJECT,
            decided_by="bob",
            reason="wrong account",
        )
        assert rejected.status is RuntimeStatus.REJECTED
        with pytest.raises(ApprovalRejected):
            store.claim(rejected.action_id, request_digest=rejected_request.request_digest)
        with pytest.raises(InvalidActionState):
            store.decide(rejected.action_id, Decision.APPROVE, decided_by="bob")


def test_store_expires_pending_and_unused_approved_actions(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        pending_request = make_request(clock, action_id="pending", key="pending")
        pending = store.submit(pending_request, approval(ttl=1))
        clock.advance(1)
        assert store.get_action(pending.action_id).status is RuntimeStatus.EXPIRED
        with pytest.raises(ApprovalExpired):
            store.decide(pending.action_id, Decision.APPROVE, decided_by="alice")

        approved_request = make_request(clock, action_id="approved", key="approved")
        approved = store.submit(approved_request, approval(ttl=1))
        store.decide(approved.action_id, Decision.APPROVE, decided_by="alice")
        clock.advance(1)
        with pytest.raises(ApprovalExpired):
            store.claim(approved.action_id, request_digest=approved_request.request_digest)
        assert len(store.list_actions(status=RuntimeStatus.EXPIRED)) == 2


def test_store_marks_started_exception_as_unknown_and_never_retries(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        store.claim(action.action_id, request_digest=request.request_digest)
        unknown = store.mark_unknown(
            action.action_id,
            request_digest=request.request_digest,
            error="TimeoutError",
        )
        assert unknown.status is RuntimeStatus.UNKNOWN
        assert unknown.error == "TimeoutError"
        with pytest.raises(ActionOutcomeUnknown):
            store.claim(action.action_id, request_digest=request.request_digest)
        with pytest.raises(InvalidActionState):
            store.complete(
                action.action_id,
                request_digest=request.request_digest,
                result=None,
            )


def test_store_validates_decisions_transitions_and_unknown_actions(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        with pytest.raises(ValueError, match="version"):
            store.submit(request, approval(version="2"))
        pending = store.submit(request, approval())
        with pytest.raises(ValueError, match="decided_by"):
            store.decide(pending.action_id, Decision.APPROVE, decided_by="")
        with pytest.raises(KeyError, match="unknown"):
            store.get_action("missing")
        with pytest.raises(ValueError, match="error"):
            store.mark_unknown(
                pending.action_id,
                request_digest=request.request_digest,
                error="",
            )


def test_store_detects_receipt_tampering(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    clock = Clock()
    with SQLiteRuntimeStore(path, clock_ns=clock) as store:
        store.submit(make_request(clock), approval())
        assert store.verify_receipt_chain()
        connection = sqlite3.connect(path)
        connection.execute("UPDATE runtime_receipts SET detail = 'tampered' WHERE sequence = 1")
        connection.commit()
        connection.close()
        assert not store.verify_receipt_chain()


def test_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '99')")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="unsupported"):
        SQLiteRuntimeStore(path)

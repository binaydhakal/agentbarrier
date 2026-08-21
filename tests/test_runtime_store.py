from __future__ import annotations

import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

from agentbarrier.errors import (
    ActionBindingError,
    ActionInProgress,
    ActionLimitExceeded,
    ActionLimitValueError,
    ActionOutcomeUnknown,
    ApprovalExpired,
    ApprovalRejected,
    ApprovalRequired,
    EmergencyPauseActive,
    InvalidActionState,
    PolicyDenied,
)
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeReconciliation,
    RuntimeRequest,
    RuntimeStatus,
)
from agentbarrier.runtime.models import ClaimOutcome
from agentbarrier.runtime.store import SQLiteRuntimeStore


def _claim_in_subprocess(
    path: Path,
    action_id: str,
    request_digest: str,
    ready: object,
    gate: object,
    results: object,
    lease_seconds: float = 300,
) -> None:
    ready.put(True)  # type: ignore[attr-defined]
    gate.wait(10)  # type: ignore[attr-defined]
    with SQLiteRuntimeStore(path, execution_lease_seconds=lease_seconds) as store:
        try:
            outcome = store.claim(action_id, request_digest=request_digest).outcome.value
        except ActionInProgress:
            outcome = "in_progress"
    results.put(outcome)  # type: ignore[attr-defined]


def _claim_limited_in_subprocess(
    path: Path,
    action_id: str,
    request_digest: str,
    ready: object,
    gate: object,
    results: object,
) -> None:
    ready.put(True)  # type: ignore[attr-defined]
    gate.wait(10)  # type: ignore[attr-defined]
    with SQLiteRuntimeStore(path) as store:
        try:
            outcome = store.claim(action_id, request_digest=request_digest).outcome.value
        except ActionLimitExceeded:
            outcome = "limited"
    results.put(outcome)  # type: ignore[attr-defined]


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


def test_store_reconciles_committed_unknown_to_replayable_result(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(request, approval())
        store.decide(
            action.action_id,
            Decision.APPROVE,
            decided_by="alice",
            reason="ticket-123",
        )
        store.claim(action.action_id, request_digest=request.request_digest)
        store.mark_unknown(
            action.action_id,
            request_digest=request.request_digest,
            error="ConnectionError",
        )

        reconciled = store.reconcile(
            action.action_id,
            RuntimeReconciliation.COMMITTED,
            resolved_by="payment-ledger",
            reason="transaction refund-1 exists",
            result={"status": "refunded"},
        )
        assert reconciled.status is RuntimeStatus.SUCCEEDED
        assert reconciled.result == {"status": "refunded"}
        assert reconciled.decided_by == "alice"
        assert reconciled.decision_reason == "ticket-123"
        assert (
            store.claim(action.action_id, request_digest=request.request_digest).outcome
            is ClaimOutcome.REPLAY
        )


def test_store_reconciles_absent_unknown_to_fresh_approval(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(request, approval(ttl=10))
        store.decide(action.action_id, Decision.APPROVE, decided_by="alice")
        store.claim(action.action_id, request_digest=request.request_digest)
        store.mark_unknown(
            action.action_id,
            request_digest=request.request_digest,
            error="TimeoutError",
        )
        clock.advance(1)

        reconciled = store.reconcile(
            action.action_id,
            RuntimeReconciliation.NOT_COMMITTED,
            resolved_by="payment-ledger",
            reason="transaction refund-1 is absent",
        )
        assert reconciled.status is RuntimeStatus.PENDING
        assert reconciled.decided_by is None
        assert reconciled.approval_ttl_ns == 10_000_000_000
        assert reconciled.expires_at_ns == clock() + 10_000_000_000
        with pytest.raises(ApprovalRequired):
            store.claim(action.action_id, request_digest=request.request_digest)


def test_store_validates_reconciliation_input_and_state(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        pending = store.submit(request, approval())
        with pytest.raises(ValueError, match="resolved_by"):
            store.reconcile(
                pending.action_id,
                RuntimeReconciliation.NOT_COMMITTED,
                resolved_by="",
                reason="absent",
            )
        with pytest.raises(ValueError, match="reason"):
            store.reconcile(
                pending.action_id,
                RuntimeReconciliation.NOT_COMMITTED,
                resolved_by="ledger",
                reason="",
            )
        with pytest.raises(ValueError, match="result"):
            store.reconcile(
                pending.action_id,
                RuntimeReconciliation.NOT_COMMITTED,
                resolved_by="ledger",
                reason="absent",
                result={"unexpected": True},
            )
        with pytest.raises(InvalidActionState, match="pending"):
            store.reconcile(
                pending.action_id,
                RuntimeReconciliation.COMMITTED,
                resolved_by="ledger",
                reason="present",
                result=None,
            )


def test_store_execution_lease_turns_abandoned_claim_into_unknown(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(
        tmp_path / "runtime.db",
        clock_ns=clock,
        execution_lease_seconds=1,
    ) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        executing = store.claim(action.action_id, request_digest=request.request_digest).action
        assert executing.execution_lease_expires_at_ns == clock() + 1_000_000_000
        clock.advance(1)

        unknown = store.get_action(action.action_id)
        assert unknown.status is RuntimeStatus.UNKNOWN
        assert unknown.error == "ExecutionLeaseExpired"
        assert unknown.execution_lease_expires_at_ns is None
        assert store.receipts(action_id=action.action_id)[-1].event.value == "execution_abandoned"
        with pytest.raises(ActionOutcomeUnknown):
            store.claim(action.action_id, request_digest=request.request_digest)


def test_two_store_connections_cannot_claim_the_same_action(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    clock = Clock()
    with (
        SQLiteRuntimeStore(path, clock_ns=clock) as first,
        SQLiteRuntimeStore(path, clock_ns=clock) as second,
    ):
        request = make_request(clock)
        action = first.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        assert (
            first.claim(action.action_id, request_digest=request.request_digest).outcome
            is ClaimOutcome.EXECUTE
        )
        with pytest.raises(ActionInProgress):
            second.claim(action.action_id, request_digest=request.request_digest)
        first.complete(
            action.action_id,
            request_digest=request.request_digest,
            result={"ok": True},
        )
        assert (
            second.claim(action.action_id, request_digest=request.request_digest).outcome
            is ClaimOutcome.REPLAY
        )


def test_two_processes_cannot_claim_the_same_action(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    clock = Clock()
    with SQLiteRuntimeStore(path, clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_subprocess,
            args=(path, action.action_id, request.request_digest, ready, gate, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
    gate.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == ["execute", "in_progress"]


def test_subprocess_exit_after_claim_expires_to_unknown(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    clock = Clock()
    with SQLiteRuntimeStore(path, clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    gate = context.Event()
    results = context.Queue()
    process = context.Process(
        target=_claim_in_subprocess,
        args=(path, action.action_id, request.request_digest, ready, gate, results, 0.01),
    )
    process.start()
    assert ready.get(timeout=10) is True
    gate.set()
    assert results.get(timeout=10) == "execute"
    process.join(timeout=10)
    assert process.exitcode == 0
    time.sleep(0.02)

    with SQLiteRuntimeStore(path) as store:
        abandoned = store.get_action(action.action_id)
        assert abandoned.status is RuntimeStatus.UNKNOWN
        assert abandoned.error == "ExecutionLeaseExpired"


def test_emergency_pause_blocks_claim_until_exact_scope_is_cleared(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(request, PolicyDecision(PolicyEffect.ALLOW, "safe", "1"))
        pause = store.set_pause(
            namespace="billing",
            tool_name="payments.refund",
            paused_by="on-call",
            reason="provider incident",
        )

        assert pause.namespace == "billing"
        assert pause.tool_name == "payments.refund"
        with pytest.raises(EmergencyPauseActive, match="provider incident") as captured:
            store.claim(action.action_id, request_digest=request.request_digest)
        assert captured.value.scope == ('{"namespace":"billing","tool_name":"payments.refund"}')
        assert store.get_action(action.action_id).status is RuntimeStatus.APPROVED
        assert store.receipts(action_id=action.action_id)[-1].event.value == (
            "emergency_pause_blocked"
        )
        assert store.verify_receipt_chain()
        assert store.verify_control_receipt_chain()

        assert not store.clear_pause(
            namespace="other",
            tool_name="payments.refund",
            resumed_by="on-call",
            reason="wrong scope",
        )
        assert store.clear_pause(
            namespace="billing",
            tool_name="payments.refund",
            resumed_by="on-call",
            reason="provider recovered",
        )
        assert store.list_pauses() == ()
        assert (
            store.claim(action.action_id, request_digest=request.request_digest).outcome
            is ClaimOutcome.EXECUTE
        )
        assert [receipt.event.value for receipt in store.control_receipts()] == [
            "emergency_pause_set",
            "emergency_pause_cleared",
        ]


@pytest.mark.parametrize(
    ("namespace", "tool_name", "blocked"),
    [
        (None, None, True),
        ("billing", None, True),
        (None, "payments.refund", True),
        ("other", None, False),
        (None, "payments.capture", False),
    ],
)
def test_emergency_pause_scope_matching(
    tmp_path: Path,
    namespace: str | None,
    tool_name: str | None,
    blocked: bool,
) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        request = make_request(clock)
        action = store.submit(request, PolicyDecision(PolicyEffect.ALLOW, "safe", "1"))
        store.set_pause(
            namespace=namespace,
            tool_name=tool_name,
            paused_by="operator",
            reason="incident",
        )
        if blocked:
            with pytest.raises(EmergencyPauseActive):
                store.claim(action.action_id, request_digest=request.request_digest)
        else:
            assert (
                store.claim(action.action_id, request_digest=request.request_digest).outcome
                is ClaimOutcome.EXECUTE
            )


def test_atomic_limit_blocks_action_count_and_integer_value_budget(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        configured = store.configure_limit(
            "refund-budget",
            namespace="billing",
            tool_name="payments.refund",
            window_seconds=60,
            max_actions=2,
            value_argument="amount",
            max_value=100,
            updated_by="risk-team",
            reason="limit refund blast radius",
        )
        assert configured.enabled
        assert configured.max_actions == 2
        assert configured.max_value == 100

        first_request = make_request(clock, action_id="first", key="first", amount=60)
        first = store.submit(
            first_request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        store.claim(first.action_id, request_digest=first_request.request_digest)
        store.complete(first.action_id, request_digest=first_request.request_digest, result={})

        second_request = make_request(clock, action_id="second", key="second", amount=41)
        second = store.submit(
            second_request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        with pytest.raises(ActionLimitExceeded, match="amount") as captured:
            store.claim(second.action_id, request_digest=second_request.request_digest)
        assert captured.value.limit_id == "refund-budget"
        assert captured.value.used == 60
        assert captured.value.requested == 41
        assert store.get_action(second.action_id).status is RuntimeStatus.APPROVED
        assert store.limit_usage("refund-budget")[0].actions_used == 1
        assert store.limit_usage("refund-budget")[0].value_used == 60
        assert store.receipts(action_id=second.action_id)[-1].event.value == "limit_blocked"


def test_value_limit_fails_closed_for_missing_or_invalid_cost(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        store.configure_limit(
            "refund-value",
            window_seconds=60,
            value_argument="money.amount_minor",
            max_value=100,
            updated_by="risk-team",
            reason="minor-unit budget",
        )
        request = RuntimeRequest(
            action_id="invalid-value",
            namespace="billing",
            tool_name="payments.refund",
            arguments={"money": {"amount_minor": "50"}},
            idempotency_key="invalid-value",
            policy_version="1",
            created_at_ns=clock(),
        )
        action = store.submit(request, PolicyDecision(PolicyEffect.ALLOW, "safe", "1"))
        with pytest.raises(ActionLimitValueError, match="non-negative integer"):
            store.claim(action.action_id, request_digest=request.request_digest)
        assert store.limit_usage("refund-value")[0].actions_used == 0


def test_not_committed_reconciliation_releases_reserved_capacity(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        store.configure_limit(
            "refund-value",
            window_seconds=60,
            value_argument="amount",
            max_value=100,
            updated_by="risk-team",
            reason="refund budget",
        )
        first_request = make_request(clock, action_id="uncertain", key="uncertain", amount=80)
        first = store.submit(first_request, approval())
        store.decide(first.action_id, Decision.APPROVE, decided_by="alice")
        store.claim(first.action_id, request_digest=first_request.request_digest)
        store.mark_unknown(
            first.action_id,
            request_digest=first_request.request_digest,
            error="TimeoutError",
        )

        second_request = make_request(clock, action_id="next", key="next", amount=30)
        second = store.submit(
            second_request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        with pytest.raises(ActionLimitExceeded):
            store.claim(second.action_id, request_digest=second_request.request_digest)

        store.reconcile(
            first.action_id,
            RuntimeReconciliation.NOT_COMMITTED,
            resolved_by="payment-ledger",
            reason="transaction is absent",
        )
        usage = store.limit_usage("refund-value")[0]
        assert usage.actions_used == 0
        assert usage.value_used == 0
        assert (
            store.claim(second.action_id, request_digest=second_request.request_digest).outcome
            is ClaimOutcome.EXECUTE
        )


def test_limit_window_resets_without_erasing_previous_usage(tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as store:
        store.configure_limit(
            "one-per-minute",
            window_seconds=60,
            max_actions=1,
            updated_by="operator",
            reason="slow rollout",
        )
        first_request = make_request(clock, action_id="first", key="first")
        first = store.submit(
            first_request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        store.claim(first.action_id, request_digest=first_request.request_digest)
        store.complete(first.action_id, request_digest=first_request.request_digest, result={})

        second_request = make_request(clock, action_id="second", key="second")
        second = store.submit(
            second_request,
            PolicyDecision(PolicyEffect.ALLOW, "safe", "1"),
        )
        with pytest.raises(ActionLimitExceeded):
            store.claim(second.action_id, request_digest=second_request.request_digest)
        clock.advance(60)
        assert (
            store.claim(second.action_id, request_digest=second_request.request_digest).outcome
            is ClaimOutcome.EXECUTE
        )


def test_limit_is_atomic_across_concurrent_processes(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with SQLiteRuntimeStore(path) as store:
        store.configure_limit(
            "one-at-a-time",
            window_seconds=60,
            max_actions=1,
            updated_by="operator",
            reason="concurrency proof",
        )
        requests = [
            RuntimeRequest(
                action_id=f"action-{index}",
                namespace="billing",
                tool_name="payments.refund",
                arguments={"amount": 1},
                idempotency_key=f"key-{index}",
                policy_version="1",
                created_at_ns=time.time_ns(),
            )
            for index in range(2)
        ]
        actions = [
            store.submit(request, PolicyDecision(PolicyEffect.ALLOW, "safe", "1"))
            for request in requests
        ]

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_limited_in_subprocess,
            args=(path, action.action_id, request.request_digest, ready, gate, results),
        )
        for action, request in zip(actions, requests, strict=True)
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
    gate.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == ["execute", "limited"]


def test_store_validates_control_configuration_and_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with SQLiteRuntimeStore(path) as store:
        with pytest.raises(ValueError, match="at least one"):
            store.configure_limit(
                "empty",
                window_seconds=60,
                updated_by="operator",
                reason="invalid",
            )
        with pytest.raises(ValueError, match="together"):
            store.configure_limit(
                "mismatch",
                window_seconds=60,
                max_value=10,
                updated_by="operator",
                reason="invalid",
            )
        with pytest.raises(ValueError, match="positive integer"):
            store.configure_limit(
                "bad-count",
                window_seconds=60,
                max_actions=0,
                updated_by="operator",
                reason="invalid",
            )
        store.set_pause(paused_by="operator", reason="incident")
        assert store.verify_control_receipt_chain()
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE runtime_control_receipts SET detail = 'tampered' WHERE sequence = 1"
        )
        connection.commit()
        connection.close()
        assert not store.verify_control_receipt_chain()


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


def test_store_writes_consistent_non_overwriting_backup(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    backup_path = tmp_path / "backups" / "runtime.db"
    backup_path.parent.mkdir()
    clock = Clock()
    with SQLiteRuntimeStore(path, clock_ns=clock) as store:
        request = make_request(clock)
        store.submit(request, approval())
        assert store.schema_version == "4"
        assert store.backup(backup_path) == backup_path

    assert backup_path.stat().st_mode & 0o777 == 0o600
    with SQLiteRuntimeStore(backup_path) as backup:
        assert len(backup.list_actions()) == 1
        assert backup.verify_receipt_chain()

    with SQLiteRuntimeStore(path) as store:
        with pytest.raises(FileExistsError, match="already exists"):
            store.backup(backup_path)
        with pytest.raises(ValueError, match="different"):
            store.backup(path)
        with pytest.raises(FileNotFoundError, match="directory"):
            store.backup(tmp_path / "missing" / "runtime.db")


def test_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '99')")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="unsupported"):
        SQLiteRuntimeStore(path)


def test_store_migrates_v3_database_to_durable_controls(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '3')")
    connection.execute(
        """
        CREATE TABLE runtime_actions (
            action_id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_rule TEXT NOT NULL,
            policy_effect TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL,
            updated_at_ns INTEGER NOT NULL,
            expires_at_ns INTEGER,
            approval_ttl_ns INTEGER,
            execution_lease_expires_at_ns INTEGER,
            result_json TEXT,
            error TEXT,
            decided_by TEXT,
            decision_reason TEXT,
            UNIQUE (namespace, tool_name, idempotency_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE runtime_receipts (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT NOT NULL,
            event TEXT NOT NULL,
            timestamp_ns INTEGER NOT NULL,
            request_digest TEXT NOT NULL,
            actor TEXT,
            detail TEXT,
            previous_hash TEXT,
            receipt_hash TEXT NOT NULL,
            FOREIGN KEY (action_id) REFERENCES runtime_actions(action_id)
        )
        """
    )
    connection.commit()
    connection.close()

    with SQLiteRuntimeStore(path) as store:
        assert store.schema_version == "4"
        store.set_pause(paused_by="operator", reason="migration check")
        store.configure_limit(
            "migration-limit",
            window_seconds=60,
            max_actions=1,
            updated_by="operator",
            reason="migration check",
        )
        assert len(store.list_pauses()) == 1
        assert len(store.list_limits()) == 1
        assert store.verify_control_receipt_chain()


@pytest.mark.parametrize("lease", [0, -1, float("nan"), float("inf"), 1e-12])
def test_store_rejects_invalid_execution_lease(tmp_path: Path, lease: float) -> None:
    with pytest.raises(ValueError, match="execution_lease_seconds"):
        SQLiteRuntimeStore(tmp_path / "runtime.db", execution_lease_seconds=lease)


def test_store_migrates_v1_and_fails_closed_for_legacy_execution(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '1')")
    connection.execute(
        """
        CREATE TABLE runtime_actions (
            action_id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_rule TEXT NOT NULL,
            policy_effect TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL,
            updated_at_ns INTEGER NOT NULL,
            expires_at_ns INTEGER,
            result_json TEXT,
            error TEXT,
            decided_by TEXT,
            decision_reason TEXT,
            UNIQUE (namespace, tool_name, idempotency_key)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO runtime_actions (
            action_id, namespace, tool_name, arguments_json, idempotency_key,
            request_digest, policy_version, policy_rule, policy_effect, status,
            created_at_ns, updated_at_ns, expires_at_ns
        ) VALUES ('pending', 'n', 'tool', '{}', 'pending-key', 'pending-digest', '1',
                  'review', 'require_approval', 'pending', 1, 1, 11)
        """
    )
    connection.execute(
        """
        INSERT INTO runtime_actions (
            action_id, namespace, tool_name, arguments_json, idempotency_key,
            request_digest, policy_version, policy_rule, policy_effect, status,
            created_at_ns, updated_at_ns
        ) VALUES ('legacy', 'n', 'tool', '{}', 'key', 'digest', '1', 'allow',
                  'allow', 'executing', 1, 1)
        """
    )
    connection.commit()
    connection.close()

    clock = Clock()
    with SQLiteRuntimeStore(path, clock_ns=clock) as store:
        action = store.get_action("legacy")
        assert action.status is RuntimeStatus.UNKNOWN
        assert action.error == "ExecutionLeaseExpired"
        pending = store.get_action("pending")
        assert pending.approval_ttl_ns == 10
        with sqlite3.connect(path) as migrated:
            metadata = migrated.execute(
                "SELECT value FROM runtime_metadata WHERE key = 'schema_version'"
            ).fetchone()
        assert metadata == ("4",)

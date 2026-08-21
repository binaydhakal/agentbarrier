from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol

import pytest

from agentbarrier.errors import ActionInProgress, ActionLimitExceeded, ApprovalRequired
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ClaimOutcome,
    PolicyDecision,
    PolicyEffect,
    PostgresRuntimeStore,
    RuntimeReconciliation,
    RuntimeRequest,
    RuntimeStatus,
    RuntimeStore,
    SQLiteRuntimeStore,
    open_runtime_store,
)


class Clock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += int(seconds * 1_000_000_000)


class StoreFactory(Protocol):
    def __call__(
        self,
        *,
        clock: Clock,
        lease_seconds: float = 300,
    ) -> AbstractContextManager[RuntimeStore]: ...


@pytest.fixture(params=("sqlite", "postgres"))
def store_factory(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[StoreFactory]:
    backend = str(request.param)
    if backend == "sqlite":
        database = tmp_path / "contract.db"

        @contextmanager
        def open_sqlite(
            *,
            clock: Clock,
            lease_seconds: float = 300,
        ) -> Iterator[RuntimeStore]:
            with SQLiteRuntimeStore(
                database,
                clock_ns=clock,
                execution_lease_seconds=lease_seconds,
            ) as store:
                yield store

        yield open_sqlite
        return

    dsn = os.environ.get("AGENTBARRIER_TEST_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("AGENTBARRIER_TEST_POSTGRES_DSN is not configured")
    schema = f"agentbarrier_test_{uuid.uuid4().hex}"
    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        create_schema=True,
        migrate=True,
    ):
        pass

    @contextmanager
    def open_postgres(
        *,
        clock: Clock,
        lease_seconds: float = 300,
    ) -> Iterator[RuntimeStore]:
        with PostgresRuntimeStore(
            dsn,
            schema=schema,
            clock_ns=clock,
            execution_lease_seconds=lease_seconds,
        ) as store:
            yield store

    try:
        yield open_postgres
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def make_request(
    clock: Clock,
    *,
    action_id: str = "action-1",
    key: str = "refund-1",
    amount: int = 50,
) -> RuntimeRequest:
    return RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": key, "amount": amount},
        idempotency_key=key,
        policy_version="contract-v1",
        created_at_ns=clock(),
    )


def approval(ttl: float | None = None) -> PolicyDecision:
    return PolicyDecision(
        PolicyEffect.REQUIRE_APPROVAL,
        "review refunds",
        "contract-v1",
        ttl,
    )


def test_runtime_store_contract_approval_execution_and_replay(
    store_factory: StoreFactory,
) -> None:
    clock = Clock()
    with store_factory(clock=clock) as store:
        request = make_request(clock)
        pending = store.submit(request, approval())
        assert pending.status is RuntimeStatus.PENDING
        assert store.submit(request, approval()).action_id == pending.action_id
        with pytest.raises(ApprovalRequired):
            store.claim(pending.action_id, request_digest=request.request_digest)

        approved = store.decide(
            pending.action_id,
            Decision.APPROVE,
            decided_by="contract-reviewer",
            reason="ticket-123",
        )
        assert approved.status is RuntimeStatus.APPROVED
        assert approved.decided_by == "contract-reviewer"
        assert store.claim(pending.action_id, request_digest=request.request_digest).outcome is (
            ClaimOutcome.EXECUTE
        )
        completed = store.complete(
            pending.action_id,
            request_digest=request.request_digest,
            result={"refunded": True},
        )
        assert completed.status is RuntimeStatus.SUCCEEDED
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


def test_runtime_store_contract_pause_limit_and_reconciliation(
    store_factory: StoreFactory,
) -> None:
    clock = Clock()
    with store_factory(clock=clock) as store:
        store.set_pause(
            namespace="billing",
            tool_name="payments.refund",
            paused_by="on-call",
            reason="provider incident",
        )
        store.configure_limit(
            "refund-budget",
            namespace="billing",
            tool_name="payments.refund",
            window_seconds=60,
            max_actions=2,
            value_argument="amount",
            max_value=100,
            updated_by="risk-team",
            reason="blast radius",
        )
        assert store.clear_pause(
            namespace="billing",
            tool_name="payments.refund",
            resumed_by="on-call",
            reason="provider recovered",
        )

        first_request = make_request(clock, action_id="uncertain", key="uncertain", amount=80)
        first = store.submit(first_request, approval())
        store.decide(first.action_id, Decision.APPROVE, decided_by="contract-reviewer")
        store.claim(first.action_id, request_digest=first_request.request_digest)
        store.mark_unknown(
            first.action_id,
            request_digest=first_request.request_digest,
            error="TimeoutError",
        )

        second_request = make_request(clock, action_id="second", key="second", amount=30)
        second = store.submit(
            second_request,
            PolicyDecision(PolicyEffect.ALLOW, "allow", "contract-v1"),
        )
        with pytest.raises(ActionLimitExceeded):
            store.claim(second.action_id, request_digest=second_request.request_digest)

        store.reconcile(
            first.action_id,
            RuntimeReconciliation.NOT_COMMITTED,
            resolved_by="payment-ledger",
            reason="transaction is absent",
        )
        usage = store.limit_usage("refund-budget")[0]
        assert (usage.actions_used, usage.value_used) == (0, 0)
        second_claim = store.claim(
            second.action_id,
            request_digest=second_request.request_digest,
        )
        assert second_claim.outcome is ClaimOutcome.EXECUTE
        assert store.verify_control_receipt_chain()
        assert store.verify_receipt_chain()


def test_runtime_store_contract_serializes_cross_connection_claims(
    store_factory: StoreFactory,
) -> None:
    clock = Clock()
    with store_factory(clock=clock) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "allow", "contract-v1"),
        )

    ready = threading.Barrier(3)

    def claim() -> str:
        with store_factory(clock=clock) as candidate:
            ready.wait(timeout=10)
            try:
                return candidate.claim(
                    action.action_id,
                    request_digest=request.request_digest,
                ).outcome.value
            except ActionInProgress:
                return "in_progress"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        ready.wait(timeout=10)
        outcomes = sorted(future.result(timeout=10) for future in futures)

    assert outcomes == ["execute", "in_progress"]
    with store_factory(clock=clock) as store:
        assert store.verify_receipt_chain()


def test_runtime_store_contract_enforces_atomic_cross_connection_limit(
    store_factory: StoreFactory,
) -> None:
    clock = Clock()
    requests = [
        make_request(clock, action_id=f"action-{index}", key=f"key-{index}", amount=1)
        for index in range(2)
    ]
    with store_factory(clock=clock) as store:
        store.configure_limit(
            "one-refund",
            window_seconds=60,
            max_actions=1,
            updated_by="risk-team",
            reason="concurrency proof",
        )
        actions = [
            store.submit(
                item,
                PolicyDecision(PolicyEffect.ALLOW, "allow", "contract-v1"),
            )
            for item in requests
        ]

    ready = threading.Barrier(3)

    def claim(index: int) -> str:
        with store_factory(clock=clock) as candidate:
            ready.wait(timeout=10)
            try:
                return candidate.claim(
                    actions[index].action_id,
                    request_digest=requests[index].request_digest,
                ).outcome.value
            except ActionLimitExceeded:
                return "limited"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, index) for index in range(2)]
        ready.wait(timeout=10)
        outcomes = sorted(future.result(timeout=10) for future in futures)

    assert outcomes == ["execute", "limited"]
    with store_factory(clock=clock) as store:
        usage = store.limit_usage("one-refund")[0]
        assert usage.actions_used == 1
        assert store.verify_receipt_chain()


def test_runtime_store_contract_expires_lease_fail_closed(
    store_factory: StoreFactory,
) -> None:
    clock = Clock()
    with store_factory(clock=clock, lease_seconds=1) as store:
        request = make_request(clock)
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "allow", "contract-v1"),
        )
        store.claim(action.action_id, request_digest=request.request_digest)
        clock.advance(1)
        abandoned = store.get_action(action.action_id)
        assert abandoned.status is RuntimeStatus.UNKNOWN
        assert abandoned.error == "ExecutionLeaseExpired"
        assert store.receipts(action_id=action.action_id)[-1].event.value == ("execution_abandoned")


def test_runtime_store_factory_requires_one_secret_safe_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="exactly one"), open_runtime_store():
        pass
    with (
        pytest.raises(ValueError, match="exactly one"),
        open_runtime_store(
            database_path=tmp_path / "runtime.db",
            postgres_dsn_env="AGENTBARRIER_DATABASE_URL",
        ),
    ):
        pass
    with (
        pytest.raises(ValueError, match="environment name"),
        open_runtime_store(postgres_dsn_env="INVALID-NAME"),
    ):
        pass
    monkeypatch.delenv("AGENTBARRIER_MISSING_DATABASE_URL", raising=False)
    with (
        pytest.raises(ValueError, match="is not set"),
        open_runtime_store(postgres_dsn_env="AGENTBARRIER_MISSING_DATABASE_URL"),
    ):
        pass

    with open_runtime_store(database_path=tmp_path / "runtime.db") as store:
        assert store.schema_version == "5"

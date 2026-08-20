from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.journal import EffectJournal
from agentbarrier.models import (
    ActionRequest,
    AuditEvent,
    Capability,
    ReconciliationStatus,
    RunStatus,
)
from agentbarrier.probe import EffectProbe


def test_reference_adapter_passes_complete_suite() -> None:
    from agentbarrier.runner import SuiteRunner

    suite = SuiteRunner().verify_sync(ReferenceAdapter())

    assert suite.passed
    assert suite.passed_count == 11
    assert suite.skipped_count == 0


def test_reference_adapter_rejects_empty_run(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = ReferenceAdapter()
        with EffectJournal(tmp_path / "empty.sqlite3") as journal:
            probe = EffectProbe(journal, run_id="empty")
            with pytest.raises(AdapterContractError, match="at least one action"):
                await adapter.begin(run_id="empty", actions=[], effect=probe)
            duplicate = ActionRequest("same", "write", {})
            with pytest.raises(AdapterContractError, match="must be unique"):
                await adapter.begin(
                    run_id="duplicates", actions=[duplicate, duplicate], effect=probe
                )
            missing_parent = ActionRequest("child", "write", {}, parent_action_id="missing")
            with pytest.raises(AdapterContractError, match="does not reference"):
                await adapter.begin(run_id="missing-parent", actions=[missing_parent], effect=probe)
            self_parent = ActionRequest("self", "write", {}, parent_action_id="self")
            with pytest.raises(AdapterContractError, match="delegate to itself"):
                await adapter.begin(run_id="self-parent", actions=[self_parent], effect=probe)
            first = ActionRequest("first", "write", {}, parent_action_id="second")
            second = ActionRequest("second", "write", {}, parent_action_id="first")
            with pytest.raises(AdapterContractError, match="cycles"):
                await adapter.begin(run_id="cycle", actions=[first, second], effect=probe)
            valid = ActionRequest("valid", "write", {}, requires_approval=False)
            for timeout in (0.0, -1.0, float("nan"), float("inf")):
                with pytest.raises(AdapterContractError, match="finite and positive"):
                    await adapter.begin(
                        run_id="invalid-timeout",
                        actions=[valid],
                        effect=probe,
                        timeout_seconds=timeout,
                    )

    asyncio.run(exercise())


def test_reference_handle_contract_errors(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = ReferenceAdapter()
        with EffectJournal(tmp_path / "events.sqlite3") as journal:
            probe = EffectProbe(journal, run_id="run")
            plain = ActionRequest("plain", "write", {}, requires_approval=False)
            handle = await adapter.begin(run_id="run", actions=[plain], effect=probe)
            with pytest.raises(AdapterContractError, match="no approval"):
                await handle.wait_for_pending(0.1)
            assert (await handle.wait(1)).status is RunStatus.COMPLETED

            with pytest.raises(AdapterContractError, match="not pending"):
                await handle.approve("missing")

            gated = ActionRequest("gated", "write", {})
            other = ActionRequest("other", "write", {})
            gated_handle = await adapter.begin(run_id="gated-run", actions=[gated], effect=probe)
            await gated_handle.wait_for_pending(1)
            with pytest.raises(AdapterContractError, match="preserve action_id"):
                await gated_handle.approve("gated", other)
            wrong_tool = ActionRequest("gated", "different_tool", {})
            with pytest.raises(AdapterContractError, match="preserve tool_name"):
                await gated_handle.approve("gated", wrong_tool)
            await gated_handle.reject("gated")
            with pytest.raises(AdapterContractError, match="already has a decision"):
                await gated_handle.reject("gated")
            await gated_handle.wait(1)
            receipts = await gated_handle.audit_receipts()
            assert any(receipt.action_id == "gated" for receipt in receipts)

    asyncio.run(exercise())


def test_reference_timeout_covers_approval_wait(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = ReferenceAdapter()
        with EffectJournal(tmp_path / "approval-timeout.sqlite3") as journal:
            probe = EffectProbe(journal, run_id="approval-timeout")
            request = ActionRequest("gated", "write", {})
            handle = await adapter.begin(
                run_id="approval-timeout",
                actions=[request],
                effect=probe,
                timeout_seconds=0.01,
            )
            await handle.wait_for_pending(1)

            outcome = await handle.wait(1)

            assert outcome.status is RunStatus.TIMED_OUT
            assert journal.committed(run_id="approval-timeout") == ()

    asyncio.run(exercise())


def test_reference_reconciliation_is_bounded_cached_and_audited(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = ReferenceAdapter()
        with EffectJournal(tmp_path / "reconciliation.sqlite3") as journal:
            request = ActionRequest("payment:stable", "charge", {"amount": 7}, False)
            probe = EffectProbe(journal, run_id="reconciliation", raise_after_commit=True)
            handle = await adapter.begin(
                run_id="reconciliation",
                actions=[request],
                effect=probe,
            )
            with pytest.raises(AdapterContractError, match="terminal state"):
                await handle.reconcile(request.action_id, 0.1)
            assert (await handle.wait(1)).status is RunStatus.UNKNOWN
            with pytest.raises(AdapterContractError, match="does not identify"):
                await handle.reconcile("missing", 0.1)

            evidence = await handle.reconcile(request.action_id, 0.1)
            assert evidence.status is ReconciliationStatus.COMMITTED
            assert await handle.reconcile(request.action_id, 0.1) is evidence
            receipts = await handle.audit_receipts()
            action_events = [
                receipt.event for receipt in receipts if receipt.action_id == request.action_id
            ]
            assert action_events.count(AuditEvent.RECONCILIATION_STARTED) == 1
            assert action_events.count(AuditEvent.RECONCILIATION_COMMITTED) == 1
            await handle.close()

    asyncio.run(exercise())


def test_reference_reconciliation_preserves_unknown_when_evidence_is_unavailable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        adapter = ReferenceAdapter()
        with EffectJournal(tmp_path / "unavailable.sqlite3") as journal:
            request = ActionRequest("payment:unavailable", "charge", {"amount": 9}, False)
            probe = EffectProbe(
                journal,
                run_id="unavailable",
                raise_after_commit=True,
                reconciliation_available=False,
            )
            handle = await adapter.begin(
                run_id="unavailable",
                actions=[request],
                effect=probe,
            )
            assert (await handle.wait(1)).status is RunStatus.UNKNOWN

            evidence = await handle.reconcile(request.action_id, 0.1)

            assert evidence.status is ReconciliationStatus.UNAVAILABLE
            assert (await handle.wait(1)).status is RunStatus.UNKNOWN
            receipts = await handle.audit_receipts()
            assert any(
                receipt.event is AuditEvent.RECONCILIATION_UNAVAILABLE
                and receipt.action_id == request.action_id
                for receipt in receipts
            )
            assert len(journal.committed(run_id="unavailable")) == 1
            await handle.close()

    asyncio.run(exercise())


def test_require_reports_unsupported_capability() -> None:
    adapter = ReferenceAdapter()
    adapter.capabilities = frozenset({Capability.APPROVAL})
    with pytest.raises(UnsupportedCapability, match="replay"):
        adapter.require(Capability.REPLAY)

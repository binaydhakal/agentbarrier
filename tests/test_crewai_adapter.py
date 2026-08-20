from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentbarrier.adapters.crewai import CrewAIAdapter
from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.journal import EffectJournal
from agentbarrier.models import (
    ActionRequest,
    ApprovalBarrierProfile,
    Capability,
    ScenarioStatus,
)
from agentbarrier.probe import EffectProbe
from agentbarrier.runner import RunnerOptions, SuiteRunner

pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:function_calling_llm is deprecated.*:DeprecationWarning:crewai\\..*"
    ),
    pytest.mark.filterwarnings("ignore:deprecated:DeprecationWarning:crewai\\..*"),
]


@pytest.mark.integration
def test_crewai_adapter_exercises_real_hook_lifecycle_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("crewai", reason="CrewAI optional dependency is not installed")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "false")
    monkeypatch.setenv("CREWAI_STORAGE_DIR", "/path/that/must/not/be-used")
    before = {
        "OTEL_SDK_DISABLED": os.environ.get("OTEL_SDK_DISABLED"),
        "CREWAI_DISABLE_TELEMETRY": os.environ.get("CREWAI_DISABLE_TELEMETRY"),
        "CREWAI_STORAGE_DIR": os.environ.get("CREWAI_STORAGE_DIR"),
    }
    adapter = CrewAIAdapter()
    suite = SuiteRunner(
        RunnerOptions(
            settle_seconds=0.01,
            operation_timeout_seconds=5.0,
            tool_timeout_seconds=0.05,
        )
    ).verify_sync(adapter)

    assert suite.error_count == 0
    skipped = {
        result.scenario_id for result in suite.results if result.status is ScenarioStatus.SKIPPED
    }
    assert skipped == {
        "replay",
        "outcome_ambiguity",
        "outcome_reconciliation",
        "cancellation",
        "timeout",
        "delegation",
        "audit_receipts",
    }
    exercised = [
        result
        for result in suite.results
        if result.scenario_id in {capability.value for capability in adapter.capabilities}
    ]
    assert exercised
    assert all(result.status is not ScenarioStatus.SKIPPED for result in exercised)
    failed = {
        result.scenario_id: result.finding.code
        for result in exercised
        if result.status is ScenarioStatus.FAILED and result.finding is not None
    }
    assert failed == {"parallel_barrier": "AB010"}

    per_action = SuiteRunner(
        RunnerOptions(
            settle_seconds=0.01,
            operation_timeout_seconds=5.0,
            scenarios=("parallel_barrier",),
            approval_profile=ApprovalBarrierProfile.PER_ACTION,
        )
    ).verify_sync(adapter)
    assert per_action.passed
    assert os.environ.get("OTEL_SDK_DISABLED") == before["OTEL_SDK_DISABLED"]
    assert os.environ.get("CREWAI_DISABLE_TELEMETRY") == before["CREWAI_DISABLE_TELEMETRY"]
    assert os.environ.get("CREWAI_STORAGE_DIR") == before["CREWAI_STORAGE_DIR"]


@pytest.mark.integration
def test_crewai_adapter_contract_errors() -> None:
    pytest.importorskip("crewai", reason="CrewAI optional dependency is not installed")

    async def exercise() -> None:
        adapter = CrewAIAdapter()
        with (
            TemporaryDirectory() as directory,
            EffectJournal(Path(directory) / "events.db") as journal,
        ):
            probe = EffectProbe(journal, run_id="run")
            with pytest.raises(AdapterContractError, match="at least one"):
                await adapter.begin(run_id="run", actions=[], effect=probe)

            first = ActionRequest("first", "sentinel_write", {"recipient": "a", "amount": 1})
            other_tool = ActionRequest("second", "other_tool", {"recipient": "b", "amount": 2})
            with pytest.raises(AdapterContractError, match="shared tool_name"):
                await adapter.begin(run_id="run", actions=[first, other_tool], effect=probe)

            invalid = ActionRequest("invalid", "sentinel_write", {"recipient": "a"})
            with pytest.raises(AdapterContractError, match="exactly"):
                await adapter.begin(run_id="run", actions=[invalid], effect=probe)
            invalid_recipient = ActionRequest(
                "invalid-recipient",
                "sentinel_write",
                {"recipient": 7, "amount": 1},
            )
            with pytest.raises(AdapterContractError, match="recipient"):
                await adapter.begin(run_id="run", actions=[invalid_recipient], effect=probe)
            invalid_amount = ActionRequest(
                "invalid-amount",
                "sentinel_write",
                {"recipient": "a", "amount": True},
            )
            with pytest.raises(AdapterContractError, match="amount"):
                await adapter.begin(run_id="run", actions=[invalid_amount], effect=probe)

            handle = await adapter.begin(run_id="run", actions=[first], effect=probe)
            await handle.wait_for_pending(3)
            wrong_id = ActionRequest("different", "sentinel_write", {"recipient": "b", "amount": 2})
            with pytest.raises(AdapterContractError, match="preserve action_id"):
                await handle.approve("first", wrong_id)
            wrong_tool = ActionRequest("first", "other_tool", {"recipient": "b", "amount": 2})
            with pytest.raises(AdapterContractError, match="preserve tool_name"):
                await handle.approve("first", wrong_tool)
            with pytest.raises(AdapterContractError, match="not pending"):
                await handle.approve("missing")
            await handle.reject("first")
            with pytest.raises(AdapterContractError, match="already has a decision"):
                await handle.reject("first")
            assert (await handle.wait(3)).status.value == "completed"
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


@pytest.mark.integration
def test_crewai_adapter_fails_closed_on_unknown_runtime_action() -> None:
    pytest.importorskip("crewai", reason="CrewAI optional dependency is not installed")

    async def exercise() -> None:
        adapter = CrewAIAdapter()
        with (
            TemporaryDirectory() as directory,
            EffectJournal(Path(directory) / "events.db") as journal,
        ):
            action = ActionRequest("known", "sentinel_write", {"recipient": "a", "amount": 1})
            probe = EffectProbe(journal, run_id="run")
            handle = await adapter.begin(run_id="run", actions=[action], effect=probe)
            await handle.wait_for_pending(3)
            intercept = handle._intercept_tool_call  # type: ignore[attr-defined]
            blocked = await asyncio.to_thread(
                intercept,
                "sentinel_write",
                {"action_id": "unknown", "recipient": "a", "amount": 1},
            )
            assert blocked is False
            outcome = await handle.wait(3)
            assert outcome.status.value == "failed"
            assert "unknown action" in (outcome.detail or "")
            assert journal.committed(run_id="run") == ()
            await handle.close()

    asyncio.run(exercise())


@pytest.mark.integration
def test_crewai_handle_stops_its_sentinel_on_timeout_and_cancel() -> None:
    pytest.importorskip("crewai", reason="CrewAI optional dependency is not installed")

    async def exercise() -> None:
        adapter = CrewAIAdapter()
        with (
            TemporaryDirectory() as directory,
            EffectJournal(Path(directory) / "events.db") as journal,
        ):
            timeout_action = ActionRequest(
                "timeout", "sentinel_write", {"recipient": "a", "amount": 1}, False
            )
            timeout_probe = EffectProbe(
                journal,
                run_id="timeout-run",
                block_before_commit=True,
            )
            timed = await adapter.begin(
                run_id="timeout-run",
                actions=[timeout_action],
                effect=timeout_probe,
                timeout_seconds=0.05,
            )
            assert (await timed.wait(3)).status.value == "timed_out"
            timeout_probe.release()
            await timed.close()
            assert journal.committed(run_id="timeout-run") == ()

            cancel_action = ActionRequest(
                "cancel", "sentinel_write", {"recipient": "a", "amount": 1}
            )
            cancel_probe = EffectProbe(journal, run_id="cancel-run")
            cancelled = await adapter.begin(
                run_id="cancel-run",
                actions=[cancel_action],
                effect=cancel_probe,
            )
            await cancelled.wait_for_pending(3)
            assert (await cancelled.wait(0.001)).status.value == "failed"
            await cancelled.cancel()
            assert (await cancelled.wait(3)).status.value == "cancelled"
            assert await cancelled.audit_receipts() == ()
            await cancelled.cancel()
            await cancelled.close()

    asyncio.run(exercise())


def test_crewai_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = CrewAIAdapter.capabilities
    assert Capability.APPROVAL in capabilities
    assert Capability.REJECTION in capabilities
    assert Capability.ARGUMENT_BINDING in capabilities
    assert Capability.PARALLEL_BARRIER in capabilities
    assert Capability.CANCELLATION not in capabilities
    assert Capability.TIMEOUT not in capabilities
    assert Capability.REPLAY not in capabilities

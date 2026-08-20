from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentbarrier.adapters.google_adk import GoogleADKAdapter
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


@pytest.mark.integration
def test_google_adk_adapter_exercises_declared_lifecycles_without_credentials() -> None:
    pytest.importorskip("google.adk", reason="Google ADK optional dependency is not installed")
    adapter = GoogleADKAdapter()
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
        "argument_binding",
        "replay",
        "outcome_ambiguity",
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


@pytest.mark.integration
def test_google_adk_adapter_contract_errors() -> None:
    pytest.importorskip("google.adk", reason="Google ADK optional dependency is not installed")

    async def exercise() -> None:
        adapter = GoogleADKAdapter()
        with (
            TemporaryDirectory() as directory,
            EffectJournal(Path(directory) / "events.db") as journal,
        ):
            probe = EffectProbe(journal, run_id="run")
            with pytest.raises(AdapterContractError, match="at least one"):
                await adapter.begin(run_id="run", actions=[], effect=probe)

            first = ActionRequest("first", "sentinel_write", {"recipient": "a", "amount": 1})
            second = ActionRequest("second", "other_tool", {"recipient": "b", "amount": 2})
            with pytest.raises(AdapterContractError, match="shared tool_name"):
                await adapter.begin(run_id="run", actions=[first, second], effect=probe)

            handle = await adapter.begin(run_id="run", actions=[first], effect=probe)
            await handle.wait_for_pending(2)
            with pytest.raises(UnsupportedCapability, match="argument editing"):
                await handle.approve("first", first.with_arguments({"recipient": "b"}))
            await handle.reject("first")
            with pytest.raises(AdapterContractError, match="already has a decision"):
                await handle.reject("first")
            await handle.wait(2)
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


def test_google_adk_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = GoogleADKAdapter.capabilities
    assert Capability.ARGUMENT_BINDING not in capabilities
    assert Capability.REPLAY not in capabilities
    assert Capability.APPROVAL in capabilities

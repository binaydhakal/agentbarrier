from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentbarrier.adapters.pydantic_ai import PydanticAIAdapter
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
def test_pydantic_ai_adapter_exercises_declared_lifecycles_without_credentials() -> None:
    pytest.importorskip("pydantic_ai", reason="PydanticAI optional dependency is not installed")
    adapter = PydanticAIAdapter()
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
def test_pydantic_ai_adapter_contract_errors() -> None:
    pytest.importorskip("pydantic_ai", reason="PydanticAI optional dependency is not installed")

    async def exercise() -> None:
        adapter = PydanticAIAdapter()
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
            await handle.wait_for_pending(1)
            replacement = ActionRequest(
                "different", "sentinel_write", {"recipient": "b", "amount": 2}
            )
            with pytest.raises(AdapterContractError, match="preserve action_id"):
                await handle.approve("first", replacement)
            await handle.reject("first")
            with pytest.raises(AdapterContractError, match="already has a decision"):
                await handle.reject("first")
            await handle.wait(1)
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


def test_pydantic_ai_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = PydanticAIAdapter.capabilities
    assert Capability.ARGUMENT_BINDING in capabilities
    assert Capability.REPLAY not in capabilities
    assert Capability.APPROVAL in capabilities

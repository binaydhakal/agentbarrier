from __future__ import annotations

import asyncio

import pytest

from agentbarrier.adapters.openai_agents import OpenAIAgentsAdapter
from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.models import ApprovalBarrierProfile, Capability, ScenarioStatus
from agentbarrier.runner import RunnerOptions, SuiteRunner


@pytest.mark.integration
def test_openai_agents_adapter_exercises_declared_lifecycles_without_credentials() -> None:
    pytest.importorskip("agents", reason="openai-agents optional dependency is not installed")
    adapter = OpenAIAgentsAdapter()
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
def test_openai_adapter_contract_errors() -> None:
    pytest.importorskip("agents", reason="openai-agents optional dependency is not installed")

    async def exercise() -> None:
        adapter = OpenAIAgentsAdapter()
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from agentbarrier.journal import EffectJournal
        from agentbarrier.models import ActionRequest
        from agentbarrier.probe import EffectProbe

        with (
            TemporaryDirectory() as directory,
            EffectJournal(Path(directory) / "events.db") as journal,
        ):
            probe = EffectProbe(journal, run_id="run")
            with pytest.raises(AdapterContractError, match="at least one"):
                await adapter.begin(run_id="run", actions=[], effect=probe)

            request = ActionRequest("action", "sentinel_write", {"recipient": "a", "amount": 1})
            handle = await adapter.begin(run_id="run", actions=[request], effect=probe)
            await handle.wait_for_pending(1)
            with pytest.raises(UnsupportedCapability, match="argument editing"):
                await handle.approve("action", request.with_arguments({"recipient": "b"}))
            await handle.reject("action")
            await handle.wait(1)
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


def test_openai_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = OpenAIAgentsAdapter.capabilities
    assert Capability.ARGUMENT_BINDING not in capabilities
    assert Capability.REPLAY not in capabilities
    assert Capability.APPROVAL in capabilities

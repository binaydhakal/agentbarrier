from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentbarrier.adapters.langgraph import LangGraphAdapter
from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.journal import EffectJournal
from agentbarrier.models import ActionRequest, Capability, ScenarioStatus
from agentbarrier.probe import EffectProbe
from agentbarrier.runner import RunnerOptions, SuiteRunner


@pytest.mark.integration
@pytest.mark.skipif(sys.version_info < (3, 11), reason="LangGraph async interrupts require 3.11+")
def test_langgraph_adapter_exercises_declared_lifecycles_without_credentials() -> None:
    pytest.importorskip("langgraph", reason="LangGraph optional dependency is not installed")
    adapter = LangGraphAdapter()
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
    assert skipped == {"replay", "outcome_ambiguity", "delegation", "audit_receipts"}
    exercised = [
        result
        for result in suite.results
        if result.scenario_id in {capability.value for capability in adapter.capabilities}
    ]
    assert exercised
    assert all(result.status is not ScenarioStatus.SKIPPED for result in exercised)


@pytest.mark.integration
@pytest.mark.skipif(sys.version_info < (3, 11), reason="LangGraph async interrupts require 3.11+")
def test_langgraph_adapter_contract_errors() -> None:
    pytest.importorskip("langgraph", reason="LangGraph optional dependency is not installed")

    async def exercise() -> None:
        adapter = LangGraphAdapter()
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
            replacement = ActionRequest(
                "different", "sentinel_write", {"recipient": "b", "amount": 2}
            )
            with pytest.raises(AdapterContractError, match="preserve action_id"):
                await handle.approve("action", replacement)
            await handle.reject("action")
            await handle.wait(1)
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


def test_langgraph_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = LangGraphAdapter.capabilities
    assert Capability.ARGUMENT_BINDING in capabilities
    assert Capability.REPLAY not in capabilities
    assert Capability.APPROVAL in capabilities

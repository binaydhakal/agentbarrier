from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest

from agentbarrier.adapters.autogen import (
    AutoGenAdapter,
    _decode_arguments,
    _json_mapping,
    _load_sdk,
)
from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.journal import EffectJournal
from agentbarrier.models import ActionRequest, Capability, ScenarioStatus
from agentbarrier.probe import EffectProbe
from agentbarrier.runner import RunnerOptions, SuiteRunner


@pytest.mark.integration
def test_autogen_adapter_exercises_declared_lifecycles_without_credentials() -> None:
    pytest.importorskip("autogen_core", reason="AutoGen Core optional dependency is not installed")
    adapter = AutoGenAdapter()
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
    failed = {
        result.scenario_id: result.finding.code
        for result in exercised
        if result.status is ScenarioStatus.FAILED and result.finding is not None
    }
    assert failed == {}


@pytest.mark.integration
def test_autogen_adapter_contract_errors() -> None:
    pytest.importorskip("autogen_core", reason="AutoGen Core optional dependency is not installed")

    async def exercise() -> None:
        adapter = AutoGenAdapter()
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
            with pytest.raises(AdapterContractError, match="not pending approval"):
                await handle.approve("missing")
            with pytest.raises(AdapterContractError, match="not pending approval"):
                await handle.approve("missing", first)
            replacement = ActionRequest(
                "different", "sentinel_write", {"recipient": "b", "amount": 2}
            )
            with pytest.raises(AdapterContractError, match="preserve action_id"):
                await handle.approve("first", replacement)
            wrong_tool = ActionRequest("first", "other_tool", {"recipient": "b", "amount": 2})
            with pytest.raises(AdapterContractError, match="preserve tool_name"):
                await handle.approve("first", wrong_tool)

            sdk = _load_sdk()
            intercept = cast(Any, handle)._intercept
            malformed_calls = (
                sdk["FunctionCall"](
                    id="unknown",
                    name="sentinel_write",
                    arguments='{"action_id": "unknown"}',
                ),
                sdk["FunctionCall"](
                    id="first",
                    name="other_tool",
                    arguments='{"action_id": "first"}',
                ),
                sdk["FunctionCall"](
                    id="first",
                    name="sentinel_write",
                    arguments='{"action_id": "different"}',
                ),
                sdk["FunctionCall"](
                    id="first",
                    name="sentinel_write",
                    arguments='{"action_id": "first", "recipient": "a", "amount": 1}',
                ),
            )
            for call in malformed_calls:
                with pytest.raises(sdk["ToolException"]):
                    await intercept(call, sdk)

            await handle.reject("first")
            with pytest.raises(AdapterContractError, match="already has a decision"):
                await handle.reject("first")
            await handle.wait(2)
            assert await handle.audit_receipts() == ()
            with pytest.raises(UnsupportedCapability, match="replay"):
                await handle.replay()
            await handle.close()

    asyncio.run(exercise())


def test_autogen_argument_validation_rejects_malformed_payloads() -> None:
    with pytest.raises(AdapterContractError, match="invalid JSON"):
        _decode_arguments("{")
    with pytest.raises(AdapterContractError, match="non-object"):
        _decode_arguments("[]")
    with pytest.raises(AdapterContractError, match="keys must be strings"):
        _json_mapping({1: "value"})


def test_autogen_adapter_declares_only_exercisable_capabilities() -> None:
    capabilities = AutoGenAdapter.capabilities
    assert Capability.ARGUMENT_BINDING in capabilities
    assert Capability.REPLAY not in capabilities
    assert Capability.APPROVAL in capabilities

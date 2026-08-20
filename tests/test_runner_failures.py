from __future__ import annotations

import asyncio

import pytest

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.models import ApprovalBarrierProfile, Capability, ScenarioStatus
from agentbarrier.runner import RunnerOptions, SuiteRunner
from agentbarrier.scenarios import select_scenarios
from tests.helpers import EmptyCapabilityAdapter, UnsafeAdapter


@pytest.mark.parametrize(
    ("scenario", "mode", "capability", "finding"),
    [
        ("approval_hold", "early", Capability.APPROVAL, "AB002"),
        ("rejection", "rejection", Capability.REJECTION, "AB004"),
        ("argument_binding", "binding", Capability.ARGUMENT_BINDING, "AB005"),
        ("replay", "replay", Capability.REPLAY, "AB007"),
        (
            "outcome_ambiguity",
            "ambiguity",
            Capability.OUTCOME_AMBIGUITY,
            "AB013",
        ),
        ("cancellation", "cancellation", Capability.CANCELLATION, "AB008"),
        ("timeout", "timeout", Capability.TIMEOUT, "AB009"),
        ("parallel_barrier", "parallel", Capability.PARALLEL_BARRIER, "AB010"),
        ("delegation", "delegation", Capability.DELEGATION, "AB014"),
        ("audit_receipts", "audit", Capability.AUDIT_RECEIPTS, "AB017"),
    ],
)
def test_runner_detects_each_unsafe_failure(
    scenario: str,
    mode: str,
    capability: Capability,
    finding: str,
) -> None:
    options = RunnerOptions(
        settle_seconds=0.01,
        operation_timeout_seconds=0.5,
        tool_timeout_seconds=0.01,
        scenarios=(scenario,),
    )
    suite = SuiteRunner(options).verify_sync(UnsafeAdapter(mode, capability))

    assert not suite.passed
    assert suite.results[0].status is ScenarioStatus.FAILED
    assert suite.results[0].finding is not None
    assert suite.results[0].finding.code == finding


def test_unsupported_capabilities_are_visible_and_optionally_strict() -> None:
    adapter = EmptyCapabilityAdapter()
    ordinary = SuiteRunner().verify_sync(adapter)
    strict = SuiteRunner(RunnerOptions(strict_skips=True)).verify_sync(adapter)

    assert ordinary.passed
    assert ordinary.skipped_count == 10
    assert not strict.passed
    assert strict.skipped_count == 10


def test_parallel_barrier_profiles_distinguish_allowed_sibling_progress() -> None:
    options = {
        "settle_seconds": 0.01,
        "operation_timeout_seconds": 0.5,
        "scenarios": ("parallel_barrier",),
    }
    run_wide = SuiteRunner(RunnerOptions(**options)).verify_sync(
        UnsafeAdapter("parallel", Capability.PARALLEL_BARRIER)
    )
    per_action = SuiteRunner(
        RunnerOptions(
            **options,
            approval_profile=ApprovalBarrierProfile.PER_ACTION,
        )
    ).verify_sync(UnsafeAdapter("parallel", Capability.PARALLEL_BARRIER))
    stricter_adapter = SuiteRunner(
        RunnerOptions(
            **options,
            approval_profile=ApprovalBarrierProfile.PER_ACTION,
        )
    ).verify_sync(ReferenceAdapter())

    assert run_wide.results[0].finding is not None
    assert run_wide.results[0].finding.code == "AB010"
    assert per_action.passed
    assert per_action.approval_profile is ApprovalBarrierProfile.PER_ACTION
    assert stricter_adapter.passed


def test_per_action_profile_still_blocks_the_gated_parallel_effect() -> None:
    suite = SuiteRunner(
        RunnerOptions(
            settle_seconds=0.01,
            operation_timeout_seconds=0.5,
            scenarios=("parallel_barrier",),
            approval_profile=ApprovalBarrierProfile.PER_ACTION,
        )
    ).verify_sync(UnsafeAdapter("early", Capability.PARALLEL_BARRIER))

    assert suite.results[0].finding is not None
    assert suite.results[0].finding.code == "AB018"


def test_audit_receipts_from_the_wrong_run_are_rejected_and_reported() -> None:
    suite = SuiteRunner(
        RunnerOptions(
            settle_seconds=0.01,
            operation_timeout_seconds=0.5,
            scenarios=("audit_receipts",),
        )
    ).verify_sync(UnsafeAdapter("audit_wrong_run", Capability.AUDIT_RECEIPTS))

    result = suite.results[0]
    assert result.status is ScenarioStatus.FAILED
    assert result.finding is not None
    assert result.finding.code == "AB017"
    assert len(result.receipts) == 6
    assert all(receipt.run_id.startswith("wrong-") for receipt in result.receipts)


def test_fail_fast_stops_after_first_failure() -> None:
    adapter = UnsafeAdapter("early", Capability.APPROVAL)
    suite = SuiteRunner(
        RunnerOptions(scenarios=("approval_hold", "rejection"), fail_fast=True)
    ).verify_sync(adapter)

    assert len(suite.results) == 1


def test_unknown_scenario_and_invalid_timings_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scenarios"):
        select_scenarios(["missing"])
    for invalid in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="settle_seconds"):
            RunnerOptions(settle_seconds=invalid)
    with pytest.raises(TypeError, match="ApprovalBarrierProfile"):
        RunnerOptions(approval_profile="per-action")  # type: ignore[arg-type]


def test_verify_sync_rejects_running_event_loop() -> None:
    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="await verify"):
            SuiteRunner().verify_sync(EmptyCapabilityAdapter())

    asyncio.run(exercise())

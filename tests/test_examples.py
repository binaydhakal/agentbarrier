from __future__ import annotations

from agentbarrier.runner import RunnerOptions, SuiteRunner
from examples.unsafe_approval import UnsafeApprovalAdapter


def test_unsafe_approval_example_produces_ab002() -> None:
    suite = SuiteRunner(RunnerOptions(scenarios=("approval_hold",))).verify_sync(
        UnsafeApprovalAdapter()
    )

    assert suite.exit_code == 1
    assert suite.results[0].finding is not None
    assert suite.results[0].finding.code == "AB002"

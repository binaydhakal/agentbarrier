from __future__ import annotations

import pytest

from agentbarrier.runner import RunnerOptions, SuiteRunner
from examples.run_payment_ledger import main as run_payment_ledger
from examples.unsafe_approval import UnsafeApprovalAdapter


def test_unsafe_approval_example_produces_ab002() -> None:
    suite = SuiteRunner(RunnerOptions(scenarios=("approval_hold",))).verify_sync(
        UnsafeApprovalAdapter()
    )

    assert suite.exit_code == 1
    assert suite.results[0].finding is not None
    assert suite.results[0].finding.code == "AB002"


def test_payment_ledger_public_runner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_payment_ledger() == 0
    output = capsys.readouterr().out
    assert "AB002: Effect committed before approval" in output
    assert "Summary: 11 passed, 0 failed, 0 errors, 0 skipped" in output
    assert "customer=8500 cents, merchant=1500 cents, transactions=1" in output
    assert "customer=7500 cents, merchant=2500 cents, transactions=1" in output

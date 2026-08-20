"""Small pytest integration shipped through the `pytest11` entry point."""

from __future__ import annotations

import pytest

from agentbarrier.models import ApprovalBarrierProfile
from agentbarrier.runner import RunnerOptions, SuiteRunner


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register suite options for projects using the pytest fixture."""

    group = parser.getgroup("agentbarrier")
    group.addoption(
        "--agentbarrier-profile",
        choices=[profile.value for profile in ApprovalBarrierProfile],
        default=ApprovalBarrierProfile.RUN_WIDE.value,
        help="approval scope used by the agentbarrier fixture (default: run-wide)",
    )


@pytest.fixture
def agentbarrier(pytestconfig: pytest.Config) -> SuiteRunner:
    """Return a runner configured by AgentBarrier's pytest options."""

    selected = pytestconfig.getoption("agentbarrier_profile")
    if not isinstance(selected, str):  # pragma: no cover - pytest validates the option
        raise TypeError("--agentbarrier-profile must be a string")
    return SuiteRunner(RunnerOptions(approval_profile=ApprovalBarrierProfile(selected)))

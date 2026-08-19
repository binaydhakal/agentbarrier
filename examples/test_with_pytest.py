"""Minimal use of AgentBarrier's installed pytest fixture."""

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.runner import SuiteRunner


def test_reference_controls(agentbarrier: SuiteRunner) -> None:
    suite = agentbarrier.verify_sync(ReferenceAdapter())
    suite.raise_for_failure()

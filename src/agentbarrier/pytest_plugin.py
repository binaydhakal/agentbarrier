"""Small pytest integration shipped through the `pytest11` entry point."""

from __future__ import annotations

import pytest

from agentbarrier.runner import SuiteRunner


@pytest.fixture
def agentbarrier() -> SuiteRunner:
    """Return a fresh runner for an application adapter test."""

    return SuiteRunner()

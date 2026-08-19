"""Run the complete suite against the safe reference adapter."""

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.reporters import render_console
from agentbarrier.runner import SuiteRunner

suite = SuiteRunner().verify_sync(ReferenceAdapter())
print(render_console(suite))
suite.raise_for_failure()

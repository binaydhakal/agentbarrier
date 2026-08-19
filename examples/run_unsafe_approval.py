"""Show AgentBarrier catching an effect that starts before approval."""

from __future__ import annotations

import sys

from unsafe_approval import UnsafeApprovalAdapter

from agentbarrier.reporters import render_console
from agentbarrier.runner import RunnerOptions, SuiteRunner

suite = SuiteRunner(RunnerOptions(scenarios=("approval_hold",))).verify_sync(
    UnsafeApprovalAdapter()
)
print(render_console(suite, color=sys.stdout.isatty()))
raise SystemExit(suite.exit_code)

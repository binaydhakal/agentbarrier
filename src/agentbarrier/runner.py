"""Suite orchestration and evidence capture."""

from __future__ import annotations

import asyncio
import math
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from agentbarrier.adapter import AgentAdapter
from agentbarrier.journal import EffectJournal
from agentbarrier.models import (
    AuditReceipt,
    EffectEvent,
    Finding,
    ScenarioResult,
    ScenarioStatus,
    SuiteResult,
)
from agentbarrier.scenarios import Scenario, ScenarioOptions, ScenarioViolation, select_scenarios


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    """Configuration for a deterministic verification suite."""

    settle_seconds: float = 0.05
    operation_timeout_seconds: float = 5.0
    tool_timeout_seconds: float = 0.05
    strict_skips: bool = False
    scenarios: tuple[str, ...] | None = None
    fail_fast: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "settle_seconds",
            "operation_timeout_seconds",
            "tool_timeout_seconds",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be finite and positive")


class SuiteRunner:
    """Run the standard guarantee suite against one adapter."""

    def __init__(self, options: RunnerOptions | None = None) -> None:
        self.options = options or RunnerOptions()

    async def verify(self, adapter: AgentAdapter) -> SuiteResult:
        """Verify all selected scenarios and return their external evidence."""

        scenarios = select_scenarios(self.options.scenarios)
        results: list[ScenarioResult] = []
        for scenario in scenarios:
            result = await self._run_scenario(adapter, scenario)
            results.append(result)
            if self.options.fail_fast and result.status in {
                ScenarioStatus.FAILED,
                ScenarioStatus.ERROR,
            }:
                break
        return SuiteResult(
            adapter=adapter.name,
            results=tuple(results),
            strict_skips=self.options.strict_skips,
        )

    def verify_sync(self, adapter: AgentAdapter) -> SuiteResult:
        """Synchronous entry point for CLI and ordinary pytest tests."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.verify(adapter))
        raise RuntimeError("verify_sync() cannot run inside an event loop; await verify() instead")

    async def _run_scenario(
        self,
        adapter: AgentAdapter,
        scenario: Scenario,
    ) -> ScenarioResult:
        started = time.perf_counter()
        if scenario.capability not in adapter.capabilities:
            return ScenarioResult(
                scenario_id=scenario.scenario_id,
                name=scenario.name,
                adapter=adapter.name,
                status=ScenarioStatus.SKIPPED,
                duration_seconds=time.perf_counter() - started,
                detail=f"adapter does not declare {scenario.capability.value!r}",
            )

        run_id = f"{scenario.scenario_id}-{uuid.uuid4().hex}"
        status: ScenarioStatus
        finding: Finding | None
        detail: str | None
        events: tuple[EffectEvent, ...]
        receipts: tuple[AuditReceipt, ...]
        scenario_receipts: tuple[AuditReceipt, ...] = ()
        with tempfile.TemporaryDirectory(prefix="agentbarrier-") as directory:
            journal = EffectJournal(Path(directory) / "effects.sqlite3")
            try:
                returned_receipts = await scenario.exercise(
                    adapter=adapter,
                    journal=journal,
                    run_id=run_id,
                    options=ScenarioOptions(
                        settle_seconds=self.options.settle_seconds,
                        operation_timeout_seconds=self.options.operation_timeout_seconds,
                        tool_timeout_seconds=self.options.tool_timeout_seconds,
                    ),
                )
                if returned_receipts is not None:
                    scenario_receipts = returned_receipts
            except ScenarioViolation as exc:
                status = ScenarioStatus.FAILED
                finding = exc.finding
                detail = str(exc)
                scenario_receipts = exc.receipts
            except Exception as exc:  # defensive: adapter errors are report evidence
                status = ScenarioStatus.ERROR
                finding = None
                detail = f"{type(exc).__name__}: {exc}"
            else:
                status = ScenarioStatus.PASSED
                finding = None
                detail = None
            finally:
                events = journal.events(run_id=run_id)
                journal_receipts = journal.receipts(run_id=run_id)
                receipts = scenario_receipts or journal_receipts
                journal.close()

        return ScenarioResult(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            adapter=adapter.name,
            status=status,
            duration_seconds=time.perf_counter() - started,
            events=events,
            receipts=receipts,
            finding=finding,
            detail=detail,
        )

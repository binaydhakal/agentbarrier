"""Optional, failure-isolated observation hooks around protected runtime attempts."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from types import TracebackType
from typing import Protocol

from agentbarrier.runtime.models import RuntimeAction


class RuntimeActionObservation(Protocol):
    """One in-flight protected action attempt observed outside the safety boundary."""

    def bind(self, action: RuntimeAction) -> None: ...

    def finish(self, outcome: str, *, action: RuntimeAction) -> None: ...

    def fail(self, error: BaseException, *, action: RuntimeAction | None) -> None: ...


class RuntimeObserver(Protocol):
    """Factory for one failure-isolated runtime attempt observation."""

    def observe(
        self,
        *,
        organization_id: str,
        namespace: str,
        tool_name: str,
    ) -> AbstractContextManager[RuntimeActionObservation]: ...


class _NoopActionObservation:
    def bind(self, action: RuntimeAction) -> None:
        del action

    def finish(self, outcome: str, *, action: RuntimeAction) -> None:
        del outcome, action

    def fail(self, error: BaseException, *, action: RuntimeAction | None) -> None:
        del error, action


class NoopRuntimeObserver:
    """Default observer that allocates no telemetry and has no optional dependencies."""

    @contextmanager
    def observe(
        self,
        *,
        organization_id: str,
        namespace: str,
        tool_name: str,
    ) -> Iterator[RuntimeActionObservation]:
        del organization_id, namespace, tool_name
        yield _NoopActionObservation()


class _GuardedActionObservation:
    """Prevent an observer failure from changing a protected action outcome."""

    def __init__(self, observation: RuntimeActionObservation) -> None:
        self._observation = observation

    def bind(self, action: RuntimeAction) -> None:
        try:
            self._observation.bind(action)
        except BaseException:
            return

    def finish(self, outcome: str, *, action: RuntimeAction) -> None:
        try:
            self._observation.finish(outcome, action=action)
        except BaseException:
            return

    def fail(self, error: BaseException, *, action: RuntimeAction | None) -> None:
        try:
            self._observation.fail(error, action=action)
        except BaseException:
            return


@contextmanager
def safe_observation(
    observer: RuntimeObserver,
    *,
    organization_id: str,
    namespace: str,
    tool_name: str,
) -> Iterator[RuntimeActionObservation]:
    """Enter, call, and exit third-party telemetry without weakening runtime enforcement."""

    manager: AbstractContextManager[RuntimeActionObservation] | None = None
    observation: RuntimeActionObservation = _NoopActionObservation()
    try:
        manager = observer.observe(
            organization_id=organization_id,
            namespace=namespace,
            tool_name=tool_name,
        )
        observation = manager.__enter__()
    except BaseException:
        manager = None
    error_info: tuple[
        type[BaseException] | None,
        BaseException | None,
        TracebackType | None,
    ] = (None, None, None)
    try:
        yield _GuardedActionObservation(observation)
    except BaseException:
        error_info = sys.exc_info()
        raise
    finally:
        if manager is not None:
            with suppress(BaseException):
                manager.__exit__(*error_info)

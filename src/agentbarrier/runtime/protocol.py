"""Backend-neutral durable runtime store contract."""

from __future__ import annotations

from typing import Protocol

from agentbarrier.models import Decision, JsonValue
from agentbarrier.runtime.models import (
    DecisionAuthorization,
    ExecutionClaim,
    PolicyDecision,
    RuntimeAction,
    RuntimeControlReceipt,
    RuntimeLimit,
    RuntimeLimitUsage,
    RuntimePause,
    RuntimeReceipt,
    RuntimeReconciliation,
    RuntimeRequest,
    RuntimeStatus,
)


class RuntimeStore(Protocol):
    """Operations every production runtime storage backend must preserve atomically."""

    def submit(self, request: RuntimeRequest, decision: PolicyDecision) -> RuntimeAction: ...

    def decide(
        self,
        action_id: str,
        decision: Decision,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> RuntimeAction: ...

    def decide_authorized(
        self,
        action_id: str,
        decision: Decision,
        *,
        authorization: DecisionAuthorization,
        reason: str | None = None,
    ) -> RuntimeAction: ...

    def set_pause(
        self,
        *,
        paused_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
    ) -> RuntimePause: ...

    def clear_pause(
        self,
        *,
        resumed_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
    ) -> bool: ...

    def list_pauses(self) -> tuple[RuntimePause, ...]: ...

    def configure_limit(
        self,
        limit_id: str,
        *,
        window_seconds: float,
        updated_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
        max_actions: int | None = None,
        value_argument: str | None = None,
        max_value: int | None = None,
    ) -> RuntimeLimit: ...

    def disable_limit(self, limit_id: str, *, updated_by: str, reason: str) -> RuntimeLimit: ...

    def list_limits(self) -> tuple[RuntimeLimit, ...]: ...

    def limit_usage(self, limit_id: str | None = None) -> tuple[RuntimeLimitUsage, ...]: ...

    def control_receipts(self) -> tuple[RuntimeControlReceipt, ...]: ...

    def verify_control_receipt_chain(self) -> bool: ...

    def claim(self, action_id: str, *, request_digest: str) -> ExecutionClaim: ...

    def complete(
        self,
        action_id: str,
        *,
        request_digest: str,
        result: JsonValue,
    ) -> RuntimeAction: ...

    def mark_unknown(
        self,
        action_id: str,
        *,
        request_digest: str,
        error: str,
    ) -> RuntimeAction: ...

    def reconcile(
        self,
        action_id: str,
        outcome: RuntimeReconciliation,
        *,
        resolved_by: str,
        reason: str,
        result: JsonValue = None,
    ) -> RuntimeAction: ...

    def get_action(self, action_id: str) -> RuntimeAction: ...

    def list_actions(
        self,
        *,
        status: RuntimeStatus | None = None,
    ) -> tuple[RuntimeAction, ...]: ...

    def receipts(self, *, action_id: str | None = None) -> tuple[RuntimeReceipt, ...]: ...

    def verify_receipt_chain(self) -> bool: ...

    @property
    def schema_version(self) -> str: ...

    def close(self) -> None: ...

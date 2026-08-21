"""Stable JSON-compatible representations of runtime state."""

from __future__ import annotations

from agentbarrier.runtime.models import (
    RuntimeAction,
    RuntimeControlReceipt,
    RuntimeLimit,
    RuntimeLimitUsage,
    RuntimePause,
    RuntimeReceipt,
)


def action_payload(action: RuntimeAction) -> dict[str, object]:
    """Serialize one runtime action without exposing implementation objects."""

    return {
        "action_id": action.action_id,
        "namespace": action.namespace,
        "tool_name": action.tool_name,
        "arguments": dict(action.arguments),
        "idempotency_key": action.idempotency_key,
        "request_digest": action.request_digest,
        "policy_version": action.policy_version,
        "policy_rule": action.policy_rule,
        "policy_effect": action.policy_effect.value,
        "status": action.status.value,
        "created_at_ns": action.created_at_ns,
        "updated_at_ns": action.updated_at_ns,
        "expires_at_ns": action.expires_at_ns,
        "approval_ttl_ns": action.approval_ttl_ns,
        "execution_lease_expires_at_ns": action.execution_lease_expires_at_ns,
        "result": action.result if action.result_available else None,
        "result_available": action.result_available,
        "error": action.error,
        "decided_by": action.decided_by,
        "decision_reason": action.decision_reason,
    }


def receipt_payload(receipt: RuntimeReceipt) -> dict[str, object]:
    """Serialize one integrity-linked runtime receipt."""

    return {
        "sequence": receipt.sequence,
        "action_id": receipt.action_id,
        "event": receipt.event.value,
        "timestamp_ns": receipt.timestamp_ns,
        "request_digest": receipt.request_digest,
        "actor": receipt.actor,
        "detail": receipt.detail,
        "previous_hash": receipt.previous_hash,
        "receipt_hash": receipt.receipt_hash,
    }


def pause_payload(pause: RuntimePause) -> dict[str, object]:
    """Serialize one active emergency pause."""

    return {
        "namespace": pause.namespace,
        "tool_name": pause.tool_name,
        "paused_at_ns": pause.paused_at_ns,
        "paused_by": pause.paused_by,
        "reason": pause.reason,
    }


def limit_payload(limit: RuntimeLimit) -> dict[str, object]:
    """Serialize one durable execution-limit definition."""

    return {
        "limit_id": limit.limit_id,
        "namespace": limit.namespace,
        "tool_name": limit.tool_name,
        "window_ns": limit.window_ns,
        "max_actions": limit.max_actions,
        "value_argument": limit.value_argument,
        "max_value": limit.max_value,
        "enabled": limit.enabled,
        "updated_at_ns": limit.updated_at_ns,
        "updated_by": limit.updated_by,
        "reason": limit.reason,
    }


def limit_usage_payload(usage: RuntimeLimitUsage) -> dict[str, object]:
    """Serialize current usage for one execution limit."""

    return {
        "limit_id": usage.limit_id,
        "window_started_at_ns": usage.window_started_at_ns,
        "actions_used": usage.actions_used,
        "value_used": usage.value_used,
    }


def control_receipt_payload(receipt: RuntimeControlReceipt) -> dict[str, object]:
    """Serialize one integrity-linked operator-control receipt."""

    return {
        "sequence": receipt.sequence,
        "event": receipt.event.value,
        "timestamp_ns": receipt.timestamp_ns,
        "actor": receipt.actor,
        "scope": receipt.scope,
        "detail": receipt.detail,
        "previous_hash": receipt.previous_hash,
        "receipt_hash": receipt.receipt_hash,
    }

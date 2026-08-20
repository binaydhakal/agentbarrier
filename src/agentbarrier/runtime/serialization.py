"""Stable JSON-compatible representations of runtime state."""

from __future__ import annotations

from agentbarrier.runtime.models import RuntimeAction, RuntimeReceipt


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

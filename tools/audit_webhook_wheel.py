"""Audit an installed wheel's signed, redacted, durable webhook lifecycle."""

from __future__ import annotations

import hmac
import json
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import anyio

from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeEvent,
    RuntimeRequest,
    SQLiteRuntimeStore,
)
from agentbarrier.service import (
    WebhookConfig,
    WebhookDeliveryStore,
    WebhookEndpoint,
    WebhookWorker,
)

SECRET = "webhook-wheel-secret-0123456789-abcdef"


class Clock:
    def __init__(self) -> None:
        self.now_ns = 1_000_000_000

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: int) -> None:
        self.now_ns += seconds * 1_000_000_000


class Sender:
    def __init__(self) -> None:
        self.statuses = [503, 204]
        self.requests: list[tuple[bytes, dict[str, str]]] = []

    async def __call__(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> int:
        if url != "https://hooks.example.com/agentbarrier" or timeout_seconds != 5:
            raise AssertionError("installed webhook changed its configured destination")
        self.requests.append((body, dict(headers)))
        return self.statuses.pop(0)


async def run_audit(directory: Path) -> dict[str, object]:
    clock = Clock()
    sender = Sender()
    endpoint = WebhookEndpoint(
        endpoint_id="wheel-audit",
        url="https://hooks.example.com/agentbarrier",
        secret=SECRET,
        secret_env="AGENTBARRIER_WEBHOOK_SECRET",
        events=frozenset({RuntimeEvent.APPROVED}),
        redact_argument_paths=("request_id", "customer.ssn"),
        timeout_seconds=5,
        initial_backoff_seconds=2,
    )
    request = RuntimeRequest(
        action_id="webhook-wheel-action",
        namespace="support",
        tool_name="payments.refund",
        arguments={
            "request_id": "private-business-id",
            "api_key": "private-api-key",
            "customer": {"name": "Customer", "ssn": "private-ssn"},
        },
        idempotency_key="private-business-id",
        policy_version="webhook-wheel-v1",
        created_at_ns=1,
    )
    with (
        SQLiteRuntimeStore(directory / "runtime.db", clock_ns=clock) as runtime_store,
        WebhookDeliveryStore(directory / "webhooks.db", clock_ns=clock) as delivery_store,
    ):
        runtime_store.submit(
            request,
            PolicyDecision(
                PolicyEffect.REQUIRE_APPROVAL,
                "review refunds",
                "webhook-wheel-v1",
            ),
        )
        runtime_store.decide(request.action_id, Decision.APPROVE, decided_by="reviewer")
        worker = WebhookWorker(
            runtime_store=runtime_store,
            delivery_store=delivery_store,
            config=WebhookConfig((endpoint,)),
            sender=sender,
            clock_ns=clock,
            worker_id="webhook-wheel-worker",
        )
        first = await worker.run_once()
        clock.advance(2)
        second = await worker.run_once()
        snapshot = delivery_store.snapshots()[0]

    if first != {"enqueued": 1, "delivered": 0, "retried": 1, "dead": 0}:
        raise AssertionError(f"installed webhook did not durably retry: {first}")
    if second != {"enqueued": 0, "delivered": 1, "retried": 0, "dead": 0}:
        raise AssertionError(f"installed webhook did not finish its retry: {second}")
    if snapshot.status != "delivered" or snapshot.attempts != 2:
        raise AssertionError("installed webhook delivery state was not durable")
    if len(sender.requests) != 2 or sender.requests[0][0] != sender.requests[1][0]:
        raise AssertionError("installed webhook did not retry exact stable bytes")

    body, headers = sender.requests[0]
    encoded = body.decode("utf-8")
    for secret in ("private-business-id", "private-api-key", "private-ssn"):
        if secret in encoded:
            raise AssertionError(f"installed webhook exposed {secret!r}")
    payload = json.loads(body)
    arguments = payload["data"]["action"]["arguments"]
    if arguments["api_key"] != "[REDACTED]":
        raise AssertionError("installed webhook did not automatically redact a credential")
    if arguments["customer"]["ssn"] != "[REDACTED]":
        raise AssertionError("installed webhook did not apply configured path redaction")
    timestamp = headers["X-AgentBarrier-Timestamp"]
    expected_signature = hmac.new(
        SECRET.encode("ascii"),
        timestamp.encode("ascii") + b"." + body,
        sha256,
    ).hexdigest()
    if headers["X-AgentBarrier-Signature"] != f"v1={expected_signature}":
        raise AssertionError("installed webhook signature did not bind the exact body")
    return {
        "attempts": snapshot.attempts,
        "event_id": headers["X-AgentBarrier-Event-Id"],
        "redacted": True,
        "status": "passed",
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agentbarrier-webhook-wheel-") as temporary:
        result = anyio.run(run_audit, Path(temporary))
    print(json.dumps(result, indent=2, sort_keys=True))

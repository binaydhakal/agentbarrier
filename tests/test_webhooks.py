from __future__ import annotations

import asyncio
import hmac
import json
from hashlib import sha256
from pathlib import Path

import pytest

from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeEvent,
    RuntimeRequest,
    SQLiteRuntimeStore,
)
from agentbarrier.service import runner as service_runner
from agentbarrier.service import webhooks as webhook_module
from agentbarrier.service.webhooks import (
    WebhookConfig,
    WebhookDeliveryStore,
    WebhookEndpoint,
    WebhookWorker,
    build_webhook_body,
    signature_headers,
)

WEBHOOK_SECRET = "webhook-secret-0123456789-abcdef"


class FakeClock:
    def __init__(self, now_ns: int = 1_000_000_000) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


class RecordingSender:
    def __init__(self, outcomes: list[int | BaseException]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str] | object,
        timeout_seconds: float,
    ) -> int:
        assert isinstance(headers, dict)
        self.requests.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_endpoint(**changes: object) -> WebhookEndpoint:
    values: dict[str, object] = {
        "endpoint_id": "operations",
        "url": "https://hooks.example.com/agentbarrier",
        "secret": WEBHOOK_SECRET,
        "secret_env": "AGENTBARRIER_WEBHOOK_SECRET",
        "events": frozenset(RuntimeEvent),
        "redact_argument_paths": ("customer.ssn",),
        "timeout_seconds": 5,
        "max_attempts": 3,
        "initial_backoff_seconds": 2,
        "max_backoff_seconds": 8,
    }
    values.update(changes)
    return WebhookEndpoint(**values)  # type: ignore[arg-type]


def submit_action(store: SQLiteRuntimeStore, *, action_id: str = "action-1") -> RuntimeRequest:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="support",
        tool_name="payments.refund",
        arguments={
            "request_id": action_id,
            "amount": 100,
            "api_key": "must-not-leak",
            "accessToken": "camel-case-secret",
            "customer": {"ssn": "111-22-3333", "name": "A User"},
        },
        idempotency_key=action_id,
        policy_version="webhook-policy-v1",
        created_at_ns=1,
    )
    store.submit(
        request,
        PolicyDecision(
            PolicyEffect.REQUIRE_APPROVAL,
            "review refunds",
            "webhook-policy-v1",
        ),
    )
    return request


def test_webhook_worker_signs_redacted_stable_body_and_retries(tmp_path: Path) -> None:
    async def run() -> None:
        clock = FakeClock()
        sender = RecordingSender([500, 204])
        endpoint = make_endpoint(events=frozenset({RuntimeEvent.APPROVED}))
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as runtime_store,
            WebhookDeliveryStore(tmp_path / "webhooks.db", clock_ns=clock) as delivery_store,
        ):
            request = submit_action(runtime_store)
            runtime_store.decide(request.action_id, Decision.APPROVE, decided_by="reviewer")
            worker = WebhookWorker(
                runtime_store=runtime_store,
                delivery_store=delivery_store,
                config=WebhookConfig((endpoint,)),
                sender=sender,
                clock_ns=clock,
                worker_id="worker-1",
            )

            first = await worker.run_once()
            first_snapshot = delivery_store.snapshots()[0]
            clock.advance(2)
            second = await worker.run_once()
            final_snapshot = delivery_store.snapshots()[0]

        assert first == {"enqueued": 1, "delivered": 0, "retried": 1, "dead": 0}
        assert first_snapshot.status == "pending"
        assert first_snapshot.attempts == 1
        assert first_snapshot.last_status_code == 500
        assert first_snapshot.last_error == "HTTPError"
        assert second == {"enqueued": 0, "delivered": 1, "retried": 0, "dead": 0}
        assert final_snapshot.status == "delivered"
        assert final_snapshot.attempts == 2
        assert final_snapshot.last_status_code == 204

        assert len(sender.requests) == 2
        first_request = sender.requests[0]
        second_request = sender.requests[1]
        assert first_request["body"] == second_request["body"]
        body = first_request["body"]
        assert isinstance(body, bytes)
        assert b"must-not-leak" not in body
        assert b"camel-case-secret" not in body
        assert b"111-22-3333" not in body
        payload = json.loads(body)
        arguments = payload["data"]["action"]["arguments"]
        assert arguments["api_key"] == "[REDACTED]"
        assert arguments["accessToken"] == "[REDACTED]"
        assert arguments["customer"] == {"ssn": "[REDACTED]", "name": "A User"}
        assert "idempotency_key" not in payload["data"]["action"]
        assert payload["data"]["action"]["event_status"] == "approved"

        for sent in sender.requests:
            sent_body = sent["body"]
            sent_headers = sent["headers"]
            assert isinstance(sent_body, bytes)
            assert isinstance(sent_headers, dict)
            timestamp = sent_headers["X-AgentBarrier-Timestamp"]
            expected = hmac.new(
                WEBHOOK_SECRET.encode("ascii"),
                timestamp.encode("ascii") + b"." + sent_body,
                sha256,
            ).hexdigest()
            assert sent_headers["X-AgentBarrier-Signature"] == f"v1={expected}"
            assert sent_headers["X-AgentBarrier-Event-Id"] == "runtime-receipt-2"
            assert sent_headers["Content-Type"] == "application/cloudevents+json"

    asyncio.run(run())


def test_webhook_worker_bounds_exception_retries_and_marks_dead(tmp_path: Path) -> None:
    async def run() -> None:
        clock = FakeClock()
        sender = RecordingSender([ConnectionError("secret detail"), TimeoutError("hidden")])
        endpoint = make_endpoint(
            events=frozenset({RuntimeEvent.APPROVAL_REQUESTED}),
            max_attempts=2,
            initial_backoff_seconds=1,
        )
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as runtime_store,
            WebhookDeliveryStore(tmp_path / "webhooks.db", clock_ns=clock) as delivery_store,
        ):
            submit_action(runtime_store)
            worker = WebhookWorker(
                runtime_store=runtime_store,
                delivery_store=delivery_store,
                config=WebhookConfig((endpoint,)),
                sender=sender,
                clock_ns=clock,
                worker_id="worker-1",
            )
            first = await worker.run_once()
            clock.advance(1)
            second = await worker.run_once()
            snapshot = delivery_store.snapshots()[0]

        assert first["retried"] == 1
        assert second["dead"] == 1
        assert snapshot.status == "dead"
        assert snapshot.attempts == 2
        assert snapshot.last_status_code is None
        assert snapshot.last_error == "TimeoutError"
        assert "hidden" not in snapshot.last_error

    asyncio.run(run())


def test_webhook_latest_checkpoint_skips_history_then_delivers_new_event(tmp_path: Path) -> None:
    async def run() -> None:
        clock = FakeClock()
        sender = RecordingSender([202])
        endpoint = make_endpoint(
            events=frozenset({RuntimeEvent.APPROVAL_REQUESTED, RuntimeEvent.APPROVED}),
            start_from="latest",
        )
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as runtime_store,
            WebhookDeliveryStore(tmp_path / "webhooks.db", clock_ns=clock) as delivery_store,
        ):
            request = submit_action(runtime_store)
            worker = WebhookWorker(
                runtime_store=runtime_store,
                delivery_store=delivery_store,
                config=WebhookConfig((endpoint,)),
                sender=sender,
                clock_ns=clock,
            )
            assert await worker.run_once() == {
                "enqueued": 0,
                "delivered": 0,
                "retried": 0,
                "dead": 0,
            }
            runtime_store.decide(request.action_id, Decision.APPROVE, decided_by="reviewer")
            result = await worker.run_once()
            snapshots = delivery_store.snapshots()

        assert result["delivered"] == 1
        assert [snapshot.event_type for snapshot in snapshots] == ["approved"]

    asyncio.run(run())


def test_webhook_delivery_claim_lease_recovers_and_rejects_old_owner(tmp_path: Path) -> None:
    clock = FakeClock()
    endpoint = make_endpoint(events=frozenset({RuntimeEvent.APPROVAL_REQUESTED}))
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as runtime_store,
        WebhookDeliveryStore(
            tmp_path / "webhooks.db",
            clock_ns=clock,
            claim_lease_seconds=1,
        ) as delivery_store,
    ):
        submit_action(runtime_store)
        receipt = runtime_store.receipts()[0]
        action = runtime_store.get_action(receipt.action_id)
        delivery_store.register_endpoint(endpoint, current_sequence=receipt.sequence)
        delivery_store.enqueue(
            endpoint,
            [(receipt, build_webhook_body(receipt, action, endpoint))],
            observed_sequence=receipt.sequence,
        )
        first = delivery_store.claim_due(worker_id="worker-1")
        assert first is not None
        assert delivery_store.claim_due(worker_id="worker-2") is None
        clock.advance(1)
        recovered = delivery_store.claim_due(worker_id="worker-2")
        assert recovered is not None
        assert recovered.delivery_id == first.delivery_id
        assert recovered.attempts == 2
        with pytest.raises(ValueError, match="owned"):
            delivery_store.mark_delivered(
                first.delivery_id,
                worker_id="worker-1",
                status_code=200,
            )


def test_dead_webhook_can_be_requeued_only_by_exact_endpoint_and_event(tmp_path: Path) -> None:
    clock = FakeClock()
    endpoint = make_endpoint(
        events=frozenset({RuntimeEvent.APPROVAL_REQUESTED}),
        max_attempts=1,
    )
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock) as runtime_store,
        WebhookDeliveryStore(tmp_path / "webhooks.db", clock_ns=clock) as delivery_store,
    ):
        submit_action(runtime_store)
        receipt = runtime_store.receipts()[0]
        action = runtime_store.get_action(receipt.action_id)
        delivery_store.register_endpoint(endpoint, current_sequence=receipt.sequence)
        delivery_store.enqueue(
            endpoint,
            [(receipt, build_webhook_body(receipt, action, endpoint))],
            observed_sequence=receipt.sequence,
        )
        claimed = delivery_store.claim_due(worker_id="worker-1")
        assert claimed is not None
        assert (
            delivery_store.mark_failed(
                claimed,
                worker_id="worker-1",
                endpoint=endpoint,
                status_code=503,
                error="HTTPError",
            )
            == "dead"
        )

        retried = delivery_store.retry_dead(
            endpoint_id=endpoint.endpoint_id,
            event_id=claimed.event_id,
        )
        assert retried.status == "pending"
        assert retried.attempts == 0
        assert retried.last_status_code == 503
        assert retried.last_error == "HTTPError"
        with pytest.raises(ValueError, match="only a dead"):
            delivery_store.retry_dead(
                endpoint_id=endpoint.endpoint_id,
                event_id=claimed.event_id,
            )
        with pytest.raises(KeyError, match="unknown webhook delivery"):
            delivery_store.retry_dead(
                endpoint_id="another-endpoint",
                event_id=claimed.event_id,
            )


def test_webhook_endpoint_configuration_is_bound_to_durable_state(tmp_path: Path) -> None:
    endpoint = make_endpoint()
    changed = make_endpoint(url="https://other.example.com/webhook")
    with WebhookDeliveryStore(tmp_path / "webhooks.db") as store:
        assert store.register_endpoint(endpoint, current_sequence=3) == 0
        assert store.register_endpoint(endpoint, current_sequence=5) == 0
        with pytest.raises(ValueError, match="changed configuration"):
            store.register_endpoint(changed, current_sequence=5)


def test_webhook_config_loads_secrets_from_environment_and_never_digests_secret() -> None:
    config = WebhookConfig.from_mapping(
        {
            "version": "1",
            "endpoints": [
                {
                    "id": "ops",
                    "url": "https://hooks.example.com/agentbarrier",
                    "secret_env": "WEBHOOK_SECRET",
                    "events": ["approval_requested", "approved"],
                    "redact_argument_paths": ["customer.ssn"],
                }
            ],
        },
        environment={"WEBHOOK_SECRET": WEBHOOK_SECRET},
    )
    endpoint = config.endpoints[0]
    assert endpoint.secret == WEBHOOK_SECRET
    assert endpoint.events == frozenset({RuntimeEvent.APPROVAL_REQUESTED, RuntimeEvent.APPROVED})
    assert WEBHOOK_SECRET not in endpoint.config_digest


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"version": "2", "endpoints": []}, "version"),
        ({"version": "1", "endpoints": "not-a-list"}, "must be a list"),
        ({"version": "1", "endpoints": []}, "at least one endpoint"),
        ({"version": "1", "endpoints": ["not-an-object"]}, "must be an object"),
        (
            {
                "version": "1",
                "endpoints": [
                    {
                        "id": "ops",
                        "url": "https://example.com/webhook",
                        "secret_env": "MISSING_SECRET",
                    }
                ],
            },
            "is not set",
        ),
        (
            {
                "version": "1",
                "endpoints": [
                    {
                        "id": "ops",
                        "url": "https://example.com/webhook",
                        "secret_env": "WEBHOOK_SECRET",
                        "max_attempts": True,
                    }
                ],
            },
            "must be an integer",
        ),
    ],
)
def test_webhook_config_rejects_malformed_values(
    config: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        WebhookConfig.from_mapping(  # type: ignore[arg-type]
            config,
            environment={"WEBHOOK_SECRET": WEBHOOK_SECRET},
        )


def test_webhook_config_rejects_unknown_fields_duplicate_ids_and_events() -> None:
    endpoint: dict[str, object] = {
        "id": "ops",
        "url": "https://example.com/webhook",
        "secret_env": "WEBHOOK_SECRET",
    }
    with pytest.raises(ValueError, match="unknown webhook config keys"):
        WebhookConfig.from_mapping(
            {"version": "1", "endpoints": [endpoint], "unexpected": True},
            environment={"WEBHOOK_SECRET": WEBHOOK_SECRET},
        )
    with pytest.raises(ValueError, match="endpoint ids must be unique"):
        WebhookConfig.from_mapping(
            {"version": "1", "endpoints": [endpoint, endpoint]},
            environment={"WEBHOOK_SECRET": WEBHOOK_SECRET},
        )
    with pytest.raises(ValueError, match="events must not contain duplicates"):
        WebhookConfig.from_mapping(
            {
                "version": "1",
                "endpoints": [
                    {**endpoint, "events": ["approved", "approved"]},
                ],
            },
            environment={"WEBHOOK_SECRET": WEBHOOK_SECRET},
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"endpoint_id": "bad id"}, "endpoint id"),
        ({"url": "file:///tmp/hook"}, "HTTP or HTTPS"),
        ({"url": "http://example.com/hook"}, "must use HTTPS"),
        ({"url": "https://user:pass@example.com/hook"}, "user information"),
        ({"url": "https://example.com/hook?token=x"}, "query"),
        ({"url": "https://example.com:99999/hook"}, "valid port"),
        ({"secret": "short"}, "32 to 512"),
        ({"events": frozenset()}, "at least one"),
        ({"redact_argument_paths": ("bad..path",)}, "dotted segments"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": 31}, "between 0 and 30"),
        ({"max_attempts": 0}, "between 1 and 20"),
        ({"initial_backoff_seconds": 0}, "initial_backoff"),
        (
            {"initial_backoff_seconds": 10, "max_backoff_seconds": 5},
            "must not be below",
        ),
        ({"start_from": "somewhere"}, "beginning.*latest"),
    ],
)
def test_webhook_endpoint_rejects_unsafe_configuration(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        make_endpoint(**changes)


def test_signature_headers_are_byte_exact() -> None:
    endpoint = make_endpoint()
    body = b'{"value":1}'
    headers = signature_headers(
        endpoint,
        body=body,
        event_id="event-1",
        timestamp="123",
    )
    expected = hmac.new(WEBHOOK_SECRET.encode(), b"123." + body, sha256).hexdigest()
    assert headers["X-AgentBarrier-Signature"] == f"v1={expected}"


def test_webhook_config_file_and_one_shot_runner_use_separate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.db"
    state_path = tmp_path / "webhooks.db"
    config_path = tmp_path / "webhooks.json"
    with SQLiteRuntimeStore(runtime_path):
        pass
    config_path.write_text(
        json.dumps(
            {
                "version": "1",
                "endpoints": [
                    {
                        "id": "operations",
                        "url": "https://hooks.example.com/agentbarrier",
                        "secret_env": "AGENTBARRIER_WEBHOOK_SECRET",
                        "start_from": "latest",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("AGENTBARRIER_WEBHOOK_SECRET", WEBHOOK_SECRET)

    assert service_runner.run_webhook_worker(
        database_path=runtime_path,
        state_path=state_path,
        config_path=config_path,
        once=True,
    ) == {"enqueued": 0, "delivered": 0, "retried": 0, "dead": 0}
    assert state_path.is_file()
    assert service_runner.webhook_delivery_status(state_path) == ()


def test_default_http_sender_disables_redirects_and_preserves_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 202

    class FakeClient:
        def __init__(self, *, follow_redirects: bool) -> None:
            captured["follow_redirects"] = follow_redirects

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **values: object) -> FakeResponse:
            captured["url"] = url
            captured.update(values)
            return FakeResponse()

    monkeypatch.setattr(webhook_module.httpx, "AsyncClient", FakeClient)
    body = b'{"event":1}'
    headers = {"X-AgentBarrier-Signature": "v1=example"}
    status = asyncio.run(
        webhook_module._send_http(
            url="https://hooks.example.com/agentbarrier",
            body=body,
            headers=headers,
            timeout_seconds=3,
        )
    )
    assert status == 202
    assert captured == {
        "follow_redirects": False,
        "url": "https://hooks.example.com/agentbarrier",
        "content": body,
        "headers": headers,
        "timeout": 3,
    }


def test_webhook_forever_validates_interval_and_is_cancellable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopWorker(Exception):
        pass

    async def stop_after_pass(_seconds: float) -> None:
        raise StopWorker

    async def run() -> None:
        endpoint = make_endpoint(start_from="latest")
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
            WebhookDeliveryStore(tmp_path / "webhooks.db") as delivery_store,
        ):
            worker = WebhookWorker(
                runtime_store=runtime_store,
                delivery_store=delivery_store,
                config=WebhookConfig((endpoint,)),
            )
            with pytest.raises(ValueError, match="poll interval"):
                await worker.run_forever(poll_interval_seconds=0)
            monkeypatch.setattr(webhook_module.anyio, "sleep", stop_after_pass)
            with pytest.raises(StopWorker):
                await worker.run_forever(poll_interval_seconds=0.1)

    asyncio.run(run())


def test_webhook_worker_requires_claim_lease_longer_than_http_timeout(tmp_path: Path) -> None:
    endpoint = make_endpoint(timeout_seconds=5)
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        WebhookDeliveryStore(
            tmp_path / "webhooks.db",
            claim_lease_seconds=5,
        ) as delivery_store,
        pytest.raises(ValueError, match="claim lease must be longer"),
    ):
        WebhookWorker(
            runtime_store=runtime_store,
            delivery_store=delivery_store,
            config=WebhookConfig((endpoint,)),
        )

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from starlette.testclient import TestClient

from agentbarrier import cli
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import runner as service_runner
from agentbarrier.service import slack as slack_module
from agentbarrier.service.slack import (
    SlackAPIError,
    SlackConfig,
    SlackInteractionService,
    SlackNotificationStore,
    SlackReviewer,
    SlackWorker,
    build_slack_decision_update,
    build_slack_notification,
    verify_slack_signature,
)

BOT_TOKEN = "xoxb-agentbarrier-test-token-0123456789"
SIGNING_SECRET = "slack-signing-secret-0123456789abcdef"
NOW_SECONDS = 2_000_000_000


class Clock:
    def __init__(self, value_ns: int = NOW_SECONDS * 1_000_000_000) -> None:
        self.value_ns = value_ns

    def ns(self) -> int:
        return self.value_ns

    def seconds(self) -> float:
        return self.value_ns / 1_000_000_000

    def advance(self, seconds: float) -> None:
        self.value_ns += int(seconds * 1_000_000_000)


class RecordingSlackAPI:
    def __init__(
        self,
        outcomes: list[tuple[int, Mapping[str, object], float | None] | BaseException],
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        method: str,
        payload: Mapping[str, object],
        bot_token: str,
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object], float | None]:
        self.calls.append(
            {
                "method": method,
                "payload": dict(payload),
                "bot_token": bot_token,
                "timeout_seconds": timeout_seconds,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_config(**changes: object) -> SlackConfig:
    values: dict[str, object] = {
        "workspace_id": "T123ABC",
        "app_id": "A123ABC",
        "channel_id": "C123ABC",
        "bot_token": BOT_TOKEN,
        "bot_token_env": "AGENTBARRIER_SLACK_BOT_TOKEN",
        "signing_secret": SIGNING_SECRET,
        "signing_secret_env": "AGENTBARRIER_SLACK_SIGNING_SECRET",
        "reviewers": (
            SlackReviewer(
                "U123ABC",
                "risk@example.com",
                frozenset(Decision),
            ),
            SlackReviewer(
                "U456ABC",
                "approver@example.com",
                frozenset({Decision.APPROVE}),
            ),
        ),
        "timeout_seconds": 2,
        "max_attempts": 3,
        "initial_backoff_seconds": 1,
        "max_backoff_seconds": 4,
    }
    values.update(changes)
    return SlackConfig(**values)  # type: ignore[arg-type]


def submit_pending(
    store: SQLiteRuntimeStore,
    *,
    action_id: str = "slack-action",
    arguments: Mapping[str, object] | None = None,
) -> RuntimeRequest:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments=dict(arguments or {"amount_cents": 2_500, "customer": "C-123"}),  # type: ignore[arg-type]
        idempotency_key=action_id,
        policy_version="slack-policy-v1",
        created_at_ns=1,
    )
    store.submit(
        request,
        PolicyDecision(
            PolicyEffect.REQUIRE_APPROVAL,
            "review refunds",
            "slack-policy-v1",
        ),
    )
    return request


def mark_posted(
    runtime_store: SQLiteRuntimeStore,
    notification_store: SlackNotificationStore,
    *,
    action_id: str = "slack-action",
    message_ts: str = "1700000000.123456",
) -> None:
    action = runtime_store.get_action(action_id)
    assert notification_store.enqueue(action, channel_id="C123ABC")
    notification = notification_store.claim_due(worker_id="test-worker")
    assert notification is not None
    notification_store.mark_posted(
        notification,
        worker_id="test-worker",
        message_ts=message_ts,
        status_code=200,
    )


def interaction_body(
    action_id: str,
    request_digest: str,
    *,
    action_name: str = "agentbarrier_approve_v1",
    user_id: str = "U123ABC",
    team_id: str = "T123ABC",
    app_id: str = "A123ABC",
    channel_id: str = "C123ABC",
    message_ts: str = "1700000000.123456",
) -> bytes:
    payload = {
        "type": "block_actions",
        "api_app_id": app_id,
        "team": {"id": team_id},
        "channel": {"id": channel_id},
        "user": {"id": user_id},
        "message": {"ts": message_ts},
        "actions": [
            {
                "action_id": action_name,
                "value": json.dumps(
                    {"action_id": action_id, "request_digest": request_digest},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ],
    }
    return urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()


def signed_headers(body: bytes, *, timestamp: int = NOW_SECONDS) -> dict[str, str]:
    signature = (
        "v0="
        + hmac.new(
            SIGNING_SECRET.encode(),
            f"v0:{timestamp}:".encode() + body,
            sha256,
        ).hexdigest()
    )
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": signature,
    }


def test_slack_worker_posts_exact_action_and_records_message_binding(tmp_path: Path) -> None:
    async def run() -> None:
        api = RecordingSlackAPI([(200, {"ok": True, "channel": "C123ABC", "ts": "1.2"}, None)])
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
            SlackNotificationStore(tmp_path / "slack.db") as notification_store,
        ):
            request = submit_pending(runtime_store)
            worker = SlackWorker(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=make_config(),
                api_caller=api,
                worker_id="worker-1",
            )
            counts = await worker.run_once()
            snapshot = notification_store.snapshots()[0]

        assert counts == {"enqueued": 1, "posted": 1, "retried": 0, "dead": 0}
        assert snapshot.status == "posted"
        assert snapshot.message_ts == "1.2"
        assert snapshot.request_digest == request.request_digest
        call = api.calls[0]
        assert call["method"] == "chat.postMessage"
        assert call["bot_token"] == BOT_TOKEN
        payload = call["payload"]
        assert isinstance(payload, dict)
        assert payload["mrkdwn"] is False
        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False
        encoded = json.dumps(payload)
        assert request.request_digest in encoded
        blocks = payload["blocks"]
        assert isinstance(blocks, list)
        argument_block = blocks[2]
        assert argument_block["text"]["text"] == '{"amount_cents":2500,"customer":"C-123"}'
        assert BOT_TOKEN not in encoded

    asyncio.run(run())


def test_slack_worker_retries_rate_limits_then_marks_dead(tmp_path: Path) -> None:
    async def run() -> None:
        clock = Clock()
        api = RecordingSlackAPI(
            [
                (429, {"ok": False, "error": "ratelimited"}, 2),
                ConnectionError("secret network detail"),
            ]
        )
        config = make_config(max_attempts=2)
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock.ns) as runtime_store,
            SlackNotificationStore(
                tmp_path / "slack.db",
                clock_ns=clock.ns,
            ) as notification_store,
        ):
            submit_pending(runtime_store)
            worker = SlackWorker(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=config,
                api_caller=api,
                worker_id="worker-1",
            )
            first = await worker.run_once()
            first_state = notification_store.snapshots()[0]
            clock.advance(2)
            second = await worker.run_once()
            final = notification_store.snapshots()[0]

        assert first["retried"] == 1
        assert first_state.status == "pending"
        assert first_state.last_status_code == 429
        assert first_state.last_error == "ratelimited"
        assert second["dead"] == 1
        assert final.status == "dead"
        assert final.attempts == 2
        assert final.last_error == "ConnectionError"
        assert "secret" not in final.last_error

    asyncio.run(run())


def test_slack_worker_marks_missing_and_decided_actions_obsolete(tmp_path: Path) -> None:
    async def run() -> None:
        api = RecordingSlackAPI([])
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
            SlackNotificationStore(tmp_path / "slack.db") as notification_store,
        ):
            first = submit_pending(runtime_store, action_id="already-decided")
            second = submit_pending(runtime_store, action_id="will-disappear")
            notification_store.enqueue(
                runtime_store.get_action(first.action_id),
                channel_id="C123ABC",
            )
            notification_store.enqueue(
                runtime_store.get_action(second.action_id),
                channel_id="C123ABC",
            )
            runtime_store.decide(first.action_id, Decision.REJECT, decided_by="cli:operator")
            original_get = runtime_store.get_action

            def get_action(action_id: str):  # type: ignore[no-untyped-def]
                if action_id == second.action_id:
                    raise KeyError(action_id)
                return original_get(action_id)

            runtime_store.get_action = get_action  # type: ignore[method-assign]
            worker = SlackWorker(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=make_config(),
                api_caller=api,
            )
            await worker.run_once()
            snapshots = notification_store.snapshots()

        assert [item.status for item in snapshots] == ["obsolete", "obsolete"]
        assert {item.last_error for item in snapshots} == {"StatusRejected", "ActionMissing"}
        assert api.calls == []

    asyncio.run(run())


def test_notification_store_recovers_abandoned_claim_and_manual_retry(tmp_path: Path) -> None:
    clock = Clock()
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock.ns) as runtime_store,
        SlackNotificationStore(
            tmp_path / "slack.db",
            clock_ns=clock.ns,
            claim_lease_seconds=1,
        ) as notification_store,
    ):
        submit_pending(runtime_store)
        notification_store.enqueue(runtime_store.get_action("slack-action"), channel_id="C123ABC")
        abandoned = notification_store.claim_due(worker_id="old-worker")
        assert abandoned is not None
        clock.advance(1)
        recovered = notification_store.claim_due(worker_id="new-worker")
        assert recovered is not None
        with pytest.raises(ValueError, match="owned"):
            notification_store.mark_obsolete(
                abandoned,
                worker_id="old-worker",
                reason="OldWorker",
            )
        status = notification_store.mark_failed(
            recovered,
            worker_id="new-worker",
            config=make_config(max_attempts=1),
            status_code=400,
            error="invalid_auth",
            retryable=False,
        )
        assert status == "dead"
        retried = notification_store.retry_dead("slack-action")

    assert retried.status == "pending"
    assert retried.attempts == 0


def test_slack_message_disables_decisions_when_exact_action_will_not_fit(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, arguments={"memo": "x" * 3_000})
        payload = build_slack_notification(
            store.get_action("slack-action"),
            channel_id="C123ABC",
            client_message_id="client-1",
        )
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    assert all(block.get("type") != "actions" for block in blocks)
    assert "decisions are disabled" in json.dumps(payload)
    assert "x" * 3_000 not in json.dumps(payload)

    with SQLiteRuntimeStore(tmp_path / "metadata.db") as store:
        request = RuntimeRequest(
            action_id="unsafe\naction",
            namespace="billing",
            tool_name="payments.refund",
            arguments={"amount_cents": 100},
            idempotency_key="unsafe-metadata",
            policy_version="v1",
            created_at_ns=1,
        )
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refunds", "v1"),
        )
        unsafe = build_slack_notification(
            action,
            channel_id="C123ABC",
            client_message_id="client-2",
        )
        store.decide(action.action_id, Decision.APPROVE, decided_by="reviewer\nspoof")
        update = build_slack_decision_update(store.get_action(action.action_id))
    unsafe_blocks = unsafe["blocks"]
    assert isinstance(unsafe_blocks, list)
    assert all(block.get("type") != "actions" for block in unsafe_blocks)
    assert "unsafe�action" in json.dumps(unsafe, ensure_ascii=False)
    assert "reviewer�spoof" in json.dumps(update, ensure_ascii=False)


def test_signed_slack_approval_records_exact_member_identity_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    api = RecordingSlackAPI([(200, {"ok": True}, None)])
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            api_caller=api,
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        body = interaction_body(request.action_id, request.request_digest)
        with TestClient(app) as client:
            response = client.post(
                "/slack/interactions", content=body, headers=signed_headers(body)
            )
            replay = client.post("/slack/interactions", content=body, headers=signed_headers(body))
        action = runtime_store.get_action(request.action_id)
        notification = notification_store.snapshots()[0]
        receipts = runtime_store.receipts(action_id=request.action_id)

    assert response.status_code == 200
    assert replay.status_code == 200
    assert action.status is RuntimeStatus.APPROVED
    assert action.decided_by == "slack:T123ABC:U123ABC:risk@example.com"
    assert notification.status == "decided"
    assert notification.decided_by == action.decided_by
    assert notification.decision == "approve"
    assert [receipt.event.value for receipt in receipts] == ["approval_requested", "approved"]
    assert [call["method"] for call in api.calls] == ["chat.update"]


def test_repeat_same_decision_preserves_original_reviewer_identity(tmp_path: Path) -> None:
    api = RecordingSlackAPI([(200, {"ok": True}, None)])
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        runtime_store.decide(request.action_id, Decision.APPROVE, decided_by="dashboard:original")
        body = interaction_body(request.action_id, request.request_digest)
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            api_caller=api,
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        with TestClient(app) as client:
            assert (
                client.post(
                    "/slack/interactions", content=body, headers=signed_headers(body)
                ).status_code
                == 200
            )
        snapshot = notification_store.snapshots()[0]

    assert snapshot.decided_by == "dashboard:original"
    assert snapshot.decision == "approve"


def test_unauthorized_or_under_scoped_slack_member_cannot_decide(tmp_path: Path) -> None:
    api = RecordingSlackAPI([(200, {"ok": True}, None), (200, {"ok": True}, None)])
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            api_caller=api,
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        outsider = interaction_body(request.action_id, request.request_digest, user_id="U999ABC")
        reject_only_approver = interaction_body(
            request.action_id,
            request.request_digest,
            user_id="U456ABC",
            action_name="agentbarrier_reject_v1",
        )
        with TestClient(app) as client:
            assert (
                client.post(
                    "/slack/interactions", content=outsider, headers=signed_headers(outsider)
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/slack/interactions",
                    content=reject_only_approver,
                    headers=signed_headers(reject_only_approver),
                ).status_code
                == 200
            )
        action = runtime_store.get_action(request.action_id)

    assert action.status is RuntimeStatus.PENDING
    assert [call["method"] for call in api.calls] == ["chat.postEphemeral", "chat.postEphemeral"]


@pytest.mark.parametrize(
    ("body_change", "header_change", "status", "code"),
    [
        ({"team_id": "T999ABC"}, {}, 403, "wrong_workspace"),
        ({"app_id": "A999ABC"}, {}, 403, "wrong_app"),
        ({"channel_id": "C999ABC"}, {}, 403, "wrong_channel"),
        ({"message_ts": "1700000000.999999"}, {}, 409, "notification_binding_failed"),
        ({}, {"X-Slack-Signature": "v0=" + "0" * 64}, 401, "invalid_signature"),
        ({}, {"X-Slack-Request-Timestamp": str(NOW_SECONDS - 301)}, 401, "stale_request"),
    ],
)
def test_slack_interactions_fail_closed_for_forgery_and_wrong_binding(
    tmp_path: Path,
    body_change: dict[str, str],
    header_change: dict[str, str],
    status: int,
    code: str,
) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        body = interaction_body(request.action_id, request.request_digest, **body_change)
        headers = signed_headers(body)
        headers.update(header_change)
        if "X-Slack-Request-Timestamp" in header_change:
            timestamp = int(header_change["X-Slack-Request-Timestamp"])
            headers = signed_headers(body, timestamp=timestamp)
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        with TestClient(app) as client:
            response = client.post("/slack/interactions", content=body, headers=headers)
        action = runtime_store.get_action(request.action_id)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert action.status is RuntimeStatus.PENDING


def test_slack_interaction_rejects_bad_media_type_and_large_body(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        body = b"{}"
        headers = signed_headers(body)
        headers["Content-Type"] = "application/json"
        large = b"x" * (64 * 1024 + 1)
        with TestClient(app) as client:
            media = client.post("/slack/interactions", content=body, headers=headers)
            too_large = client.post(
                "/slack/interactions",
                content=large,
                headers=signed_headers(large),
            )

    assert media.status_code == 415
    assert too_large.status_code == 413


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workspace_id": "bad"}, "workspace_id"),
        ({"app_id": "bad"}, "app_id"),
        ({"channel_id": "bad"}, "channel_id"),
        ({"bot_token": "not-a-token"}, "bot token"),
        ({"signing_secret": "short"}, "signing secret"),
        ({"reviewers": ()}, "at least one reviewer"),
        ({"timeout_seconds": 3}, "timeout_seconds"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"initial_backoff_seconds": 0}, "initial_backoff_seconds"),
        ({"initial_backoff_seconds": 5, "max_backoff_seconds": 4}, "must not be below"),
    ],
)
def test_slack_config_rejects_unsafe_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_config(**changes)


def test_slack_config_loads_only_named_environment_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "slack.json"
    config_path.write_text(
        json.dumps(
            {
                "version": "1",
                "workspace_id": "T123ABC",
                "app_id": "A123ABC",
                "channel_id": "C123ABC",
                "bot_token_env": "SLACK_TOKEN",
                "signing_secret_env": "SLACK_SIGNING_SECRET",
                "reviewers": [
                    {
                        "user_id": "U123ABC",
                        "subject": "risk@example.com",
                        "decisions": ["approve", "reject"],
                    }
                ],
            }
        )
    )
    config = SlackConfig.from_file(
        config_path,
        environment={"SLACK_TOKEN": BOT_TOKEN, "SLACK_SIGNING_SECRET": SIGNING_SECRET},
    )
    assert config.bot_token == BOT_TOKEN
    assert config.signing_secret == SIGNING_SECRET
    assert BOT_TOKEN not in repr(config)
    assert SIGNING_SECRET not in repr(config)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"user_id": "bad"}, "user_id"),
        ({"subject": ""}, "subject"),
        ({"subject": "risk\nadmin"}, "control"),
        ({"decisions": frozenset()}, "at least one decision"),
    ],
)
def test_slack_reviewer_rejects_unsafe_identity(
    values: dict[str, object],
    message: str,
) -> None:
    reviewer: dict[str, object] = {
        "user_id": "U123ABC",
        "subject": "risk@example.com",
        "decisions": frozenset(Decision),
    }
    reviewer.update(values)
    with pytest.raises(ValueError, match=message):
        SlackReviewer(**reviewer)  # type: ignore[arg-type]


def base_config_mapping() -> dict[str, object]:
    return {
        "version": "1",
        "workspace_id": "T123ABC",
        "app_id": "A123ABC",
        "channel_id": "C123ABC",
        "bot_token_env": "SLACK_TOKEN",
        "signing_secret_env": "SLACK_SECRET",
        "reviewers": [{"user_id": "U123ABC", "subject": "risk@example.com"}],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"version": "2"}, "version"),
        ({"workspace_id": 1}, "must be strings"),
        ({"bot_token_env": "MISSING"}, "is not set"),
        ({"reviewers": "not-a-list"}, "must be a list"),
        ({"reviewers": ["not-an-object"]}, "must be an object"),
        ({"reviewers": [{"user_id": 1, "subject": "risk"}]}, "must be strings"),
        (
            {"reviewers": [{"user_id": "U123ABC", "subject": "risk", "decisions": "approve"}]},
            "must be a list",
        ),
        (
            {"reviewers": [{"user_id": "U123ABC", "subject": "risk", "decisions": [1]}]},
            "must be a string",
        ),
        (
            {
                "reviewers": [
                    {
                        "user_id": "U123ABC",
                        "subject": "risk",
                        "decisions": ["approve", "approve"],
                    }
                ]
            },
            "duplicates",
        ),
        ({"max_attempts": True}, "must be an integer"),
        ({"timeout_seconds": True}, "must be a number"),
        ({"unexpected": True}, "unknown Slack config keys"),
    ],
)
def test_slack_mapping_config_fails_closed(
    change: dict[str, object],
    message: str,
) -> None:
    config = base_config_mapping()
    config.update(change)
    with pytest.raises((TypeError, ValueError), match=message):
        SlackConfig.from_mapping(
            config,
            environment={"SLACK_TOKEN": BOT_TOKEN, "SLACK_SECRET": SIGNING_SECRET},
        )


def test_slack_config_rejects_duplicate_reviewers_bad_env_and_secret_characters() -> None:
    reviewer = make_config().reviewers[0]
    with pytest.raises(ValueError, match="unique"):
        make_config(reviewers=(reviewer, reviewer))
    with pytest.raises(ValueError, match="bot_token_env"):
        make_config(bot_token_env="bad-name")
    with pytest.raises(ValueError, match="printable"):
        make_config(bot_token=BOT_TOKEN + "\n")
    with pytest.raises(ValueError, match="at most 100"):
        make_config(
            reviewers=tuple(
                SlackReviewer(f"U{index:03d}ABC", f"reviewer-{index}", frozenset(Decision))
                for index in range(101)
            )
        )


def test_slack_config_requires_an_object(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("[]")
    with pytest.raises(TypeError, match="JSON object"):
        SlackConfig.from_file(path)
    with pytest.raises(TypeError, match="JSON object"):
        SlackConfig.from_mapping([])  # type: ignore[arg-type]


def test_notification_store_validation_and_binding_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        SlackNotificationStore(tmp_path / "zero.db", claim_lease_seconds=0)
    with pytest.raises(ValueError, match="one nanosecond"):
        SlackNotificationStore(tmp_path / "tiny.db", claim_lease_seconds=1e-12)

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        submit_pending(runtime_store)
        action = runtime_store.get_action("slack-action")
        assert notification_store.enqueue(action, channel_id="C123ABC")
        assert not notification_store.enqueue(action, channel_id="C123ABC")
        notification_store._connection.execute(
            "UPDATE slack_notifications SET message_ts = '1.2' WHERE action_id = ?",
            (action.action_id,),
        )
        with pytest.raises(ValueError, match="not interactive"):
            notification_store.require_message_binding(
                action_id=action.action_id,
                request_digest=action.request_digest,
                channel_id="C123ABC",
                message_ts="1.2",
            )
        with pytest.raises(KeyError, match="unknown"):
            notification_store.require_message_binding(
                action_id="missing",
                request_digest=action.request_digest,
                channel_id="C123ABC",
                message_ts="1.2",
            )
        with pytest.raises(ValueError, match="dead"):
            notification_store.retry_dead(action.action_id)
        with pytest.raises(KeyError, match="unknown"):
            notification_store.retry_dead("missing")
        with pytest.raises(ValueError, match="outcome"):
            notification_store.record_interaction(
                signature_digest="digest",
                action_id=action.action_id,
                request_digest=action.request_digest,
                user_id="U123ABC",
                decision=Decision.APPROVE,
                outcome="",
            )
        with pytest.raises(ValueError, match="binding changed"):
            notification_store.mark_decided(
                action_id=action.action_id,
                request_digest="0" * 64,
                decided_by="reviewer",
                decision=Decision.APPROVE,
            )
        with pytest.raises(ValueError, match="worker_id"):
            notification_store.claim_due(worker_id=" ")
        notification = notification_store.claim_due(worker_id="worker")
        assert notification is not None
        with pytest.raises(ValueError, match="timestamp"):
            notification_store.mark_posted(
                notification,
                worker_id="worker",
                message_ts="invalid",
                status_code=200,
            )
        with pytest.raises(ValueError, match="status code"):
            notification_store.mark_posted(
                notification,
                worker_id="worker",
                message_ts="1.2",
                status_code=99,
            )
        with pytest.raises(ValueError, match="safe code"):
            notification_store.mark_failed(
                notification,
                worker_id="worker",
                config=make_config(),
                status_code=500,
                error="unsafe error",
                retryable=True,
            )
        with pytest.raises(ValueError, match="safe code"):
            notification_store.mark_obsolete(
                notification,
                worker_id="worker",
                reason="unsafe reason",
            )


def test_notification_store_rejects_schema_and_changed_enqueue_binding(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.db"
    connection = sqlite3.connect(schema_path)
    connection.execute("CREATE TABLE slack_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO slack_metadata VALUES ('schema_version', '999')")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="unsupported Slack schema"):
        SlackNotificationStore(schema_path)

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "binding.db") as notification_store,
    ):
        submit_pending(runtime_store)
        action = runtime_store.get_action("slack-action")
        notification_store.enqueue(action, channel_id="C123ABC")
        notification_store._connection.execute(
            "UPDATE slack_notifications SET channel_id = 'C999ABC'"
        )
        with pytest.raises(ValueError, match="binding changed"):
            notification_store.enqueue(action, channel_id="C123ABC")


def test_worker_rejects_bad_identity_lease_and_post_response(tmp_path: Path) -> None:
    async def run() -> None:
        with (
            SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
            SlackNotificationStore(tmp_path / "slack.db") as notification_store,
        ):
            submit_pending(runtime_store)
            with pytest.raises(ValueError, match="worker_id"):
                SlackWorker(
                    runtime_store=runtime_store,
                    notification_store=notification_store,
                    config=make_config(),
                    worker_id=" ",
                )
            with (
                SlackNotificationStore(
                    tmp_path / "short-lease.db",
                    claim_lease_seconds=1,
                ) as short_lease,
                pytest.raises(ValueError, match="lease must be longer"),
            ):
                SlackWorker(
                    runtime_store=runtime_store,
                    notification_store=short_lease,
                    config=make_config(timeout_seconds=2),
                )
            api = RecordingSlackAPI([(200, {"ok": True, "channel": "wrong", "ts": "1.2"}, None)])
            worker = SlackWorker(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=make_config(max_attempts=1),
                api_caller=api,
            )
            result = await worker.run_once()
            assert result["dead"] == 1
            assert notification_store.snapshots()[0].last_error == "InvalidPostResponse"
            with pytest.raises(ValueError, match="poll interval"):
                await worker.run_forever(poll_interval_seconds=0)

    asyncio.run(run())


def test_stale_conflicting_click_updates_to_actual_runtime_decision(tmp_path: Path) -> None:
    api = RecordingSlackAPI([(200, {"ok": True}, None)])
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        runtime_store.decide(request.action_id, Decision.APPROVE, decided_by="dashboard:original")
        body = interaction_body(
            request.action_id,
            request.request_digest,
            action_name="agentbarrier_reject_v1",
        )
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            api_caller=api,
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        with TestClient(app) as client:
            response = client.post(
                "/slack/interactions", content=body, headers=signed_headers(body)
            )
        snapshot = notification_store.snapshots()[0]

    assert response.status_code == 200
    assert snapshot.status == "decided"
    assert snapshot.decision == "approve"
    assert snapshot.decided_by == "dashboard:original"


def test_slack_service_reports_missing_or_changed_runtime_action(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        request = submit_pending(runtime_store)
        mark_posted(runtime_store, notification_store)
        original = runtime_store.get_action
        body = interaction_body(request.action_id, request.request_digest)
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        runtime_store.get_action = lambda _action_id: (_ for _ in ()).throw(KeyError())  # type: ignore[assignment]
        with TestClient(app) as client:
            missing = client.post(
                "/slack/interactions",
                content=body,
                headers=signed_headers(body),
            )
        runtime_store.get_action = lambda action_id: replace(  # type: ignore[method-assign]
            original(action_id),
            request_digest="0" * 64,
        )
        changed_body = interaction_body(
            request.action_id,
            request.request_digest,
            action_name="agentbarrier_reject_v1",
        )
        with TestClient(app) as client:
            changed = client.post(
                "/slack/interactions",
                content=changed_body,
                headers=signed_headers(changed_body),
            )

    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "action_missing"
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "binding_changed"


def test_slack_parser_rejects_malformed_payload_shapes(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        valid = interaction_body("action", "0" * 64)
        encoded_payload = valid.decode().partition("payload=")[2]
        from urllib.parse import unquote_plus

        base = json.loads(unquote_plus(encoded_payload))
        payloads: list[tuple[object, str]] = [
            ([], "invalid_payload"),
            ({**base, "type": "view_submission"}, "unsupported_interaction"),
            ({key: value for key, value in base.items() if key != "team"}, "invalid_payload"),
            ({**base, "user": {"id": "bad"}}, "invalid_identity"),
            ({**base, "actions": []}, "invalid_action"),
            ({**base, "actions": ["bad"]}, "invalid_action"),
            ({**base, "actions": [{}]}, "invalid_action"),
            (
                {**base, "actions": [{"action_id": "unknown", "value": "{}"}]},
                "unknown_action",
            ),
            (
                {
                    **base,
                    "actions": [{"action_id": "agentbarrier_approve_v1", "value": "{"}],
                },
                "invalid_binding",
            ),
            (
                {
                    **base,
                    "actions": [{"action_id": "agentbarrier_approve_v1", "value": "[]"}],
                },
                "invalid_binding",
            ),
            (
                {
                    **base,
                    "actions": [
                        {
                            "action_id": "agentbarrier_approve_v1",
                            "value": json.dumps({"action_id": "action", "request_digest": "bad"}),
                        }
                    ],
                },
                "invalid_binding",
            ),
        ]
        with TestClient(app) as client:
            malformed_form = b"payload"
            responses = [
                client.post(
                    "/slack/interactions",
                    content=malformed_form,
                    headers=signed_headers(malformed_form),
                )
            ]
            invalid_json = b"payload=not-json"
            responses.append(
                client.post(
                    "/slack/interactions",
                    content=invalid_json,
                    headers=signed_headers(invalid_json),
                )
            )
            for payload, _code in payloads:
                body = urlencode({"payload": json.dumps(payload)}).encode()
                responses.append(
                    client.post(
                        "/slack/interactions",
                        content=body,
                        headers=signed_headers(body),
                    )
                )

    expected = ["invalid_form", "invalid_payload", *[code for _, code in payloads]]
    assert [response.status_code for response in responses] == [400] * len(responses)
    assert [response.json()["error"]["code"] for response in responses] == expected


def test_slack_http_routes_and_internal_errors_fail_closed(tmp_path: Path) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as runtime_store,
        SlackNotificationStore(tmp_path / "slack.db") as notification_store,
    ):
        app = SlackInteractionService(
            runtime_store=runtime_store,
            notification_store=notification_store,
            config=make_config(),
            clock_seconds=lambda: NOW_SECONDS,
        ).app
        with TestClient(app, raise_server_exceptions=False) as client:
            missing = client.get("/missing")
            method = client.get("/slack/interactions")
            notification_store.interaction_outcome = lambda _digest: 1 / 0  # type: ignore[assignment,return-value]
            body = interaction_body("action", "0" * 64)
            internal = client.post(
                "/slack/interactions",
                content=body,
                headers=signed_headers(body),
            )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "route_not_found"
    assert method.status_code == 405
    assert method.json()["error"]["code"] == "method_not_allowed"
    assert internal.status_code == 500
    assert internal.json()["error"]["code"] == "internal_error"


def test_slack_http_client_sanitizes_transport_and_response_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def __init__(self, **_values: object) -> None:
            pass

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            raise httpx.ConnectError("secret")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("agentbarrier.service.slack.httpx.AsyncClient", FailingClient)
    with pytest.raises(SlackAPIError, match="ConnectError"):
        asyncio.run(
            slack_module._call_slack_api(
                method="chat.postMessage",
                payload={},
                bot_token=BOT_TOKEN,
                timeout_seconds=2,
            )
        )
    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(
            slack_module._call_slack_api(
                method="users.list",
                payload={},
                bot_token=BOT_TOKEN,
                timeout_seconds=2,
            )
        )
    assert slack_module._retry_after_seconds(None) is None
    assert slack_module._retry_after_seconds("bad") is None
    assert slack_module._retry_after_seconds("-1") is None


def test_run_slack_service_opens_config_state_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.db"
    state_path = tmp_path / "slack.db"
    config_path = tmp_path / "slack.json"
    with SQLiteRuntimeStore(runtime_path):
        pass
    config_path.write_text(json.dumps(base_config_mapping()))
    monkeypatch.setenv("SLACK_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SLACK_SECRET", SIGNING_SECRET)
    captured: dict[str, object] = {}

    def run(app: object, **values: object) -> None:
        captured["app"] = app
        captured.update(values)

    monkeypatch.setattr("agentbarrier.service.runner.uvicorn.run", run)
    service_runner.run_slack_service(
        database_path=runtime_path,
        state_path=state_path,
        config_path=config_path,
    )
    assert state_path.is_file()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8789
    assert captured["log_level"] == "info"
    with pytest.raises(ValueError, match="host"):
        service_runner.run_slack_service(
            database_path=runtime_path,
            state_path=state_path,
            config_path=config_path,
            host=" ",
        )
    with pytest.raises(ValueError, match="port"):
        service_runner.run_slack_service(
            database_path=runtime_path,
            state_path=state_path,
            config_path=config_path,
            port=70_000,
        )
    with pytest.raises(ValueError, match="must be separate"):
        service_runner.run_slack_service(
            database_path=runtime_path,
            state_path=runtime_path,
            config_path=config_path,
        )


def test_verify_slack_signature_validates_exact_body_and_headers() -> None:
    body = b"payload=exact"
    headers = signed_headers(body)
    verify_slack_signature(
        signing_secret=SIGNING_SECRET,
        timestamp=headers["X-Slack-Request-Timestamp"],
        signature=headers["X-Slack-Signature"],
        body=body,
        now_seconds=NOW_SECONDS,
    )
    with pytest.raises(slack_module.SlackRequestError, match="signature"):
        verify_slack_signature(
            signing_secret=SIGNING_SECRET,
            timestamp=str(NOW_SECONDS),
            signature="not-hex",
            body=body,
            now_seconds=NOW_SECONDS,
        )


def test_slack_api_http_client_disables_redirects_and_uses_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def __init__(self) -> None:
            self.headers = {"retry-after": "2"}

        @staticmethod
        def json() -> object:
            return {"ok": True, "channel": "C123ABC", "ts": "1.2"}

    class FakeClient:
        def __init__(self, **values: object) -> None:
            captured.update(values)

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **values: object) -> FakeResponse:
            captured["url"] = url
            captured.update(values)
            return FakeResponse()

    monkeypatch.setattr("agentbarrier.service.slack.httpx.AsyncClient", FakeClient)
    status, data, retry_after = asyncio.run(
        slack_module._call_slack_api(
            method="chat.postMessage",
            payload={"channel": "C123ABC", "text": "hello"},
            bot_token=BOT_TOKEN,
            timeout_seconds=2,
        )
    )
    assert status == 200
    assert data["ok"] is True
    assert retry_after == 2
    assert captured["follow_redirects"] is False
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == f"Bearer {BOT_TOKEN}"


def test_slack_runner_status_retry_and_cli_forwarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "slack.db"
    clock = Clock()
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db", clock_ns=clock.ns) as runtime_store,
        SlackNotificationStore(state_path, clock_ns=clock.ns) as notification_store,
    ):
        submit_pending(runtime_store)
        notification_store.enqueue(runtime_store.get_action("slack-action"), channel_id="C123ABC")
        notification = notification_store.claim_due(worker_id="worker")
        assert notification is not None
        notification_store.mark_failed(
            notification,
            worker_id="worker",
            config=make_config(max_attempts=1),
            status_code=400,
            error="invalid_auth",
            retryable=False,
        )

    assert service_runner.slack_notification_status(state_path)[0].status == "dead"
    assert (
        service_runner.retry_slack_notification(
            state_path,
            action_id="slack-action",
        ).status
        == "pending"
    )
    assert cli.main(["slack", "status", "--state-db", str(state_path), "--json"]) == 0
    assert '"status": "pending"' in capsys.readouterr().out

    captured: dict[str, object] = {}

    def run_slack_service(**values: object) -> None:
        captured.update(values)

    monkeypatch.setattr(service_runner, "run_slack_service", run_slack_service)
    assert (
        cli.main(
            [
                "slack",
                "serve",
                "--db",
                str(tmp_path / "runtime.db"),
                "--state-db",
                str(state_path),
                "--config",
                str(tmp_path / "config.json"),
            ]
        )
        == 0
    )
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8789


def test_slack_api_error_sanitizes_untrusted_codes() -> None:
    error = SlackAPIError(
        "unsafe error with secret detail",
        status_code=500,
        retryable=True,
    )
    assert error.code == "SlackAPIError"
    assert str(error) == "SlackAPIError"

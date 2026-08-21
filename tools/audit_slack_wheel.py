"""Audit a clean installed wheel's signed Slack approval lifecycle without Slack credentials."""

from __future__ import annotations

import asyncio
import hmac
import json
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from starlette.testclient import TestClient

from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service.slack import (
    SlackConfig,
    SlackNotificationStore,
    SlackReviewer,
    SlackWorker,
    create_slack_app,
)

BOT_TOKEN = "xoxb-wheel-audit-token-0123456789"
SIGNING_SECRET = "wheel-audit-signing-secret-0123456789"
NOW = 2_000_000_000


class AuditSlackAPI:
    def __init__(self) -> None:
        self.methods: list[str] = []

    async def __call__(
        self,
        *,
        method: str,
        payload: Mapping[str, object],
        bot_token: str,
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object], float | None]:
        assert bot_token == BOT_TOKEN
        assert timeout_seconds < 3
        self.methods.append(method)
        if method == "chat.postMessage":
            assert payload["channel"] == "C123ABC"
            return 200, {"ok": True, "channel": "C123ABC", "ts": "1700000000.123456"}, None
        return 200, {"ok": True}, None


def main() -> None:
    config = SlackConfig(
        workspace_id="T123ABC",
        app_id="A123ABC",
        channel_id="C123ABC",
        bot_token=BOT_TOKEN,
        bot_token_env="SLACK_TOKEN",
        signing_secret=SIGNING_SECRET,
        signing_secret_env="SLACK_SECRET",
        reviewers=(SlackReviewer("U123ABC", "wheel-auditor", frozenset({Decision.APPROVE})),),
    )
    api = AuditSlackAPI()
    with tempfile.TemporaryDirectory(prefix="agentbarrier-slack-audit-") as temporary:
        root = Path(temporary)
        with (
            SQLiteRuntimeStore(root / "runtime.db") as runtime_store,
            SlackNotificationStore(root / "slack.db") as notification_store,
        ):
            request = RuntimeRequest(
                action_id="wheel-slack-action",
                namespace="billing",
                tool_name="payments.refund",
                arguments={"amount_cents": 2_500, "customer_id": "customer-1"},
                idempotency_key="refund-1",
                policy_version="wheel-slack-v1",
                created_at_ns=1,
            )
            runtime_store.submit(
                request,
                PolicyDecision(
                    PolicyEffect.REQUIRE_APPROVAL,
                    "review refunds",
                    "wheel-slack-v1",
                ),
            )
            worker = SlackWorker(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=config,
                api_caller=api,
                worker_id="wheel-worker",
            )
            result = asyncio.run(worker.run_once())
            assert result["posted"] == 1

            payload = {
                "type": "block_actions",
                "api_app_id": config.app_id,
                "team": {"id": config.workspace_id},
                "channel": {"id": config.channel_id},
                "user": {"id": "U123ABC"},
                "message": {"ts": "1700000000.123456"},
                "actions": [
                    {
                        "action_id": "agentbarrier_approve_v1",
                        "value": json.dumps(
                            {
                                "action_id": request.action_id,
                                "request_digest": request.request_digest,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                ],
            }
            body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
            signature = (
                "v0="
                + hmac.new(
                    SIGNING_SECRET.encode(),
                    f"v0:{NOW}:".encode() + body,
                    sha256,
                ).hexdigest()
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": str(NOW),
                "X-Slack-Signature": signature,
            }
            app = create_slack_app(
                runtime_store=runtime_store,
                notification_store=notification_store,
                config=config,
                api_caller=api,
                clock_seconds=lambda: NOW,
            )
            with TestClient(app) as client:
                first = client.post("/slack/interactions", content=body, headers=headers)
                replay = client.post("/slack/interactions", content=body, headers=headers)
            action = runtime_store.get_action(request.action_id)
            receipt_events = [
                receipt.event.value
                for receipt in runtime_store.receipts(action_id=request.action_id)
            ]
            notification = notification_store.snapshots()[0]

    assert first.status_code == 200
    assert replay.status_code == 200
    assert action.status is RuntimeStatus.APPROVED
    assert action.decided_by == "slack:T123ABC:U123ABC:wheel-auditor"
    assert receipt_events == ["approval_requested", "approved"]
    assert notification.status == "decided"
    assert notification.request_digest == request.request_digest
    assert api.methods == ["chat.postMessage", "chat.update"]
    print("installed wheel Slack audit passed")


if __name__ == "__main__":
    main()

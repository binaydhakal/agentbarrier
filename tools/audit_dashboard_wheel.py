"""Audit an installed wheel through a real dashboard approval lifecycle."""

from __future__ import annotations

import argparse
import html
import json
import re
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import StaticBearerAuth, create_dashboard_app, hash_bearer_token

_REVIEWER_TOKEN = "dashboard-wheel-reviewer-token-0123456789"
_ORIGIN = "http://testserver"
_CSRF_PATTERN = re.compile(r'name="csrf" value="([A-Za-z0-9_-]+)"')


def run_audit(directory: Path) -> dict[str, object]:
    """Exchange a credential for a session and approve one exact installed-wheel action."""

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "runtime.db"
    action_id = "dashboard-wheel-action"
    request = RuntimeRequest(
        action_id=action_id,
        namespace="wheel-audit",
        tool_name="payments.refund",
        arguments={
            "request_id": action_id,
            "account_id": "account-wheel-1",
            "amount_cents": 2_500,
        },
        idempotency_key=action_id,
        policy_version="dashboard-wheel-policy-v1",
        created_at_ns=1,
    )
    auth = StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "dashboard-wheel-reviewer",
                    "token_sha256": hash_bearer_token(_REVIEWER_TOKEN),
                    "scopes": ["actions:read", "actions:decide"],
                }
            ],
        }
    )

    with SQLiteRuntimeStore(database) as store:
        store.submit(
            request,
            PolicyDecision(
                PolicyEffect.REQUIRE_APPROVAL,
                "review refunds",
                "dashboard-wheel-policy-v1",
            ),
        )
        app = create_dashboard_app(
            store=store,
            auth=auth,
            cookie_secure=False,
            public_origin=_ORIGIN,
        )
        with TestClient(app) as client:
            login_form = client.get("/dashboard/login")
            login_csrf = _csrf(login_form.text)
            signed_in = client.post(
                "/dashboard/login",
                data={"token": _REVIEWER_TOKEN, "csrf": login_csrf},
                headers={"Origin": _ORIGIN},
                follow_redirects=False,
            )
            if signed_in.status_code != 303:
                raise AssertionError("installed dashboard did not create a reviewer session")
            if _REVIEWER_TOKEN in signed_in.text or _REVIEWER_TOKEN in signed_in.headers.get(
                "set-cookie", ""
            ):
                raise AssertionError("installed dashboard exposed the bearer credential")

            detail = client.get(f"/dashboard/actions/{action_id}")
            if detail.status_code != 200 or '"amount_cents": 2500' not in html.unescape(
                detail.text
            ):
                raise AssertionError("installed dashboard did not render exact action arguments")
            if detail.headers.get("cache-control") != "no-store":
                raise AssertionError("installed dashboard allowed sensitive response caching")
            if "default-src 'none'" not in detail.headers.get("content-security-policy", ""):
                raise AssertionError("installed dashboard omitted its restrictive CSP")

            approved = client.post(
                f"/dashboard/actions/{action_id}/approve",
                data={"csrf": _csrf(detail.text), "reason": "clean-install verification"},
                headers={"Origin": _ORIGIN},
                follow_redirects=False,
            )
            if approved.status_code != 303:
                raise AssertionError("installed dashboard did not record the approval")

        action = store.get_action(action_id)
        if action.status is not RuntimeStatus.APPROVED:
            raise AssertionError(f"expected approved action, observed {action.status.value}")
        if action.decided_by != "dashboard-wheel-reviewer":
            raise AssertionError("installed dashboard did not bind reviewer identity")
        if not store.verify_receipt_chain():
            raise AssertionError("installed dashboard receipt chain is invalid")
        events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
        if events != ["approval_requested", "approved"]:
            raise AssertionError(f"unexpected installed dashboard events: {events}")

    return {
        "action_id": action_id,
        "decided_by": action.decided_by,
        "events": events,
        "status": "passed",
    }


def _csrf(document: str) -> str:
    match = _CSRF_PATTERN.search(document)
    if match is None:
        raise AssertionError("installed dashboard omitted its CSRF field")
    return match.group(1)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(options.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="agentbarrier-dashboard-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

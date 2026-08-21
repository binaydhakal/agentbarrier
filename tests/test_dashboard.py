from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import uvicorn
from httpx2 import Response
from starlette.testclient import TestClient

from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    RuntimeRequest,
    RuntimeStatus,
    SQLiteRuntimeStore,
)
from agentbarrier.service import StaticBearerAuth, hash_bearer_token
from agentbarrier.service import runner as service_runner
from agentbarrier.service.dashboard import DashboardSessionStore, create_dashboard_app

REVIEWER_TOKEN = "dashboard-reviewer-token-0123456789"
READER_TOKEN = "dashboard-reader-token-012345678901"
ORIGIN = "http://testserver"


class Clock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def make_auth() -> StaticBearerAuth:
    return StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "reviewer@example.com",
                    "token_sha256": hash_bearer_token(REVIEWER_TOKEN),
                    "scopes": ["actions:read", "actions:decide", "audit:read"],
                },
                {
                    "subject": "read-only@example.com",
                    "token_sha256": hash_bearer_token(READER_TOKEN),
                    "scopes": ["actions:read"],
                },
            ],
        }
    )


def submit_pending(
    store: SQLiteRuntimeStore,
    *,
    action_id: str = "dashboard-action",
    note: str = "ordinary",
) -> RuntimeRequest:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": action_id, "amount_cents": 2_500, "note": note},
        idempotency_key=action_id,
        policy_version="dashboard-policy-v1",
        created_at_ns=1,
    )
    store.submit(
        request,
        PolicyDecision(
            PolicyEffect.REQUIRE_APPROVAL,
            "review refunds",
            "dashboard-policy-v1",
        ),
    )
    return request


def csrf_from(response_text: str) -> str:
    match = re.search(r'name="csrf" value="([A-Za-z0-9_-]+)"', response_text)
    assert match is not None
    return match.group(1)


def sign_in(client: TestClient, token: str = REVIEWER_TOKEN) -> Response:
    form = client.get("/dashboard/login")
    csrf = csrf_from(form.text)
    return client.post(
        "/dashboard/login",
        data={"token": token, "csrf": csrf},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


def test_dashboard_exchanges_bearer_for_opaque_session_and_renders_queue(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        store.set_pause(paused_by="on-call", reason="incident")
        store.configure_limit(
            "refund-budget",
            window_seconds=300,
            max_actions=20,
            value_argument="amount_cents",
            max_value=50_000,
            updated_by="risk-team",
            reason="blast radius",
        )
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            unauthorized = client.get("/dashboard/")
            login_form = client.get("/dashboard/login", headers={"X-Request-Id": "dash-123"})
            signed_in = sign_in(client)
            queue = client.get("/dashboard/")
            stylesheet = client.get("/dashboard/assets/style.css")

    assert unauthorized.status_code == 401
    assert "Sign-in required" in unauthorized.text
    assert login_form.status_code == 200
    assert login_form.headers["x-request-id"] == "dash-123"
    assert login_form.headers["cache-control"] == "no-store"
    assert login_form.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    assert login_form.headers["x-frame-options"] == "DENY"
    assert "<script" not in login_form.text

    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/dashboard/"
    session_cookie = signed_in.headers["set-cookie"]
    assert "agentbarrier_session=" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert REVIEWER_TOKEN not in session_cookie
    assert REVIEWER_TOKEN not in signed_in.text

    assert queue.status_code == 200
    assert '<html lang="en">' in queue.text
    assert 'href="#main">Skip to main content' in queue.text
    assert "reviewer@example.com" in queue.text
    assert "payments.refund" in queue.text
    assert "1</span> active emergency pause" in queue.text
    assert "1</span> enabled execution limit" in queue.text
    assert "Runtime actions" in queue.text
    assert "Current limit windows" in queue.text
    assert "<script" not in queue.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")


def test_dashboard_approves_exact_action_with_authenticated_identity(tmp_path: Path) -> None:
    dangerous = '</pre><script>alert("x")</script>'
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store, note=dangerous)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            assert sign_in(client).status_code == 303
            detail = client.get("/dashboard/actions/dashboard-action")
            csrf = csrf_from(detail.text)
            approved = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": csrf, "reason": "ticket-123"},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            result = client.get(approved.headers["location"])
        action = store.get_action("dashboard-action")
        events = [receipt.event.value for receipt in store.receipts(action_id=action.action_id)]

    assert detail.status_code == 200
    assert "Exact arguments" in detail.text
    assert dangerous not in detail.text
    assert "&lt;script&gt;alert" in detail.text
    assert '<label for="approve-reason">' in detail.text
    assert '<label for="reject-reason">' in detail.text
    assert approved.status_code == 303
    assert approved.headers["location"].endswith("?decision=approved")
    assert "Action approved." in result.text
    assert action.status is RuntimeStatus.APPROVED
    assert action.decided_by == "reviewer@example.com"
    assert action.decision_reason == "ticket-123"
    assert events == ["approval_requested", "approved"]


def test_dashboard_reader_cannot_decide_action(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            assert sign_in(client, READER_TOKEN).status_code == 303
            detail = client.get("/dashboard/actions/dashboard-action")
            csrf = csrf_from(detail.text)
            forbidden = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": csrf},
                headers={"Origin": ORIGIN},
            )
        action = store.get_action("dashboard-action")

    assert "cannot decide them" in detail.text
    assert "/approve" not in detail.text
    assert forbidden.status_code == 403
    assert "Access denied" in forbidden.text
    assert action.status is RuntimeStatus.PENDING


@pytest.mark.parametrize(
    ("origin", "csrf_value"),
    [
        ("https://evil.example", "valid"),
        (ORIGIN, "wrong-csrf-token-value-012345678901"),
        ("not an origin", "valid"),
    ],
)
def test_dashboard_rejects_cross_origin_and_csrf_attacks(
    tmp_path: Path,
    origin: str | None,
    csrf_value: str,
) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            assert sign_in(client).status_code == 303
            session_csrf = csrf_from(client.get("/dashboard/actions/dashboard-action").text)
            presented = session_csrf if csrf_value == "valid" else csrf_value
            headers = {"Origin": origin} if origin is not None else {}
            response = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": presented},
                headers=headers,
            )
        action = store.get_action("dashboard-action")

    assert response.status_code == 403
    assert "Request rejected" in response.text
    assert action.status is RuntimeStatus.PENDING


def test_dashboard_accepts_valid_csrf_when_browser_omits_origin_headers(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            form = client.get("/dashboard/login")
            signed_in = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": csrf_from(form.text)},
                follow_redirects=False,
            )
            detail = client.get("/dashboard/actions/dashboard-action")
            approved = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": csrf_from(detail.text)},
                follow_redirects=False,
            )
        action = store.get_action("dashboard-action")

    assert signed_in.status_code == 303
    assert approved.status_code == 303
    assert action.status is RuntimeStatus.APPROVED


def test_dashboard_accepts_opaque_same_origin_browser_with_valid_csrf(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            form = client.get("/dashboard/login")
            accepted = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": csrf_from(form.text)},
                headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
                follow_redirects=False,
            )
            client.cookies.clear()
            form = client.get("/dashboard/login")
            rejected = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": csrf_from(form.text)},
                headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
            )

    assert accepted.status_code == 303
    assert rejected.status_code == 403


def test_dashboard_rejects_login_csrf_invalid_credentials_and_token_leakage(tmp_path: Path) -> None:
    unknown_token = "unknown-dashboard-token-012345678901"
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            form = client.get("/dashboard/login")
            csrf = csrf_from(form.text)
            wrong_csrf = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": "wrong-login-csrf-token-0123456789"},
                headers={"Origin": ORIGIN},
            )
            cross_origin = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": csrf},
                headers={"Origin": "https://evil.example"},
            )
            invalid = client.post(
                "/dashboard/login",
                data={"token": unknown_token, "csrf": csrf},
                headers={"Origin": ORIGIN},
            )

    assert wrong_csrf.status_code == 403
    assert cross_origin.status_code == 403
    assert invalid.status_code == 401
    assert "invalid or cannot read" in invalid.text
    assert unknown_token not in invalid.text
    assert unknown_token not in invalid.headers.get("set-cookie", "")


def test_dashboard_logout_and_expired_session_fail_closed(tmp_path: Path) -> None:
    clock = Clock()
    sessions = DashboardSessionStore(ttl_seconds=1, clock_ns=clock)
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            sessions=sessions,
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            assert sign_in(client).status_code == 303
            page = client.get("/dashboard/")
            csrf = csrf_from(page.text)
            logged_out = client.post(
                "/dashboard/logout",
                data={"csrf": csrf},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            after_logout = client.get("/dashboard/")
            assert sign_in(client).status_code == 303
            clock.value += 1_000_000_000
            expired = client.get("/dashboard/")

    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/dashboard/login"
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    assert after_logout.status_code == 401
    assert expired.status_code == 401


@pytest.mark.parametrize(
    ("content_type", "body", "status"),
    [
        ("application/json", b"{}", 415),
        ("application/x-www-form-urlencoded", b"csrf=a&csrf=b", 400),
        ("application/x-www-form-urlencoded", b"unknown=value", 400),
        ("application/x-www-form-urlencoded", b"x" * (16 * 1024 + 1), 413),
    ],
)
def test_dashboard_rejects_adversarial_form_shapes(
    tmp_path: Path,
    content_type: str,
    body: bytes,
    status: int,
) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            response = client.post(
                "/dashboard/login",
                content=body,
                headers={"Content-Type": content_type, "Origin": ORIGIN},
            )

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "Traceback" not in response.text


def test_secure_dashboard_uses_host_cookie_hsts_and_referer_fallback(tmp_path: Path) -> None:
    origin = "https://review.example.com"
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=True,
            public_origin=origin,
        )
        with TestClient(app, base_url=origin) as client:
            form = client.get("/dashboard/login")
            csrf = csrf_from(form.text)
            signed_in = client.post(
                "/dashboard/login",
                data={"token": REVIEWER_TOKEN, "csrf": csrf},
                headers={"Referer": f"{origin}/dashboard/login"},
                follow_redirects=False,
            )
            queue = client.get("/dashboard/")

    cookies = signed_in.headers.get_list("set-cookie")
    assert signed_in.status_code == 303
    assert any("__Host-agentbarrier_session=" in item for item in cookies)
    assert any("Secure" in item and "Path=/" in item for item in cookies)
    assert queue.status_code == 200
    assert queue.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://review.example.com",
        "https://user@review.example.com",
        "https://review.example.com/path",
        "https://review.example.com?query=1",
        "https://review.example.com:0",
    ],
)
def test_dashboard_rejects_invalid_public_origins(tmp_path: Path, origin: str) -> None:
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(ValueError, match="origin"),
    ):
        create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=True,
            public_origin=origin,
        )


def test_dashboard_session_store_is_bounded_and_validates_configuration() -> None:
    principal = make_auth().authenticate(f"Bearer {REVIEWER_TOKEN}")
    sessions = DashboardSessionStore(max_sessions=1)
    token, session = sessions.create(principal)
    assert sessions.get(token) == session
    with pytest.raises(RuntimeError, match="capacity"):
        sessions.create(principal)
    sessions.delete(token)
    assert sessions.get(token) is None
    assert sessions.get("malformed") is None

    for ttl in (0, float("nan"), float("inf"), 8 * 24 * 60 * 60):
        with pytest.raises(ValueError, match="TTL"):
            DashboardSessionStore(ttl_seconds=ttl)
    for maximum in (0, True):
        with pytest.raises(ValueError, match="max_sessions"):
            DashboardSessionStore(max_sessions=maximum)


def test_dashboard_handles_changed_state_missing_actions_and_bad_queries(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        submit_pending(store)
        app = create_dashboard_app(
            store=store,
            auth=make_auth(),
            cookie_secure=False,
            public_origin=ORIGIN,
        )
        with TestClient(app) as client:
            assert sign_in(client).status_code == 303
            detail = client.get("/dashboard/actions/dashboard-action")
            csrf = csrf_from(detail.text)
            store.decide("dashboard-action", Decision.REJECT, decided_by="other-reviewer")
            conflict = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": csrf},
                headers={"Origin": ORIGIN},
            )
            missing = client.get("/dashboard/actions/missing")
            bad_filter = client.get("/dashboard/?status=not-a-status")
            duplicate = client.get("/dashboard/?status=pending&status=approved")
            unknown = client.get("/dashboard/?unknown=1")
            bad_decision = client.get("/dashboard/actions/dashboard-action?decision=other")

    assert conflict.status_code == 409
    assert missing.status_code == 404
    assert bad_filter.status_code == 400
    assert duplicate.status_code == 400
    assert unknown.status_code == 400
    assert bad_decision.status_code == 400


def test_dashboard_runner_rejects_public_insecure_listener_and_runs_on_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        service_runner.run_approval_dashboard(
            database_path=tmp_path / "missing.db",
            auth_path=tmp_path / "missing-auth.json",
            host="0.0.0.0",
        )

    database = tmp_path / "runtime.db"
    with SQLiteRuntimeStore(database):
        pass
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": "1",
                "tokens": [
                    {
                        "subject": "reviewer@example.com",
                        "token_sha256": hash_bearer_token(REVIEWER_TOKEN),
                        "scopes": ["actions:read", "actions:decide"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    captured: list[dict[str, object]] = []

    def run_app(app: object, **keywords: object) -> None:
        captured.append({"app": app, **keywords})

    monkeypatch.setattr(uvicorn, "run", run_app)
    service_runner.run_approval_dashboard(
        database_path=database,
        auth_path=auth_path,
        session_ttl_seconds=60,
    )
    assert captured[0]["host"] == "127.0.0.1"
    assert captured[0]["port"] == 8788
    assert captured[0]["log_level"] == "info"


@pytest.mark.parametrize("host", ["", "example.com"])
def test_dashboard_runner_validates_listener_configuration(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError):
        service_runner.run_approval_dashboard(
            database_path=tmp_path / "missing.db",
            auth_path=tmp_path / "missing-auth.json",
            host=host,
            cookie_secure=host == "example.com",
        )

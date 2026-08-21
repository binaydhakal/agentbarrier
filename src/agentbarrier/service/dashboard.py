"""Server-rendered approval dashboard with opaque sessions and CSRF protection."""

# ruff: noqa: E501 -- HTML and CSS templates are kept readable as literal source lines.

from __future__ import annotations

import hmac
import html
import json
import math
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentbarrier.errors import InvalidActionState, RuntimeActionError
from agentbarrier.models import Decision
from agentbarrier.runtime import RuntimeAction, RuntimeStatus, RuntimeStore
from agentbarrier.service.auth import AuthenticationError, Principal, StaticBearerAuth

_MAX_FORM_BYTES = 16 * 1024
_MAX_DASHBOARD_ACTIONS = 100
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_OPAQUE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_DASHBOARD_CSS = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; color: #172033; background: #f5f7fb; line-height: 1.5; }
a { color: #155eef; }
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible {
  outline: 3px solid #84adff; outline-offset: 2px;
}
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 1rem; top: 1rem; z-index: 10; background: white; padding: .75rem; }
header { background: #111827; color: white; padding: 1rem 0; }
.shell { width: min(1120px, calc(100% - 2rem)); margin: 0 auto; }
.bar { display: flex; gap: 1rem; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.brand { color: white; text-decoration: none; font-weight: 750; font-size: 1.1rem; }
main { padding: 2rem 0 4rem; }
.card { background: white; border: 1px solid #dfe4ec; border-radius: .75rem; padding: 1.25rem; margin-bottom: 1rem; box-shadow: 0 1px 2px #1018280d; }
.narrow { max-width: 32rem; margin: 3rem auto; }
h1 { font-size: 1.75rem; margin: 0 0 .5rem; }
h2 { font-size: 1.15rem; margin: 0 0 .75rem; }
p { margin: .5rem 0 1rem; }
.muted { color: #526078; }
.notice { border-left: .3rem solid #155eef; background: #eff4ff; padding: .75rem 1rem; margin: 1rem 0; }
.error { border-left-color: #b42318; background: #fef3f2; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: 1rem; }
.metric { font-size: 1.6rem; font-weight: 750; display: block; }
label { display: block; font-weight: 650; margin: 1rem 0 .35rem; }
input, textarea, select { width: 100%; border: 1px solid #98a2b3; border-radius: .4rem; padding: .65rem; font: inherit; background: white; }
textarea { min-height: 6rem; resize: vertical; }
button, .button { border: 0; border-radius: .4rem; padding: .65rem 1rem; font: inherit; font-weight: 700; cursor: pointer; background: #155eef; color: white; text-decoration: none; display: inline-block; }
.danger { background: #b42318; }
.secondary { background: #344054; }
.actions { display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; }
.inline { display: inline; }
table { border-collapse: collapse; width: 100%; }
caption { text-align: left; font-weight: 700; padding: 0 0 .75rem; }
th, td { border-bottom: 1px solid #e4e7ec; text-align: left; padding: .7rem .5rem; vertical-align: top; }
th { color: #475467; font-size: .875rem; }
.table-wrap { overflow-x: auto; }
.status { display: inline-block; border-radius: 999px; padding: .15rem .55rem; font-size: .8rem; font-weight: 750; background: #eef2f6; }
.pending { background: #fff3d6; color: #7a4d00; }
.approved, .succeeded { background: #dcfae6; color: #067647; }
.rejected, .denied, .unknown { background: #fee4e2; color: #b42318; }
dl { display: grid; grid-template-columns: minmax(9rem, 14rem) 1fr; gap: .5rem 1rem; }
dt { font-weight: 700; color: #475467; }
dd { margin: 0; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #101828; color: #f2f4f7; border-radius: .5rem; padding: 1rem; overflow: auto; }
.decision-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr)); gap: 1rem; }
@media (max-width: 640px) { dl { grid-template-columns: 1fr; } dd { margin-bottom: .5rem; } }
""".strip()


@dataclass(frozen=True, slots=True)
class DashboardSession:
    """Server-side state for one opaque browser session."""

    principal: Principal
    csrf_token: str
    expires_at_ns: int


class DashboardSessionStore:
    """Bounded, process-local opaque sessions that never retain bearer credentials."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 8 * 60 * 60,
        max_sessions: int = 1_000,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("dashboard session TTL must be finite and greater than zero")
        if ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("dashboard session TTL must not exceed seven days")
        ttl_ns = int(ttl_seconds * 1_000_000_000)
        if ttl_ns < 1:
            raise ValueError("dashboard session TTL must be at least one nanosecond")
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int) or max_sessions < 1:
            raise ValueError("dashboard max_sessions must be a positive integer")
        self.ttl_seconds = ttl_seconds
        self._ttl_ns = ttl_ns
        self._max_sessions = max_sessions
        self._clock_ns = clock_ns
        self._sessions: dict[str, DashboardSession] = {}
        self._lock = threading.Lock()

    def create(self, principal: Principal) -> tuple[str, DashboardSession]:
        """Create a random browser token and retain only its digest."""

        now = self._clock_ns()
        with self._lock:
            self._remove_expired(now)
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("dashboard session capacity is exhausted")
            while True:  # pragma: no branch - a 256-bit digest collision is not practical
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)
                if digest not in self._sessions:
                    break
            session = DashboardSession(
                principal=principal,
                csrf_token=secrets.token_urlsafe(32),
                expires_at_ns=now + self._ttl_ns,
            )
            self._sessions[digest] = session
        return token, session

    def get(self, token: str | None) -> DashboardSession | None:
        """Resolve a valid unexpired cookie without exposing stored bearer material."""

        if token is None or _OPAQUE_TOKEN_PATTERN.fullmatch(token) is None:
            return None
        digest = self._digest(token)
        now = self._clock_ns()
        with self._lock:
            session = self._sessions.get(digest)
            if session is None:
                return None
            if now >= session.expires_at_ns:
                del self._sessions[digest]
                return None
            return session

    def delete(self, token: str | None) -> None:
        """Invalidate one browser session if its token is well formed."""

        if token is None or _OPAQUE_TOKEN_PATTERN.fullmatch(token) is None:
            return
        with self._lock:
            self._sessions.pop(self._digest(token), None)

    def _remove_expired(self, now: int) -> None:
        expired = [key for key, value in self._sessions.items() if now >= value.expires_at_ns]
        for key in expired:
            del self._sessions[key]

    @staticmethod
    def _digest(token: str) -> str:
        return sha256(token.encode("ascii")).hexdigest()


class DashboardError(Exception):
    """Safe dashboard error rendered without internal exception details."""

    def __init__(self, status_code: int, title: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.title = title
        self.message = message


class DashboardSecurityHeadersMiddleware:
    """Apply browser isolation, request IDs, and non-cacheable responses."""

    def __init__(self, app: ASGIApp, *, secure_transport: bool) -> None:
        self.app = app
        self.secure_transport = secure_transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        candidate = Headers(scope=scope).get("x-request-id")
        request_id = (
            candidate
            if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate)
            else str(uuid.uuid4())
        )
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = request_id
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; style-src 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                )
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
                )
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["Vary"] = "Cookie"
                if self.secure_transport:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ApprovalDashboard:
    """Small server-rendered review UI around one trusted runtime store."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        auth: StaticBearerAuth,
        sessions: DashboardSessionStore | None = None,
        cookie_secure: bool = True,
        public_origin: str | None = None,
    ) -> None:
        self.store = store
        self.auth = auth
        self.sessions = sessions or DashboardSessionStore()
        self.cookie_secure = cookie_secure
        self.public_origin = _normalize_public_origin(public_origin)
        if (
            cookie_secure
            and self.public_origin is not None
            and not self.public_origin.startswith("https://")
        ):
            raise ValueError("secure dashboard cookies require an HTTPS public origin")
        self.session_cookie = (
            "__Host-agentbarrier_session" if cookie_secure else "agentbarrier_session"
        )
        self.login_csrf_cookie = (
            "__Host-agentbarrier_login_csrf" if cookie_secure else "agentbarrier_login_csrf"
        )
        self.cookie_path = "/" if cookie_secure else "/dashboard"
        self.app = Starlette(
            debug=False,
            routes=[
                Route("/dashboard", self.root, methods=["GET"]),
                Route("/dashboard/", self.index, methods=["GET"]),
                Route("/dashboard/login", self.login_form, methods=["GET"]),
                Route("/dashboard/login", self.login, methods=["POST"]),
                Route("/dashboard/logout", self.logout, methods=["POST"]),
                Route("/dashboard/assets/style.css", self.stylesheet, methods=["GET"]),
                Route("/dashboard/actions/{action_id:str}", self.action_detail, methods=["GET"]),
                Route(
                    "/dashboard/actions/{action_id:str}/approve",
                    self.approve_action,
                    methods=["POST"],
                ),
                Route(
                    "/dashboard/actions/{action_id:str}/reject",
                    self.reject_action,
                    methods=["POST"],
                ),
            ],
            exception_handlers={
                DashboardError: self.dashboard_error,
                HTTPException: self.http_error,
                Exception: self.internal_error,
            },
        )
        self.app.add_middleware(
            DashboardSecurityHeadersMiddleware,
            secure_transport=cookie_secure,
        )

    async def root(self, request: Request) -> Response:
        del request
        return RedirectResponse("/dashboard/", status_code=308)

    async def stylesheet(self, request: Request) -> Response:
        del request
        return PlainTextResponse(_DASHBOARD_CSS, media_type="text/css")

    async def login_form(self, request: Request) -> Response:
        if self._session(request) is not None:
            return RedirectResponse("/dashboard/", status_code=303)
        return self._new_login_response()

    async def login(self, request: Request) -> Response:
        self._require_same_origin(request)
        form = await _read_form(request, allowed={"token", "csrf"})
        presented_csrf = form.get("csrf", "")
        cookie_csrf = request.cookies.get(self.login_csrf_cookie, "")
        if (
            _OPAQUE_TOKEN_PATTERN.fullmatch(presented_csrf) is None
            or _OPAQUE_TOKEN_PATTERN.fullmatch(cookie_csrf) is None
            or not hmac.compare_digest(presented_csrf, cookie_csrf)
        ):
            raise DashboardError(403, "Request rejected", "The login form expired or is invalid.")
        token = form.get("token", "")
        try:
            principal = self.auth.authenticate(f"Bearer {token}")
            self.auth.require_scope(principal, "actions:read")
        except AuthenticationError:
            return self._new_login_response(
                error="The credential is invalid or cannot read approval actions.",
                status_code=401,
            )
        try:
            session_token, _session = self.sessions.create(principal)
        except RuntimeError as error:
            raise DashboardError(
                503,
                "Dashboard unavailable",
                "The dashboard cannot create another session right now.",
            ) from error
        response = RedirectResponse("/dashboard/", status_code=303)
        response.set_cookie(
            self.session_cookie,
            session_token,
            max_age=max(1, math.ceil(self.sessions.ttl_seconds)),
            path=self.cookie_path,
            secure=self.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        response.delete_cookie(self.login_csrf_cookie, path=self.cookie_path)
        return response

    async def logout(self, request: Request) -> Response:
        session = self._require_session(request)
        self._require_same_origin(request)
        form = await _read_form(request, allowed={"csrf"})
        self._require_csrf(session, form.get("csrf"))
        self.sessions.delete(request.cookies.get(self.session_cookie))
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(self.session_cookie, path=self.cookie_path)
        return response

    async def index(self, request: Request) -> Response:
        session = self._require_session(request)
        unknown_query = set(request.query_params) - {"status"}
        if unknown_query:
            raise DashboardError(400, "Invalid filter", "The dashboard filter is not recognized.")
        raw_status = request.query_params.get("status", RuntimeStatus.PENDING.value)
        if len(request.query_params.getlist("status")) > 1:
            raise DashboardError(400, "Invalid filter", "The status filter must not be repeated.")
        if raw_status == "all":
            status = None
        else:
            try:
                status = RuntimeStatus(raw_status)
            except ValueError as error:
                raise DashboardError(
                    400,
                    "Invalid filter",
                    "The selected action status is not recognized.",
                ) from error
        actions = self.store.list_actions(status=status)[:_MAX_DASHBOARD_ACTIONS]
        pauses = self.store.list_pauses()
        limits = self.store.list_limits()
        usage = {item.limit_id: item for item in self.store.limit_usage()}
        options = [
            f'<option value="all"{_selected(raw_status, "all")}>All</option>',
            *[
                f'<option value="{item.value}"{_selected(raw_status, item.value)}>'
                f"{html.escape(item.value.replace('_', ' ').title())}</option>"
                for item in RuntimeStatus
            ],
        ]
        rows = "".join(_action_row(action) for action in actions)
        if not rows:
            rows = '<tr><td colspan="5">No actions match this filter.</td></tr>'
        pause_summary = (
            f'<span class="metric">{len(pauses)}</span> active emergency pause'
            f"{'s' if len(pauses) != 1 else ''}"
        )
        enabled_limits = sum(limit.enabled for limit in limits)
        limit_summary = (
            f'<span class="metric">{enabled_limits}</span> enabled execution limit'
            f"{'s' if enabled_limits != 1 else ''}"
        )
        usage_rows = (
            "".join(
                "<tr>"
                f"<td>{html.escape(limit.limit_id)}</td>"
                f"<td>{'Enabled' if limit.enabled else 'Disabled'}</td>"
                f"<td>{usage[limit.limit_id].actions_used}</td>"
                f"<td>{usage[limit.limit_id].value_used}</td>"
                "</tr>"
                for limit in limits
            )
            or '<tr><td colspan="4">No execution limits configured.</td></tr>'
        )
        body = f"""
        <div class="bar"><div><h1>Approval queue</h1><p class="muted">Review exact agent actions before execution.</p></div>
        <form method="get" action="/dashboard/"><label for="status">Status</label>
        <div class="actions"><select id="status" name="status">{"".join(options)}</select><button type="submit">Filter</button></div></form></div>
        <div class="grid"><section class="card" aria-label="Emergency pause status">{pause_summary}</section>
        <section class="card" aria-label="Execution limit status">{limit_summary}</section>
        <section class="card" aria-label="Audit status"><span class="metric">{"Valid" if self.store.verify_receipt_chain() else "Invalid"}</span> action receipt chain</section></div>
        <section class="card"><div class="table-wrap"><table><caption>Runtime actions</caption>
        <thead><tr><th scope="col">Created</th><th scope="col">Status</th><th scope="col">Namespace</th><th scope="col">Tool</th><th scope="col">Review</th></tr></thead>
        <tbody>{rows}</tbody></table></div></section>
        <section class="card"><div class="table-wrap"><table><caption>Current limit windows</caption>
        <thead><tr><th scope="col">Limit</th><th scope="col">State</th><th scope="col">Actions used</th><th scope="col">Value used</th></tr></thead>
        <tbody>{usage_rows}</tbody></table></div></section>
        """
        return self._page("Approval queue", body, session=session)

    async def action_detail(self, request: Request) -> Response:
        session = self._require_session(request)
        _reject_dashboard_query(request, {"decision"})
        action_id = _dashboard_action_id(request)
        try:
            action = self.store.get_action(action_id)
        except KeyError as error:
            raise DashboardError(
                404, "Action not found", "The requested action does not exist."
            ) from error
        decision = request.query_params.get("decision")
        notice = ""
        if decision in {"approved", "rejected"}:
            notice = f'<div class="notice" role="status">Action {decision}.</div>'
        elif decision is not None:
            raise DashboardError(400, "Invalid result", "The decision result is not recognized.")
        decision_forms = ""
        if action.status is RuntimeStatus.PENDING and session.principal.has_scope("actions:decide"):
            encoded_id = quote(action.action_id, safe="")
            csrf = html.escape(session.csrf_token, quote=True)
            decision_forms = f"""
            <section class="decision-grid" aria-label="Decision forms">
              <form class="card" method="post" action="/dashboard/actions/{encoded_id}/approve">
                <h2>Approve this exact action</h2><label for="approve-reason">Reason (optional)</label>
                <textarea id="approve-reason" name="reason" maxlength="2000"></textarea>
                <input type="hidden" name="csrf" value="{csrf}"><button type="submit">Approve action</button>
              </form>
              <form class="card" method="post" action="/dashboard/actions/{encoded_id}/reject">
                <h2>Reject this action</h2><label for="reject-reason">Reason (optional)</label>
                <textarea id="reject-reason" name="reason" maxlength="2000"></textarea>
                <input type="hidden" name="csrf" value="{csrf}"><button class="danger" type="submit">Reject action</button>
              </form>
            </section>
            """
        elif action.status is RuntimeStatus.PENDING:
            decision_forms = '<div class="notice">Your identity can inspect actions but cannot decide them.</div>'
        body = f"""
        <p><a href="/dashboard/">← Back to approval queue</a></p>{notice}
        <section class="card"><h1>Action details</h1><dl>
        {_detail("Action ID", action.action_id)}{_detail("Status", action.status.value)}
        {_detail("Namespace", action.namespace)}{_detail("Tool", action.tool_name)}
        {_detail("Policy rule", action.policy_rule)}{_detail("Policy version", action.policy_version)}
        {_detail("Created", _timestamp(action.created_at_ns))}{_detail("Decided by", action.decided_by or "—")}
        {_detail("Decision reason", action.decision_reason or "—")}</dl>
        <h2>Exact arguments</h2>{_json_block(dict(action.arguments))}
        <h2>Stored result</h2>{_json_block(action.result) if action.result_available else "<p>Not available.</p>"}
        </section>{decision_forms}
        """
        return self._page(f"Action {action.action_id}", body, session=session)

    async def approve_action(self, request: Request) -> Response:
        return await self._decide(request, Decision.APPROVE)

    async def reject_action(self, request: Request) -> Response:
        return await self._decide(request, Decision.REJECT)

    async def _decide(self, request: Request, decision: Decision) -> Response:
        session = self._require_session(request)
        self._require_scope(session, "actions:decide")
        self._require_same_origin(request)
        form = await _read_form(request, allowed={"csrf", "reason"})
        self._require_csrf(session, form.get("csrf"))
        reason = _validate_reason(form.get("reason"))
        action_id = _dashboard_action_id(request)
        try:
            self.store.decide(
                action_id,
                decision,
                decided_by=session.principal.subject,
                reason=reason,
            )
        except KeyError as error:
            raise DashboardError(
                404, "Action not found", "The requested action does not exist."
            ) from error
        except (InvalidActionState, RuntimeActionError) as error:
            raise DashboardError(
                409,
                "Action state changed",
                "This action can no longer accept that decision. Refresh its current state.",
            ) from error
        result = "approved" if decision is Decision.APPROVE else "rejected"
        return RedirectResponse(
            f"/dashboard/actions/{quote(action_id, safe='')}?decision={result}",
            status_code=303,
        )

    def _new_login_response(self, *, error: str | None = None, status_code: int = 200) -> Response:
        csrf = secrets.token_urlsafe(32)
        error_html = (
            f'<div class="notice error" role="alert">{html.escape(error)}</div>' if error else ""
        )
        body = f"""
        <section class="card narrow"><h1>Sign in to review actions</h1>
        <p class="muted">Use a scoped reviewer bearer credential. It is verified once and is never stored in the browser or session database.</p>
        {error_html}<form method="post" action="/dashboard/login">
        <label for="token">Reviewer credential</label><input id="token" name="token" type="password" autocomplete="current-password" required maxlength="512">
        <input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
        <p><button type="submit">Sign in</button></p></form></section>
        """
        response = self._page("Sign in", body, status_code=status_code)
        response.set_cookie(
            self.login_csrf_cookie,
            csrf,
            max_age=600,
            path=self.cookie_path,
            secure=self.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    def _session(self, request: Request) -> DashboardSession | None:
        return self.sessions.get(request.cookies.get(self.session_cookie))

    def _require_session(self, request: Request) -> DashboardSession:
        session = self._session(request)
        if session is None:
            raise DashboardError(401, "Sign-in required", "Sign in to review approval actions.")
        return session

    @staticmethod
    def _require_scope(session: DashboardSession, scope: str) -> None:
        if not session.principal.has_scope(scope):
            raise DashboardError(403, "Access denied", "Your identity cannot decide actions.")

    @staticmethod
    def _require_csrf(session: DashboardSession, presented: str | None) -> None:
        if (
            presented is None
            or _OPAQUE_TOKEN_PATTERN.fullmatch(presented) is None
            or not hmac.compare_digest(session.csrf_token, presented)
        ):
            raise DashboardError(403, "Request rejected", "The form expired or is invalid.")

    def _require_same_origin(self, request: Request) -> None:
        expected = self.public_origin or _request_origin(request)
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin is None and referer is None:
            return
        if origin == "null" and request.headers.get("sec-fetch-site") == "same-origin":
            return
        try:
            if origin is not None:
                presented = _normalize_public_origin(origin)
            else:
                presented = _origin_from_referer(cast(str, referer))
        except ValueError:
            presented = None
        if presented is None or not hmac.compare_digest(expected, presented):
            raise DashboardError(403, "Request rejected", "The request origin is not trusted.")

    def _page(
        self,
        title: str,
        body: str,
        *,
        session: DashboardSession | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        identity = ""
        if session is not None:
            identity = f"""
            <div class="actions"><span>{html.escape(session.principal.subject)}</span>
            <form class="inline" method="post" action="/dashboard/logout">
            <input type="hidden" name="csrf" value="{html.escape(session.csrf_token, quote=True)}">
            <button class="secondary" type="submit">Sign out</button></form></div>
            """
        document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{html.escape(title)} · AgentBarrier</title>
        <link rel="stylesheet" href="/dashboard/assets/style.css"></head><body>
        <a class="skip" href="#main">Skip to main content</a><header><div class="shell bar">
        <a class="brand" href="/dashboard/">AgentBarrier</a>{identity}</div></header>
        <main id="main" class="shell">{body}</main></body></html>"""
        return HTMLResponse(document, status_code=status_code)

    async def dashboard_error(self, request: Request, error: Exception) -> Response:
        dashboard_error = cast(DashboardError, error)
        session = self._session(request)
        body = (
            f'<section class="card narrow"><h1>{html.escape(dashboard_error.title)}</h1>'
            f"<p>{html.escape(dashboard_error.message)}</p>"
            '<p><a href="/dashboard/login">Sign in</a> or '
            '<a href="/dashboard/">return to the dashboard</a>.</p></section>'
        )
        return self._page(
            dashboard_error.title,
            body,
            session=session,
            status_code=dashboard_error.status_code,
        )

    async def http_error(self, request: Request, error: Exception) -> Response:
        http_error = cast(HTTPException, error)
        title = "Page not found" if http_error.status_code == 404 else "Method not allowed"
        return await self.dashboard_error(
            request,
            DashboardError(
                http_error.status_code, title, "The requested dashboard route is unavailable."
            ),
        )

    async def internal_error(self, request: Request, error: Exception) -> Response:
        del error
        return await self.dashboard_error(
            request,
            DashboardError(
                500,
                "Dashboard error",
                "The dashboard could not complete this request.",
            ),
        )


def create_dashboard_app(
    *,
    store: RuntimeStore,
    auth: StaticBearerAuth,
    sessions: DashboardSessionStore | None = None,
    cookie_secure: bool = True,
    public_origin: str | None = None,
) -> Starlette:
    """Create the server-rendered approval dashboard around trusted server-side state."""

    return ApprovalDashboard(
        store=store,
        auth=auth,
        sessions=sessions,
        cookie_secure=cookie_secure,
        public_origin=public_origin,
    ).app


async def _read_form(request: Request, *, allowed: set[str]) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise DashboardError(415, "Unsupported form", "Dashboard forms must use URL encoding.")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_FORM_BYTES:
                raise DashboardError(413, "Form too large", "The submitted form is too large.")
        except ValueError as error:
            raise DashboardError(
                400, "Invalid request", "Content-Length must be an integer."
            ) from error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_FORM_BYTES:
            raise DashboardError(413, "Form too large", "The submitted form is too large.")
    try:
        decoded = body.decode("utf-8")
        parsed = parse_qs(
            decoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise DashboardError(
            400, "Invalid form", "The submitted form is not valid UTF-8 data."
        ) from error
    unknown = set(parsed) - allowed
    if unknown or any(len(values) != 1 for values in parsed.values()):
        raise DashboardError(400, "Invalid form", "The submitted form contains unexpected fields.")
    return {key: values[0] for key, values in parsed.items()}


def _validate_reason(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not value.strip() or len(value) > 2_000:
        raise DashboardError(400, "Invalid reason", "A reason must contain 1 to 2000 characters.")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise DashboardError(400, "Invalid reason", "The reason contains invalid characters.")
    return value


def _normalize_public_origin(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("dashboard public origin is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("dashboard public origin must contain only an HTTP(S) origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}"


def _request_origin(request: Request) -> str:
    return cast(str, _normalize_public_origin(str(request.base_url).rstrip("/")))


def _origin_from_referer(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return _normalize_public_origin(origin)
    except ValueError:
        return None


def _dashboard_action_id(request: Request) -> str:
    action_id = cast(str, request.path_params["action_id"])
    if not action_id.strip() or len(action_id) > 128:
        raise DashboardError(400, "Invalid action", "The action identifier is invalid.")
    return action_id


def _reject_dashboard_query(request: Request, allowed: set[str]) -> None:
    if set(request.query_params) - allowed or any(
        len(request.query_params.getlist(name)) > 1 for name in allowed
    ):
        raise DashboardError(400, "Invalid query", "The dashboard query is not recognized.")


def _selected(value: str, expected: str) -> str:
    return " selected" if value == expected else ""


def _action_row(action: RuntimeAction) -> str:
    encoded_id = quote(action.action_id, safe="")
    return (
        "<tr>"
        f"<td>{html.escape(_timestamp(action.created_at_ns))}</td>"
        f'<td><span class="status {html.escape(action.status.value)}">{html.escape(action.status.value)}</span></td>'
        f"<td>{html.escape(action.namespace)}</td>"
        f"<td>{html.escape(action.tool_name)}</td>"
        f'<td><a class="button" href="/dashboard/actions/{encoded_id}">Review</a></td>'
        "</tr>"
    )


def _detail(label: str, value: str) -> str:
    return f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"


def _json_block(value: object) -> str:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    return f"<pre>{html.escape(rendered)}</pre>"


def _timestamp(value_ns: int) -> str:
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=timezone.utc).isoformat()

"""Scoped bearer authentication for the MCP HTTP gateway."""

from __future__ import annotations

import json
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from agentbarrier.service.auth import AuthenticationError, StaticBearerAuth

MCP_CALL_SCOPE = "mcp:call"
"""Scope required to connect to and call the MCP HTTP gateway."""


class MCPBearerAuthMiddleware:
    """Require one static scoped bearer identity before MCP HTTP processing."""

    def __init__(self, app: ASGIApp, auth: StaticBearerAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        try:
            principal = self.auth.authenticate(headers.get("authorization"))
            self.auth.require_scope(principal, MCP_CALL_SCOPE)
        except AuthenticationError as error:
            payload = json.dumps(
                {"error": {"code": error.code, "message": error.message}},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            response = Response(
                payload,
                status_code=error.status_code,
                media_type="application/json",
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "WWW-Authenticate": error.challenge,
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                },
            )
            await response(scope, receive, send)
            return

        authenticated_scope: dict[str, Any] = dict(scope)
        state = dict(scope.get("state", {}))
        state["agentbarrier_principal"] = principal
        authenticated_scope["state"] = state
        await self.app(authenticated_scope, receive, send)


__all__ = ["MCP_CALL_SCOPE", "MCPBearerAuthMiddleware"]

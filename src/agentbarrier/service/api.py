"""Scoped HTTP API for runtime action review and audit inspection."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, cast

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentbarrier import __version__
from agentbarrier.errors import ApprovalAuthorizationError, InvalidActionState, RuntimeActionError
from agentbarrier.models import Decision
from agentbarrier.runtime import RuntimeStatus, RuntimeStore
from agentbarrier.runtime.serialization import action_payload, receipt_payload
from agentbarrier.service.auth import AuthenticationError, Principal, StaticBearerAuth

_MAX_BODY_BYTES = 16 * 1024
_MAX_PAGE_SIZE = 100
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ServiceError(Exception):
    """Safe HTTP error rendered through one JSON envelope."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


class SecurityHeadersMiddleware:
    """Attach a safe request identifier and non-browser API security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

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
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = "default-src 'none'"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ApprovalAPI:
    """HTTP application exposing scoped, identity-bound approval operations."""

    def __init__(self, *, store: RuntimeStore, auth: StaticBearerAuth) -> None:
        self.store = store
        self.auth = auth
        self.openapi = _openapi_document()
        self.app = Starlette(
            debug=False,
            routes=[
                Route("/health/ready", self.readiness, methods=["GET"]),
                Route("/openapi.json", self.openapi_json, methods=["GET"]),
                Route("/v1/actions", self.list_actions, methods=["GET"]),
                Route("/v1/actions/{action_id:str}", self.get_action, methods=["GET"]),
                Route(
                    "/v1/actions/{action_id:str}/approve",
                    self.approve_action,
                    methods=["POST"],
                ),
                Route(
                    "/v1/actions/{action_id:str}/reject",
                    self.reject_action,
                    methods=["POST"],
                ),
                Route("/v1/audit", self.list_audit, methods=["GET"]),
            ],
            exception_handlers={
                ServiceError: _service_error_handler,
                HTTPException: _http_error_handler,
                Exception: _internal_error_handler,
            },
        )
        self.app.add_middleware(SecurityHeadersMiddleware)

    async def readiness(self, request: Request) -> Response:
        del request
        return JSONResponse(
            {
                "status": "ready",
                "schema_version": self.store.schema_version,
                "version": __version__,
            }
        )

    async def openapi_json(self, request: Request) -> Response:
        del request
        return JSONResponse(self.openapi)

    async def list_actions(self, request: Request) -> Response:
        principal = self._authorize(request, "actions:read")
        _reject_unknown_query(request, {"status", "limit", "after"})
        raw_status = _single_query_value(request, "status")
        try:
            status = RuntimeStatus(raw_status) if raw_status is not None else None
        except ValueError as error:
            raise ServiceError(
                status_code=400,
                code="invalid_status",
                message="status is not a recognized runtime action state",
            ) from error
        limit = _parse_limit(request)
        actions = [
            action
            for action in self.store.list_actions(status=status)
            if principal.can_access_action(action)
        ]
        after = _single_query_value(request, "after")
        if after is not None:
            for index, action in enumerate(actions):
                if action.action_id == after:
                    actions = actions[index + 1 :]
                    break
            else:
                raise ServiceError(
                    status_code=400,
                    code="invalid_cursor",
                    message="after does not identify an action in the selected result set",
                )
        page = actions[:limit]
        next_cursor = page[-1].action_id if len(actions) > limit and page else None
        return JSONResponse(
            {
                "data": [action_payload(action) for action in page],
                "next_cursor": next_cursor,
            }
        )

    async def get_action(self, request: Request) -> Response:
        principal = self._authorize(request, "actions:read")
        _reject_unknown_query(request, set())
        action_id = _path_action_id(request)
        try:
            action = self.store.get_action(action_id)
        except KeyError as error:
            raise _not_found(action_id) from error
        if not principal.can_access_action(action):
            raise _not_found(action_id)
        return JSONResponse({"data": action_payload(action)})

    async def approve_action(self, request: Request) -> Response:
        return await self._decide(request, Decision.APPROVE)

    async def reject_action(self, request: Request) -> Response:
        return await self._decide(request, Decision.REJECT)

    async def _decide(self, request: Request, decision: Decision) -> Response:
        principal = self._authorize(request, "actions:decide")
        _reject_unknown_query(request, set())
        action_id = _path_action_id(request)
        reason = await _read_decision_reason(request)
        try:
            if principal.organization_id is None:
                action = self.store.decide(
                    action_id,
                    decision,
                    decided_by=principal.subject,
                    reason=reason,
                )
            else:
                action = self.store.decide_authorized(
                    action_id,
                    decision,
                    authorization=principal.decision_authorization(),
                    reason=reason,
                )
        except KeyError as error:
            raise _not_found(action_id) from error
        except ApprovalAuthorizationError as error:
            if error.code in {"organization_mismatch", "namespace_forbidden"}:
                raise _not_found(action_id) from error
            raise ServiceError(
                status_code=403,
                code=error.code,
                message=str(error),
            ) from error
        except (InvalidActionState, RuntimeActionError) as error:
            raise ServiceError(
                status_code=409,
                code="action_state_conflict",
                message="the action cannot accept that decision in its current state",
            ) from error
        return JSONResponse({"data": action_payload(action)})

    async def list_audit(self, request: Request) -> Response:
        principal = self._authorize(request, "audit:read")
        _reject_unknown_query(request, {"action_id", "after_sequence", "limit"})
        action_id = _single_query_value(request, "action_id")
        if action_id is not None and (not action_id.strip() or len(action_id) > 128):
            raise ServiceError(
                status_code=400,
                code="invalid_action_id",
                message="action_id must contain 1 to 128 characters",
            )
        after_sequence = _parse_nonnegative_integer(request, "after_sequence", default=0)
        limit = _parse_limit(request)
        visible_action_ids = {
            action.action_id
            for action in self.store.list_actions()
            if principal.can_access_action(action)
        }
        if action_id is not None and action_id not in visible_action_ids:
            raise _not_found(action_id)
        receipts = [
            receipt
            for receipt in self.store.receipts(action_id=action_id)
            if receipt.sequence > after_sequence and receipt.action_id in visible_action_ids
        ]
        page = receipts[:limit]
        next_sequence = page[-1].sequence if len(receipts) > limit and page else None
        return JSONResponse(
            {
                "data": [receipt_payload(receipt) for receipt in page],
                "next_sequence": next_sequence,
                "chain_valid": self.store.verify_receipt_chain(),
            }
        )

    def _authorize(self, request: Request, scope: str) -> Principal:
        try:
            principal = self.auth.authenticate(request.headers.get("authorization"))
            self.auth.require_scope(principal, scope)
            return principal
        except AuthenticationError as error:
            raise ServiceError(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
                headers={"WWW-Authenticate": error.challenge},
            ) from error


def create_approval_app(*, store: RuntimeStore, auth: StaticBearerAuth) -> Starlette:
    """Create a Starlette approval API around one already-open runtime store."""

    return ApprovalAPI(store=store, auth=auth).app


async def _read_decision_reason(request: Request) -> str | None:
    body = await _read_limited_body(request)
    if not body:
        return None
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise ServiceError(
            status_code=415,
            code="unsupported_media_type",
            message="decision bodies must use application/json",
        )
    try:
        value: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(
            status_code=400,
            code="invalid_json",
            message="request body is not valid UTF-8 JSON",
        ) from error
    if not isinstance(value, Mapping):
        raise ServiceError(
            status_code=400,
            code="invalid_body",
            message="decision body must be a JSON object",
        )
    data = cast(Mapping[str, object], value)
    unknown = sorted(key for key in data if key != "reason")
    if unknown:
        raise ServiceError(
            status_code=400,
            code="unknown_body_fields",
            message=f"unknown decision body fields: {', '.join(unknown)}",
        )
    reason = data.get("reason")
    if reason is None:
        return None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ServiceError(
            status_code=400,
            code="invalid_reason",
            message="reason must contain 1 to 2000 characters when provided",
        )
    if any(ord(character) < 32 and character not in "\t\n\r" for character in reason):
        raise ServiceError(
            status_code=400,
            code="invalid_reason",
            message="reason contains unsupported control characters",
        )
    return reason


async def _read_limited_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise _body_too_large()
        except ValueError as error:
            raise ServiceError(
                status_code=400,
                code="invalid_content_length",
                message="Content-Length must be an integer",
            ) from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise _body_too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _body_too_large() -> ServiceError:
    return ServiceError(
        status_code=413,
        code="body_too_large",
        message=f"request body must not exceed {_MAX_BODY_BYTES} bytes",
    )


def _path_action_id(request: Request) -> str:
    action_id = cast(str, request.path_params["action_id"])
    if not action_id.strip() or len(action_id) > 128:
        raise ServiceError(
            status_code=400,
            code="invalid_action_id",
            message="action_id must contain 1 to 128 characters",
        )
    return action_id


def _not_found(action_id: str) -> ServiceError:
    del action_id
    return ServiceError(
        status_code=404,
        code="action_not_found",
        message="the requested runtime action does not exist",
    )


def _reject_unknown_query(request: Request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise ServiceError(
            status_code=400,
            code="unknown_query_parameters",
            message=f"unknown query parameters: {', '.join(unknown)}",
        )


def _single_query_value(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise ServiceError(
            status_code=400,
            code="duplicate_query_parameter",
            message=f"query parameter {name!r} must not be repeated",
        )
    return values[0] if values else None


def _parse_limit(request: Request) -> int:
    return _parse_nonnegative_integer(
        request,
        "limit",
        default=50,
        minimum=1,
        maximum=_MAX_PAGE_SIZE,
    )


def _parse_nonnegative_integer(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = _single_query_value(request, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ServiceError(
            status_code=400,
            code=f"invalid_{name}",
            message=f"{name} must be an integer",
        ) from error
    if value < minimum or (maximum is not None and value > maximum):
        range_message = (
            f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        )
        raise ServiceError(
            status_code=400,
            code=f"invalid_{name}",
            message=f"{name} must be {range_message}",
        )
    return value


async def _service_error_handler(request: Request, error: Exception) -> Response:
    service_error = cast(ServiceError, error)
    return _error_response(
        request,
        status_code=service_error.status_code,
        code=service_error.code,
        message=service_error.message,
        headers=service_error.headers,
    )


async def _http_error_handler(request: Request, error: Exception) -> Response:
    http_error = cast(HTTPException, error)
    code = "route_not_found" if http_error.status_code == 404 else "method_not_allowed"
    message = (
        "the requested route does not exist"
        if http_error.status_code == 404
        else "the HTTP method is not allowed"
    )
    return _error_response(
        request,
        status_code=http_error.status_code,
        code=code,
        message=message,
        headers=http_error.headers,
    )


async def _internal_error_handler(request: Request, error: Exception) -> Response:
    del error
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message="the approval service could not complete the request",
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": cast(str, request.state.request_id),
            }
        },
        status_code=status_code,
        headers=dict(headers or {}),
    )


def _openapi_document() -> dict[str, Any]:
    bearer_security: list[dict[str, list[str]]] = [{"bearerAuth": []}]
    action_response = {
        "200": {"description": "Runtime action"},
        "401": {"description": "Missing or invalid bearer token"},
        "403": {"description": "Insufficient scope"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "AgentBarrier Approval API",
            "version": __version__,
            "description": "Scoped review and audit operations for exact AI-agent tool calls.",
        },
        "paths": {
            "/health/ready": {
                "get": {
                    "operationId": "readiness",
                    "responses": {"200": {"description": "Service is ready"}},
                }
            },
            "/openapi.json": {
                "get": {
                    "operationId": "openapiDocument",
                    "responses": {"200": {"description": "OpenAPI document"}},
                }
            },
            "/v1/actions": {
                "get": {
                    "operationId": "listActions",
                    "security": bearer_security,
                    "x-agentbarrier-scope": "actions:read",
                    "responses": action_response,
                }
            },
            "/v1/actions/{action_id}": {
                "get": {
                    "operationId": "getAction",
                    "security": bearer_security,
                    "x-agentbarrier-scope": "actions:read",
                    "parameters": [_action_id_parameter()],
                    "responses": action_response | {"404": {"description": "Action not found"}},
                }
            },
            "/v1/actions/{action_id}/approve": {
                "post": _decision_operation("approveAction", "Approve an exact pending action")
            },
            "/v1/actions/{action_id}/reject": {
                "post": _decision_operation("rejectAction", "Reject an exact pending action")
            },
            "/v1/audit": {
                "get": {
                    "operationId": "listAuditReceipts",
                    "security": bearer_security,
                    "x-agentbarrier-scope": "audit:read",
                    "responses": action_response,
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": {
                "DecisionRequest": {
                    "type": "object",
                    "properties": {"reason": {"type": "string", "maxLength": 2000}},
                    "additionalProperties": False,
                }
            },
        },
    }


def _action_id_parameter() -> dict[str, Any]:
    return {
        "name": "action_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
    }


def _decision_operation(operation_id: str, summary: str) -> dict[str, Any]:
    return {
        "operationId": operation_id,
        "summary": summary,
        "security": [{"bearerAuth": []}],
        "x-agentbarrier-scope": "actions:decide",
        "parameters": [_action_id_parameter()],
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/DecisionRequest"}}
            },
        },
        "responses": {
            "200": {"description": "Decision recorded or replayed idempotently"},
            "401": {"description": "Missing or invalid bearer token"},
            "403": {"description": "Insufficient scope"},
            "404": {"description": "Action not found"},
            "409": {"description": "Action state conflict"},
        },
    }

"""Protected Python function execution at the real effect boundary."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from functools import wraps
from types import MappingProxyType
from typing import ParamSpec, TypeVar, cast

from agentbarrier.errors import ActionOutcomeUnknown
from agentbarrier.models import JsonValue
from agentbarrier.runtime.models import (
    ClaimOutcome,
    RuntimeAction,
    RuntimeRequest,
    RuntimeStatus,
    canonical_json,
)
from agentbarrier.runtime.policy import RuntimePolicy
from agentbarrier.runtime.protocol import RuntimeStore

P = ParamSpec("P")
R = TypeVar("R")
IdempotencySelector = str | Callable[[Mapping[str, JsonValue]], str]


class RuntimeBarrier:
    """Evaluate policy and enforce durable execution around agent tool calls."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        store: RuntimeStore,
        namespace: str = "default",
        organization_id: str = "default",
        requested_by: str | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not namespace.strip():
            raise ValueError("namespace must not be empty")
        if not organization_id.strip():
            raise ValueError("organization_id must not be empty")
        if requested_by is not None and not requested_by.strip():
            raise ValueError("requested_by must not be empty when provided")
        self.policy = policy
        self.store = store
        self.namespace = namespace
        self.organization_id = organization_id
        self.requested_by = requested_by
        self._clock_ns = clock_ns

    def protect(
        self,
        function: Callable[P, R],
        *,
        tool_name: str,
        idempotency_key: IdempotencySelector,
    ) -> Callable[P, R]:
        """Wrap a JSON-compatible sync or async tool with runtime enforcement."""

        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            raise TypeError("generator functions are not supported by the runtime barrier")
        signature = inspect.signature(function)

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                arguments = self._bind_arguments(
                    signature=signature,
                    args=args,
                    kwargs=kwargs,
                )
                key = self._select_idempotency_key(idempotency_key, arguments)

                async def operation() -> object:
                    return await cast(Awaitable[object], function(*args, **kwargs))

                return await self.execute_async(
                    tool_name=tool_name,
                    arguments=arguments,
                    idempotency_key=key,
                    operation=operation,
                )

            return cast(Callable[P, R], async_wrapper)

        @wraps(function)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            arguments = self._bind_arguments(
                signature=signature,
                args=args,
                kwargs=kwargs,
            )
            key = self._select_idempotency_key(idempotency_key, arguments)
            return self.execute(
                tool_name=tool_name,
                arguments=arguments,
                idempotency_key=key,
                operation=lambda: function(*args, **kwargs),
            )

        return sync_wrapper

    def execute(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
        operation: Callable[[], R],
    ) -> R:
        """Enforce policy around one dynamically identified synchronous tool call.

        This transport-neutral boundary is intended for MCP proxies, framework adapters, and
        application dispatchers that receive a tool name and JSON arguments at runtime. The
        operation is called only after policy and approval checks pass, and at most once for the
        exact namespace, tool name, idempotency key, arguments, and policy version.
        """

        if not callable(operation):
            raise TypeError("operation must be callable")
        request, action = self._prepare_request(
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )
        claim = self.store.claim(action.action_id, request_digest=request.request_digest)
        if claim.outcome is ClaimOutcome.REPLAY:
            return cast(R, claim.result)
        try:
            result = operation()
        except BaseException as exc:
            self._record_unknown(claim.action, request, exc)
            raise
        self._complete(claim.action, request, result)
        return result

    async def execute_async(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
        operation: Callable[[], Awaitable[R]],
    ) -> R:
        """Enforce policy around one dynamically identified asynchronous tool call."""

        if not callable(operation):
            raise TypeError("operation must be callable")
        request, action = self._prepare_request(
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )
        claim = self.store.claim(action.action_id, request_digest=request.request_digest)
        if claim.outcome is ClaimOutcome.REPLAY:
            return cast(R, claim.result)
        try:
            result = await operation()
        except BaseException as exc:
            self._record_unknown(claim.action, request, exc)
            raise
        self._complete(claim.action, request, result)
        return result

    @staticmethod
    def _bind_arguments(
        *,
        signature: inspect.Signature,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> Mapping[str, JsonValue]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        encoded = canonical_json(
            cast(Mapping[str, JsonValue], dict(bound.arguments)),
            path="arguments",
        )
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("bound arguments did not encode as an object")
        return MappingProxyType(cast(dict[str, JsonValue], decoded))

    def _prepare_request(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> tuple[RuntimeRequest, RuntimeAction]:
        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if not isinstance(idempotency_key, str):
            raise TypeError("idempotency_key must be a string")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        encoded = canonical_json(dict(arguments), path="arguments")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("arguments did not encode as an object")
        normalized_arguments = MappingProxyType(cast(dict[str, JsonValue], decoded))
        request = RuntimeRequest(
            action_id=str(uuid.uuid4()),
            namespace=self.namespace,
            tool_name=tool_name,
            arguments=normalized_arguments,
            idempotency_key=idempotency_key,
            policy_version=self.policy.version,
            created_at_ns=self._clock_ns(),
            organization_id=self.organization_id,
            requested_by=self.requested_by,
        )
        decision = self.policy.evaluate(tool_name, request.arguments)
        return request, self.store.submit(request, decision)

    @staticmethod
    def _select_idempotency_key(
        selector: IdempotencySelector,
        arguments: Mapping[str, JsonValue],
    ) -> str:
        if isinstance(selector, str):
            if selector not in arguments:
                raise ValueError(f"idempotency argument {selector!r} was not bound")
            raw_key = arguments[selector]
            if not isinstance(raw_key, str):
                raise TypeError(f"idempotency argument {selector!r} must be a string")
            key = raw_key
        else:
            key = selector(arguments)
            if not isinstance(key, str):
                raise TypeError("idempotency selector must return a string")
        if not key.strip():
            raise ValueError("idempotency key must not be empty")
        return key

    def _complete(
        self,
        action: RuntimeAction,
        request: RuntimeRequest,
        result: object,
    ) -> None:
        try:
            encoded = canonical_json(cast(JsonValue, result), path="result")
            normalized = cast(JsonValue, json.loads(encoded))
            self.store.complete(
                action.action_id,
                request_digest=request.request_digest,
                result=normalized,
            )
        except BaseException as exc:
            unknown = self._record_unknown(action, request, exc)
            raise ActionOutcomeUnknown(unknown) from exc

    def _record_unknown(
        self,
        action: RuntimeAction,
        request: RuntimeRequest,
        error: BaseException,
    ) -> RuntimeAction:
        detail = type(error).__name__
        try:
            return self.store.mark_unknown(
                action.action_id,
                request_digest=request.request_digest,
                error=detail,
            )
        except BaseException:
            return replace(action, status=RuntimeStatus.UNKNOWN, error=detail)

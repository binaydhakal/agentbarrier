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
from agentbarrier.runtime.store import SQLiteRuntimeStore

P = ParamSpec("P")
R = TypeVar("R")
IdempotencySelector = str | Callable[[Mapping[str, JsonValue]], str]


class RuntimeBarrier:
    """Evaluate policy and enforce durable execution around Python tools."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        store: SQLiteRuntimeStore,
        namespace: str = "default",
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not namespace.strip():
            raise ValueError("namespace must not be empty")
        self.policy = policy
        self.store = store
        self.namespace = namespace
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
        signature = inspect.signature(function)

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                request, action = self._prepare(
                    signature=signature,
                    args=args,
                    kwargs=kwargs,
                    tool_name=tool_name,
                    idempotency_selector=idempotency_key,
                )
                claim = self.store.claim(action.action_id, request_digest=request.request_digest)
                if claim.outcome is ClaimOutcome.REPLAY:
                    return claim.result
                try:
                    awaitable = cast(Awaitable[object], function(*args, **kwargs))
                    result = await awaitable
                except BaseException as exc:
                    self._record_unknown(claim.action, request, exc)
                    raise
                self._complete(claim.action, request, result)
                return result

            return cast(Callable[P, R], async_wrapper)

        @wraps(function)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            request, action = self._prepare(
                signature=signature,
                args=args,
                kwargs=kwargs,
                tool_name=tool_name,
                idempotency_selector=idempotency_key,
            )
            claim = self.store.claim(action.action_id, request_digest=request.request_digest)
            if claim.outcome is ClaimOutcome.REPLAY:
                return cast(R, claim.result)
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                self._record_unknown(claim.action, request, exc)
                raise
            self._complete(claim.action, request, result)
            return result

        return sync_wrapper

    def _prepare(
        self,
        *,
        signature: inspect.Signature,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
        tool_name: str,
        idempotency_selector: IdempotencySelector,
    ) -> tuple[RuntimeRequest, RuntimeAction]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        encoded = canonical_json(
            cast(Mapping[str, JsonValue], dict(bound.arguments)),
            path="arguments",
        )
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("bound arguments did not encode as an object")
        arguments = MappingProxyType(cast(dict[str, JsonValue], decoded))
        key = self._select_idempotency_key(idempotency_selector, arguments)
        request = RuntimeRequest(
            action_id=str(uuid.uuid4()),
            namespace=self.namespace,
            tool_name=tool_name,
            arguments=arguments,
            idempotency_key=key,
            policy_version=self.policy.version,
            created_at_ns=self._clock_ns(),
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

"""OpenAI Agents function tools protected by the AgentBarrier runtime."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, get_type_hints

from agentbarrier.models import JsonValue
from agentbarrier.runtime import IdempotencySelector, RuntimeBarrier
from agentbarrier.runtime.models import canonical_json

if TYPE_CHECKING:
    from agents import FunctionTool

P = ParamSpec("P")
R = TypeVar("R")


def runtime_function_tool(
    function: Callable[P, R],
    *,
    barrier: RuntimeBarrier,
    idempotency_key: IdempotencySelector,
    **function_tool_options: Any,
) -> FunctionTool:
    """Build an OpenAI ``FunctionTool`` with durable runtime enforcement.

    The returned object is a normal SDK function tool. AgentBarrier evaluates the exact parsed
    function arguments immediately before the original callable executes. SDK-injected run context
    is deliberately excluded from the policy request.

    ``failure_error_function`` must remain ``None`` so approval, denial, and unknown-outcome
    exceptions reach the application instead of being converted into model-visible tool output.
    Native SDK ``needs_approval`` is also disabled to avoid two independent approval authorities.
    """

    try:
        from agents import RunContextWrapper, function_tool
    except ImportError as error:  # pragma: no cover - exercised in a clean optional install
        raise ImportError("OpenAI Agents integration requires 'agentbarrier[openai]'") from error

    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        raise TypeError("OpenAI Agents runtime tool requires a plain function or bound method")
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("generator functions are not supported by the OpenAI runtime boundary")
    options = dict(function_tool_options)
    if options.get("needs_approval", False) is not False:
        raise ValueError(
            "OpenAI Agents native needs_approval cannot be combined with AgentBarrier runtime "
            "approval"
        )
    if options.get("failure_error_function") is not None:
        raise ValueError(
            "OpenAI Agents failure_error_function must be None so runtime decisions fail closed"
        )
    if options.get("timeout_behavior", "raise_exception") != "raise_exception":
        raise ValueError(
            "OpenAI Agents timeout_behavior must be 'raise_exception' so unknown outcomes "
            "cannot become model-visible success"
        )
    options["needs_approval"] = False
    options["failure_error_function"] = None
    options["timeout_behavior"] = "raise_exception"

    exposed_name = options.get("name_override") or getattr(function, "__name__", None)
    if not isinstance(exposed_name, str) or not exposed_name.strip():
        raise ValueError("OpenAI Agents runtime tool must have a non-empty name")
    signature = inspect.signature(function)

    def policy_arguments(
        args: tuple[object, ...], kwargs: Mapping[str, object]
    ) -> Mapping[str, JsonValue]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
        first_parameter = next(iter(signature.parameters), None)
        if first_parameter is not None and isinstance(
            values.get(first_parameter), RunContextWrapper
        ):
            del values[first_parameter]
        encoded = canonical_json(cast(Mapping[str, JsonValue], values), path="arguments")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("OpenAI Agents tool arguments did not encode as an object")
        return MappingProxyType(cast(dict[str, JsonValue], decoded))

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def async_protected(*args: P.args, **kwargs: P.kwargs) -> Any:
            arguments = policy_arguments(cast(tuple[object, ...], args), kwargs)
            key = _select_idempotency_key(idempotency_key, arguments)

            async def operation() -> Any:
                return await cast(Any, function)(*args, **kwargs)

            return await barrier.execute_async(
                tool_name=exposed_name,
                arguments=arguments,
                idempotency_key=key,
                operation=operation,
            )

        protected: Callable[..., Any] = async_protected
    else:

        @wraps(function)
        def sync_protected(*args: P.args, **kwargs: P.kwargs) -> Any:
            arguments = policy_arguments(cast(tuple[object, ...], args), kwargs)
            key = _select_idempotency_key(idempotency_key, arguments)
            return barrier.execute(
                tool_name=exposed_name,
                arguments=arguments,
                idempotency_key=key,
                operation=lambda: function(*args, **kwargs),
            )

        protected = sync_protected

    protected.__annotations__ = get_type_hints(function, include_extras=True)
    return function_tool(protected, **options)


def _select_idempotency_key(
    selector: IdempotencySelector,
    arguments: Mapping[str, JsonValue],
) -> str:
    if isinstance(selector, str):
        if selector not in arguments:
            raise ValueError(f"idempotency argument {selector!r} was not bound")
        value = arguments[selector]
        if not isinstance(value, str):
            raise TypeError(f"idempotency argument {selector!r} must be a string")
        key = value
    else:
        key = selector(arguments)
        if not isinstance(key, str):
            raise TypeError("idempotency selector must return a string")
    if not key.strip():
        raise ValueError("idempotency key must not be empty")
    return key


__all__ = ["runtime_function_tool"]

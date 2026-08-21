"""Google Agent Development Kit tools protected by the AgentBarrier runtime."""

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
    from google.adk.tools.function_tool import FunctionTool

P = ParamSpec("P")
R = TypeVar("R")


def runtime_function_tool(
    function: Callable[P, R],
    *,
    barrier: RuntimeBarrier,
    idempotency_key: IdempotencySelector,
    name: str | None = None,
    description: str | None = None,
    require_confirmation: bool = False,
) -> FunctionTool:
    """Build a Google ADK ``FunctionTool`` with durable runtime enforcement.

    The original callable must be async so host cancellation can reach the effect boundary.
    ADK-injected ``ToolContext`` is available to the callable but excluded from policy arguments.
    AgentBarrier owns approval, so ADK's independent native confirmation mechanism is disabled.
    """

    try:
        from google.adk.tools.function_tool import FunctionTool
    except ImportError as error:  # pragma: no cover - exercised in a clean optional install
        raise ImportError("Google ADK integration requires 'agentbarrier[google-adk]'") from error

    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        raise TypeError("Google ADK runtime tool requires a plain function or bound method")
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("generator functions are not supported by the Google ADK runtime boundary")
    if not inspect.iscoroutinefunction(function):
        raise TypeError(
            "Google ADK runtime tools must be async so host cancellation reaches the "
            "effect boundary"
        )
    if require_confirmation is not False:
        raise ValueError(
            "Google ADK native require_confirmation cannot be combined with AgentBarrier "
            "runtime approval"
        )

    exposed_name = getattr(function, "__name__", None) if name is None else name
    if not isinstance(exposed_name, str) or not exposed_name.strip():
        raise ValueError("Google ADK runtime tool must have a non-empty name")
    if description is not None and not isinstance(description, str):
        raise TypeError("Google ADK runtime tool description must be a string")

    signature = inspect.signature(function)
    unsupported_parameters = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }
    ]
    if unsupported_parameters:
        joined = ", ".join(unsupported_parameters)
        raise TypeError(f"Google ADK runtime tool has unsupported parameters: {joined}")
    if "input_stream" in signature.parameters:
        raise TypeError(
            "Google ADK streaming input tools are not supported by the runtime boundary"
        )

    public_argument_names: frozenset[str] | None = None

    def policy_arguments(
        args: tuple[object, ...], kwargs: Mapping[str, object]
    ) -> Mapping[str, JsonValue]:
        if public_argument_names is None:  # pragma: no cover - assigned before tool is returned
            raise RuntimeError("Google ADK tool schema was not initialized")
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        visible = {
            parameter_name: value
            for parameter_name, value in bound.arguments.items()
            if parameter_name in public_argument_names
        }
        encoded = canonical_json(cast(Mapping[str, JsonValue], visible), path="arguments")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("Google ADK tool arguments did not encode as an object")
        return MappingProxyType(cast(dict[str, JsonValue], decoded))

    @wraps(function)
    async def protected(*args: P.args, **kwargs: P.kwargs) -> Any:
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

    protected.__name__ = exposed_name
    if description is not None:
        protected.__doc__ = description
    protected.__annotations__ = get_type_hints(function, include_extras=True)
    tool = FunctionTool(protected, require_confirmation=False)

    context_parameter = cast(str | None, getattr(tool, "_context_param_name", None))
    public_argument_names = frozenset(
        parameter_name
        for parameter_name in signature.parameters
        if parameter_name != context_parameter
    )
    return tool


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

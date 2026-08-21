"""PydanticAI tools protected by the AgentBarrier runtime."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, get_type_hints

from agentbarrier.errors import FrameworkControlSignalError
from agentbarrier.models import JsonValue
from agentbarrier.runtime import IdempotencySelector, RuntimeBarrier
from agentbarrier.runtime.models import canonical_json

if TYPE_CHECKING:
    from pydantic_ai import Tool

P = ParamSpec("P")
R = TypeVar("R")


def runtime_tool(
    function: Callable[P, R],
    *,
    barrier: RuntimeBarrier,
    idempotency_key: IdempotencySelector,
    **tool_options: Any,
) -> Tool[Any]:
    """Build a PydanticAI ``Tool`` with durable runtime enforcement.

    The integration accepts async functions only because a cancelled synchronous function may keep
    running in PydanticAI's worker thread. ``RunContext`` remains available to the callable but is
    excluded from the exact policy arguments.
    """

    try:
        from pydantic_ai import ApprovalRequired as PydanticApprovalRequired
        from pydantic_ai import (
            CallDeferred,
            ModelRetry,
            Tool,
            ToolFailed,
        )
    except ImportError as error:  # pragma: no cover - exercised in a clean optional install
        raise ImportError("PydanticAI integration requires 'agentbarrier[pydantic-ai]'") from error

    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        raise TypeError("PydanticAI runtime tool requires a plain function or bound method")
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("generator functions are not supported by the PydanticAI runtime boundary")
    if not inspect.iscoroutinefunction(function):
        raise TypeError(
            "PydanticAI runtime tools must be async so host cancellation reaches the "
            "effect boundary"
        )

    options = dict(tool_options)
    if options.get("requires_approval", False) is not False:
        raise ValueError(
            "PydanticAI native requires_approval cannot be combined with AgentBarrier runtime "
            "approval"
        )
    if options.get("timeout") is not None:
        raise ValueError(
            "PydanticAI tool timeout must be None so unknown outcomes cannot become model retries"
        )
    if options.get("max_retries") not in (None, 0):
        raise ValueError("PydanticAI max_retries must be 0 for protected consequential tools")
    if options.get("prepare") is not None:
        raise ValueError(
            "PydanticAI prepare is not supported because it can rename the policy-bound tool"
        )
    if options.get("function_schema") is not None:
        raise ValueError(
            "PydanticAI custom function_schema is not supported at the runtime boundary"
        )
    if options.get("takes_ctx") is not None:
        raise ValueError(
            "PydanticAI takes_ctx must be inferred from the original RunContext annotation"
        )
    if "schema_generator" in options:
        raise ValueError(
            "PydanticAI custom schema_generator is not supported at the runtime boundary"
        )
    options["requires_approval"] = False
    options["timeout"] = None
    options["max_retries"] = 0

    exposed_name = options.get("name") or getattr(function, "__name__", None)
    if not isinstance(exposed_name, str) or not exposed_name.strip():
        raise ValueError("PydanticAI runtime tool must have a non-empty name")
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
        raise TypeError(f"PydanticAI runtime tool has unsupported parameters: {joined}")
    public_argument_names: frozenset[str] | None = None

    def policy_arguments(
        args: tuple[object, ...], kwargs: Mapping[str, object]
    ) -> Mapping[str, JsonValue]:
        if public_argument_names is None:  # pragma: no cover - assigned before tool is returned
            raise RuntimeError("PydanticAI tool schema was not initialized")
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        visible = {
            name: value for name, value in bound.arguments.items() if name in public_argument_names
        }
        encoded = canonical_json(cast(Mapping[str, JsonValue], visible), path="arguments")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("PydanticAI tool arguments did not encode as an object")
        return MappingProxyType(cast(dict[str, JsonValue], decoded))

    control_signals = (PydanticApprovalRequired, CallDeferred, ModelRetry, ToolFailed)

    @wraps(function)
    async def protected(*args: P.args, **kwargs: P.kwargs) -> Any:
        arguments = policy_arguments(cast(tuple[object, ...], args), kwargs)
        key = _select_idempotency_key(idempotency_key, arguments)

        async def operation() -> Any:
            try:
                return await cast(Any, function)(*args, **kwargs)
            except control_signals as error:
                raise FrameworkControlSignalError("PydanticAI", type(error).__name__) from error

        return await barrier.execute_async(
            tool_name=exposed_name,
            arguments=arguments,
            idempotency_key=key,
            operation=operation,
        )

    protected.__annotations__ = get_type_hints(function, include_extras=True)
    tool = Tool(protected, **options)
    schema_properties = tool.function_schema.json_schema.get("properties")
    if not isinstance(schema_properties, dict):
        raise ValueError("PydanticAI runtime tool requires an object parameter schema")
    public_argument_names = frozenset(schema_properties)
    expected_names = list(signature.parameters)
    if tool.takes_ctx:
        expected_names = expected_names[1:]
    if public_argument_names != frozenset(expected_names):
        raise ValueError(
            "PydanticAI runtime tool requires one schema property per original tool argument"
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


__all__ = ["runtime_tool"]

"""LangGraph tools protected by the AgentBarrier runtime."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, get_type_hints

from agentbarrier.models import JsonValue
from agentbarrier.runtime import IdempotencySelector, RuntimeBarrier
from agentbarrier.runtime.models import canonical_json

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool, StructuredTool
    from langgraph.prebuilt import ToolNode

P = ParamSpec("P")
R = TypeVar("R")


def runtime_structured_tool(
    function: Callable[P, R],
    *,
    barrier: RuntimeBarrier,
    idempotency_key: IdempotencySelector,
    **structured_tool_options: Any,
) -> StructuredTool:
    """Build a LangGraph-compatible tool with durable runtime enforcement.

    LangGraph-injected arguments such as ``ToolRuntime`` remain available to the original
    callable but are excluded from the policy request. The returned ``StructuredTool`` must be
    executed by a node that propagates execution errors, such as :func:`runtime_tool_node`.
    """

    try:
        from langchain_core.tools import StructuredTool
    except ImportError as error:  # pragma: no cover - exercised in a clean optional install
        raise ImportError("LangGraph integration requires 'agentbarrier[langgraph]'") from error

    if not (inspect.isfunction(function) or inspect.ismethod(function)):
        raise TypeError("LangGraph runtime tool requires a plain function or bound method")
    if inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise TypeError("generator functions are not supported by the LangGraph runtime boundary")

    options = dict(structured_tool_options)
    if options.get("args_schema") is not None or options.get("infer_schema", True) is not True:
        raise ValueError(
            "LangGraph runtime tools require inferred schemas so policy sees every model argument"
        )
    if options.get("handle_tool_error", False) is not False:
        raise ValueError(
            "LangGraph handle_tool_error must be False so unknown outcomes fail closed"
        )
    if options.get("response_format", "content") != "content":
        raise ValueError(
            "LangGraph response_format must be 'content' so the durable result is JSON-compatible"
        )
    if "func" in options or "coroutine" in options:
        raise ValueError("func and coroutine are owned by the LangGraph runtime integration")
    options["handle_tool_error"] = False
    options["response_format"] = "content"

    exposed_name = options.get("name") or getattr(function, "__name__", None)
    if not isinstance(exposed_name, str) or not exposed_name.strip():
        raise ValueError("LangGraph runtime tool must have a non-empty name")
    signature = inspect.signature(function)
    public_argument_names: frozenset[str] | None = None

    def policy_arguments(
        args: tuple[object, ...], kwargs: Mapping[str, object]
    ) -> Mapping[str, JsonValue]:
        if public_argument_names is None:  # pragma: no cover - assigned before tool is returned
            raise RuntimeError("LangGraph tool schema was not initialized")
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        visible = {
            name: value for name, value in bound.arguments.items() if name in public_argument_names
        }
        encoded = canonical_json(cast(Mapping[str, JsonValue], visible), path="arguments")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # pragma: no cover - encoded value is a mapping
            raise RuntimeError("LangGraph tool arguments did not encode as an object")
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

        async_protected.__annotations__ = get_type_hints(function, include_extras=True)
        tool = StructuredTool.from_function(coroutine=async_protected, **options)
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

        sync_protected.__annotations__ = get_type_hints(function, include_extras=True)
        tool = StructuredTool.from_function(func=sync_protected, **options)

    public_argument_names = frozenset(tool.args)
    return tool


def runtime_tool_node(
    tools: Sequence[BaseTool | Callable[..., object]],
    *,
    name: str = "tools",
    tags: list[str] | None = None,
    messages_key: str = "messages",
) -> ToolNode:
    """Build a fail-closed ``ToolNode`` for protected consequential tools.

    Error conversion is deliberately disabled. Once a protected operation is claimed, any
    exception means its outcome may be unknown; returning that exception to the model as an
    ordinary ``ToolMessage`` could invite an unsafe retry.
    """

    try:
        from langgraph.prebuilt import ToolNode
    except ImportError as error:  # pragma: no cover - exercised in a clean optional install
        raise ImportError("LangGraph integration requires 'agentbarrier[langgraph]'") from error

    return ToolNode(
        tools,
        name=name,
        tags=tags,
        handle_tool_errors=False,
        messages_key=messages_key,
    )


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


__all__ = ["runtime_structured_tool", "runtime_tool_node"]

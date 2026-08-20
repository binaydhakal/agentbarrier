from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbarrier.models import JsonValue
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimePolicy,
    RuntimeRequest,
)


def test_runtime_request_binds_canonical_identity_and_detaches_arguments() -> None:
    arguments: dict[str, JsonValue] = {"amount": 25, "nested": {"currency": "USD"}}
    request = RuntimeRequest(
        action_id="action-1",
        namespace="billing",
        tool_name="payments.refund",
        arguments=arguments,
        idempotency_key="refund-1",
        policy_version="2026-08-20",
        created_at_ns=1,
    )
    arguments["amount"] = 100
    nested = request.arguments["nested"]
    assert isinstance(nested, dict)
    nested["currency"] = "EUR"

    assert request.arguments == {"amount": 25, "nested": {"currency": "USD"}}
    assert len(request.request_digest) == 64
    changed = RuntimeRequest(
        action_id="action-2",
        namespace="billing",
        tool_name="payments.refund",
        arguments={"amount": 26, "nested": {"currency": "USD"}},
        idempotency_key="refund-1",
        policy_version="2026-08-20",
        created_at_ns=2,
    )
    assert changed.request_digest != request.request_digest


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("action_id", ""),
        ("namespace", " "),
        ("tool_name", ""),
        ("idempotency_key", ""),
        ("policy_version", ""),
    ],
)
def test_runtime_request_rejects_empty_identity(keyword: str, value: str) -> None:
    data = {
        "action_id": "a",
        "namespace": "n",
        "tool_name": "tool",
        "arguments": {},
        "idempotency_key": "key",
        "policy_version": "1",
        "created_at_ns": 1,
    }
    data[keyword] = value
    with pytest.raises(ValueError, match=keyword):
        RuntimeRequest(**data)  # type: ignore[arg-type]


def test_runtime_request_rejects_invalid_json_and_timestamp() -> None:
    with pytest.raises(ValueError, match="created_at_ns"):
        RuntimeRequest(
            action_id="a",
            namespace="n",
            tool_name="tool",
            arguments={},
            idempotency_key="key",
            policy_version="1",
            created_at_ns=-1,
        )
    with pytest.raises(ValueError, match="non-finite"):
        RuntimeRequest(
            action_id="a",
            namespace="n",
            tool_name="tool",
            arguments={"bad": float("nan")},
            idempotency_key="key",
            policy_version="1",
            created_at_ns=1,
        )


@pytest.mark.parametrize(
    ("condition", "arguments", "expected"),
    [
        (ArgumentCondition("value", ConditionOperator.EXISTS), {"value": None}, True),
        (ArgumentCondition("value", ConditionOperator.EXISTS, False), {}, True),
        (ArgumentCondition("value", ConditionOperator.EQ, 3), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.NE, 3), {"value": 4}, True),
        (ArgumentCondition("value", ConditionOperator.LT, 4), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.LE, 3), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.GT, 2), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.GE, 3), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.IN, [2, 3]), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.NOT_IN, [1, 2]), {"value": 3}, True),
        (ArgumentCondition("value", ConditionOperator.CONTAINS, "bar"), {"value": "foobar"}, True),
        (ArgumentCondition("value", ConditionOperator.CONTAINS, 2), {"value": [1, 2]}, True),
        (
            ArgumentCondition("value", ConditionOperator.CONTAINS, "key"),
            {"value": {"key": 1}},
            True,
        ),
        (
            ArgumentCondition("value", ConditionOperator.STARTS_WITH, "pre"),
            {"value": "prefix"},
            True,
        ),
        (ArgumentCondition("value", ConditionOperator.ENDS_WITH, "fix"), {"value": "suffix"}, True),
        (
            ArgumentCondition("nested.amount", ConditionOperator.GT, 20),
            {"nested": {"amount": 50}},
            True,
        ),
        (ArgumentCondition("missing", ConditionOperator.EQ, 1), {}, False),
        (ArgumentCondition("value", ConditionOperator.GT, 1), {"value": "3"}, False),
        (ArgumentCondition("value", ConditionOperator.IN, "bad"), {"value": 1}, False),
        (ArgumentCondition("value", ConditionOperator.CONTAINS, 1), {"value": "abc"}, False),
    ],
)
def test_argument_conditions(
    condition: ArgumentCondition, arguments: dict[str, object], expected: bool
) -> None:
    assert condition.matches(arguments) is expected  # type: ignore[arg-type]


def test_policy_uses_first_matching_rule_and_fails_closed_by_default() -> None:
    policy = RuntimePolicy(
        version="1",
        rules=(
            PolicyRule(
                "large refund",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.*",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
                approval_ttl_seconds=60,
            ),
            PolicyRule("small refund", PolicyEffect.ALLOW, tool="payments.*"),
        ),
    )

    large = policy.evaluate("payments.refund", {"amount": 50})
    small = policy.evaluate("payments.refund", {"amount": 10})
    unknown = policy.evaluate("email.send", {"to": "person@example.com"})

    assert large.effect is PolicyEffect.REQUIRE_APPROVAL
    assert large.rule_name == "large refund"
    assert large.approval_ttl_seconds == 60
    assert small.effect is PolicyEffect.ALLOW
    assert unknown.effect is PolicyEffect.DENY
    assert unknown.rule_name == "<default>"


def test_policy_loads_strict_json_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "2",
                "default": "deny",
                "rules": [
                    {
                        "name": "review writes",
                        "effect": "require_approval",
                        "tool": "database.*",
                        "approval_ttl_seconds": 30,
                        "conditions": [{"path": "operation", "operator": "ne", "value": "read"}],
                    }
                ],
            }
        )
    )

    policy = RuntimePolicy.from_file(path)
    decision = policy.evaluate("database.query", {"operation": "delete"})
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.policy_version == "2"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "JSON object"),
        ({"version": 1, "rules": []}, "version"),
        ({"version": "1", "default": 1, "rules": []}, "default"),
        ({"version": "1", "rules": "bad"}, "rules"),
        ({"version": "1", "rules": ["bad"]}, "policy rule"),
        ({"version": "1", "rules": [{"name": 1, "effect": "allow"}]}, "strings"),
        (
            {
                "version": "1",
                "rules": [{"name": "x", "effect": "allow", "approval_ttl_seconds": "bad"}],
            },
            "number",
        ),
        (
            {"version": "1", "rules": [{"name": "x", "effect": "allow", "conditions": "bad"}]},
            "conditions",
        ),
        (
            {"version": "1", "rules": [{"name": "x", "effect": "allow", "conditions": ["bad"]}]},
            "condition",
        ),
        (
            {
                "version": "1",
                "rules": [
                    {"name": "x", "effect": "allow", "conditions": [{"path": 1, "operator": "eq"}]}
                ],
            },
            "path",
        ),
    ],
)
def test_policy_rejects_malformed_documents(tmp_path: Path, value: object, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value))
    with pytest.raises((TypeError, ValueError), match=message):
        RuntimePolicy.from_file(path)


def test_policy_rejects_invalid_rule_configuration() -> None:
    with pytest.raises(ValueError, match="unique"):
        RuntimePolicy(
            "1",
            (
                PolicyRule("same", PolicyEffect.ALLOW),
                PolicyRule("same", PolicyEffect.DENY),
            ),
        )
    with pytest.raises(ValueError, match="only"):
        PolicyRule("allow", PolicyEffect.ALLOW, approval_ttl_seconds=1)
    with pytest.raises(ValueError, match="greater"):
        PolicyRule("review", PolicyEffect.REQUIRE_APPROVAL, approval_ttl_seconds=0)
    with pytest.raises(ValueError, match="path"):
        ArgumentCondition("", ConditionOperator.EQ, 1)

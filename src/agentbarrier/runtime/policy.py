"""Deterministic runtime policy evaluation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast

from agentbarrier.models import JsonValue
from agentbarrier.runtime.models import (
    ConditionOperator,
    PolicyDecision,
    PolicyEffect,
    canonical_json,
)

_MISSING = object()


@dataclass(frozen=True, slots=True)
class ArgumentCondition:
    """One comparison against a dotted path inside canonical tool arguments."""

    path: str
    operator: ConditionOperator
    value: JsonValue = True

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("condition path must not be empty")
        canonical_json(self.value, path="condition value")
        if self.operator is ConditionOperator.EXISTS and not isinstance(self.value, bool):
            raise TypeError("exists condition value must be a boolean")
        if self.operator in {ConditionOperator.IN, ConditionOperator.NOT_IN} and not isinstance(
            self.value, list
        ):
            raise TypeError(f"{self.operator.value} condition value must be a list")
        if self.operator in {
            ConditionOperator.STARTS_WITH,
            ConditionOperator.ENDS_WITH,
        } and not isinstance(self.value, str):
            raise TypeError(f"{self.operator.value} condition value must be a string")
        if self.operator in {
            ConditionOperator.LT,
            ConditionOperator.LE,
            ConditionOperator.GT,
            ConditionOperator.GE,
        } and (not isinstance(self.value, (int, float, str)) or isinstance(self.value, bool)):
            raise TypeError(f"{self.operator.value} condition value must be a number or string")

    def matches(self, arguments: Mapping[str, JsonValue]) -> bool:
        """Return whether the argument value satisfies this condition."""

        actual: object = arguments
        for segment in self.path.split("."):
            if not isinstance(actual, Mapping) or segment not in actual:
                actual = _MISSING
                break
            actual = actual[segment]

        if self.operator is ConditionOperator.EXISTS:
            return (actual is not _MISSING) is bool(self.value)
        if actual is _MISSING:
            return False
        if self.operator is ConditionOperator.EQ:
            return actual == self.value
        if self.operator is ConditionOperator.NE:
            return actual != self.value
        if self.operator in {
            ConditionOperator.LT,
            ConditionOperator.LE,
            ConditionOperator.GT,
            ConditionOperator.GE,
        }:
            return self._ordered_match(actual)
        if self.operator in {ConditionOperator.IN, ConditionOperator.NOT_IN}:
            if not isinstance(self.value, list):
                return False
            included = actual in self.value
            return included if self.operator is ConditionOperator.IN else not included
        if self.operator is ConditionOperator.CONTAINS:
            if isinstance(actual, str) and isinstance(self.value, str):
                return self.value in actual
            if isinstance(actual, list):
                return self.value in actual
            if isinstance(actual, Mapping) and isinstance(self.value, str):
                return self.value in actual
            return False
        if self.operator is ConditionOperator.STARTS_WITH:
            return (
                isinstance(actual, str)
                and isinstance(self.value, str)
                and actual.startswith(self.value)
            )
        if self.operator is ConditionOperator.ENDS_WITH:
            return (
                isinstance(actual, str)
                and isinstance(self.value, str)
                and actual.endswith(self.value)
            )
        return False  # pragma: no cover - exhaustive enum handling

    def _ordered_match(self, actual: object) -> bool:
        both_numbers = (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and isinstance(self.value, (int, float))
            and not isinstance(self.value, bool)
        )
        both_strings = isinstance(actual, str) and isinstance(self.value, str)
        if not (both_numbers or both_strings):
            return False
        if self.operator is ConditionOperator.LT:
            return actual < self.value  # type: ignore[operator]
        if self.operator is ConditionOperator.LE:
            return actual <= self.value  # type: ignore[operator]
        if self.operator is ConditionOperator.GT:
            return actual > self.value  # type: ignore[operator]
        return actual >= self.value  # type: ignore[operator]


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """An ordered rule matching a tool glob and all argument conditions."""

    name: str
    effect: PolicyEffect
    tool: str = "*"
    conditions: tuple[ArgumentCondition, ...] = ()
    approval_ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("rule name must not be empty")
        if not self.tool.strip():
            raise ValueError("rule tool pattern must not be empty")
        if self.approval_ttl_seconds is not None and (
            not math.isfinite(self.approval_ttl_seconds) or self.approval_ttl_seconds <= 0
        ):
            raise ValueError("approval_ttl_seconds must be finite and greater than zero")
        if (
            self.effect is not PolicyEffect.REQUIRE_APPROVAL
            and self.approval_ttl_seconds is not None
        ):
            raise ValueError("approval_ttl_seconds is valid only for require_approval rules")

    def matches(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> bool:
        """Return whether this rule matches the complete request."""

        return fnmatchcase(tool_name, self.tool) and all(
            condition.matches(arguments) for condition in self.conditions
        )


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Ordered, fail-closed runtime policy."""

    version: str
    rules: tuple[PolicyRule, ...]
    default_effect: PolicyEffect = PolicyEffect.DENY

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("policy version must not be empty")
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("policy rule names must be unique")

    def evaluate(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> PolicyDecision:
        """Return the first matching rule, or the explicit default."""

        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        for rule in self.rules:
            if rule.matches(tool_name, arguments):
                return PolicyDecision(
                    effect=rule.effect,
                    rule_name=rule.name,
                    policy_version=self.version,
                    approval_ttl_seconds=rule.approval_ttl_seconds,
                )
        return PolicyDecision(
            effect=self.default_effect,
            rule_name="<default>",
            policy_version=self.version,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> RuntimePolicy:
        """Load a strict JSON policy file."""

        data: object = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("policy document must be a JSON object")
        return cls.from_mapping(cast(Mapping[str, object], data))

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RuntimePolicy:
        """Parse and validate a mapping as a runtime policy."""

        cls._validate_keys(data, {"version", "default", "rules"}, label="policy")
        version = data.get("version")
        if not isinstance(version, str):
            raise TypeError("policy version must be a string")
        default_value = data.get("default", PolicyEffect.DENY.value)
        if not isinstance(default_value, str):
            raise TypeError("policy default must be a string")
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
            raise TypeError("policy rules must be a list")
        rules = tuple(cls._parse_rule(item) for item in raw_rules)
        return cls(
            version=version,
            rules=rules,
            default_effect=PolicyEffect(default_value),
        )

    @staticmethod
    def _parse_rule(value: object) -> PolicyRule:
        if not isinstance(value, Mapping):
            raise TypeError("each policy rule must be an object")
        rule = cast(Mapping[str, object], value)
        RuntimePolicy._validate_keys(
            rule,
            {"name", "effect", "tool", "approval_ttl_seconds", "conditions"},
            label="rule",
        )
        name = rule.get("name")
        effect = rule.get("effect")
        tool = rule.get("tool", "*")
        ttl = rule.get("approval_ttl_seconds")
        raw_conditions = rule.get("conditions", [])
        if not isinstance(name, str) or not isinstance(effect, str) or not isinstance(tool, str):
            raise TypeError("rule name, effect, and tool must be strings")
        if ttl is not None and (not isinstance(ttl, (int, float)) or isinstance(ttl, bool)):
            raise TypeError("approval_ttl_seconds must be a number")
        if not isinstance(raw_conditions, Sequence) or isinstance(raw_conditions, (str, bytes)):
            raise TypeError("rule conditions must be a list")
        conditions = tuple(RuntimePolicy._parse_condition(item) for item in raw_conditions)
        return PolicyRule(
            name=name,
            effect=PolicyEffect(effect),
            tool=tool,
            conditions=conditions,
            approval_ttl_seconds=float(ttl) if ttl is not None else None,
        )

    @staticmethod
    def _parse_condition(value: object) -> ArgumentCondition:
        if not isinstance(value, Mapping):
            raise TypeError("each policy condition must be an object")
        condition = cast(Mapping[str, object], value)
        RuntimePolicy._validate_keys(condition, {"path", "operator", "value"}, label="condition")
        path = condition.get("path")
        operator = condition.get("operator")
        condition_value = condition.get("value", True)
        if not isinstance(path, str) or not isinstance(operator, str):
            raise TypeError("condition path and operator must be strings")
        return ArgumentCondition(
            path=path,
            operator=ConditionOperator(operator),
            value=cast(JsonValue, condition_value),
        )

    @staticmethod
    def _validate_keys(value: Mapping[str, object], allowed: set[str], *, label: str) -> None:
        non_string = [key for key in value if not isinstance(key, str)]
        if non_string:
            raise TypeError(f"{label} keys must be strings")
        unknown = sorted(key for key in value if key not in allowed)
        if unknown:
            raise ValueError(f"unknown {label} keys: {', '.join(unknown)}")

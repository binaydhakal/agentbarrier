from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agentbarrier.runtime import PolicyEffect, RuntimePolicy

SCHEMA_PATH = Path("docs/schemas/runtime-policy-v1.schema.json")


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_runtime_policy_schema_accepts_parser_compatible_policy() -> None:
    document = {
        "version": "refunds-v1",
        "default": "deny",
        "rules": [
            {
                "name": "review large refunds",
                "effect": "require_approval",
                "tool": "payments.refund",
                "approval_ttl_seconds": 3600,
                "conditions": [
                    {"path": "amount", "operator": "gt", "value": 20},
                    {"path": "currency", "operator": "in", "value": ["USD", "CAD"]},
                ],
            }
        ],
    }

    _validator().validate(document)
    policy = RuntimePolicy.from_mapping(document)
    assert policy.rules[0].effect is PolicyEffect.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "document",
    [
        {"version": "1", "rules": [], "unknown": True},
        {"version": "1", "rules": [{"name": "allow", "effect": "allow", "unknown": 1}]},
        {
            "version": "1",
            "rules": [
                {
                    "name": "bad membership",
                    "effect": "deny",
                    "conditions": [{"path": "amount", "operator": "in", "value": "100"}],
                }
            ],
        },
        {
            "version": "1",
            "rules": [
                {
                    "name": "too short TTL",
                    "effect": "require_approval",
                    "approval_ttl_seconds": 1e-12,
                }
            ],
        },
        {
            "version": "1",
            "rules": [
                {
                    "name": "bad TTL owner",
                    "effect": "allow",
                    "approval_ttl_seconds": 10,
                }
            ],
        },
    ],
)
def test_runtime_policy_schema_rejects_malformed_policy(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _validator().validate(document)

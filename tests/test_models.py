from __future__ import annotations

import pytest

from agentbarrier.errors import AmbiguousEffectError, SuiteFailure
from agentbarrier.models import (
    ActionRequest,
    ApprovalBarrierProfile,
    ScenarioResult,
    ScenarioStatus,
    SuiteResult,
    action_digest,
)


def test_action_request_validates_identity_and_copies_arguments() -> None:
    arguments = {"amount": 3, "nested": {"value": 1}, "items": [1, 2]}
    request = ActionRequest("action-1", "refund", arguments)
    arguments["amount"] = 99
    arguments["nested"]["value"] = 99  # type: ignore[index]

    assert dict(request.arguments) == {
        "amount": 3,
        "nested": {"value": 1},
        "items": [1, 2],
    }
    detached = request.arguments["nested"]
    assert isinstance(detached, dict)
    detached["value"] = 42
    assert request.arguments["nested"] == {"value": 1}
    with pytest.raises(TypeError):
        request.arguments["amount"] = 5  # type: ignore[index]
    with pytest.raises(ValueError, match="action_id"):
        ActionRequest(" ", "refund", {})
    with pytest.raises(ValueError, match="tool_name"):
        ActionRequest("action-1", "", {})


def test_ambiguous_effect_error_distinguishes_observed_and_unconfirmed_commits() -> None:
    committed = AmbiguousEffectError("action")
    unconfirmed = AmbiguousEffectError("action", commit_observed=False)

    assert committed.commit_observed is True
    assert "after 'action' committed" in str(committed)
    assert unconfirmed.commit_observed is False
    assert "commit could be confirmed" in str(unconfirmed)


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"bad": float("nan")}, ValueError),
        ({"bad": object()}, TypeError),
        ({"bad": [{1: "not a string key"}]}, TypeError),
        ({"bad": (1, 2)}, TypeError),
    ],
)
def test_action_request_rejects_non_json_arguments(
    arguments: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        ActionRequest("action-1", "refund", arguments)  # type: ignore[arg-type]


def test_action_request_replacement_preserves_identity() -> None:
    request = ActionRequest("a", "write", {"value": 1}, parent_action_id="parent")
    replacement = request.with_arguments({"value": 2})

    assert replacement.action_id == request.action_id
    assert replacement.tool_name == request.tool_name
    assert replacement.parent_action_id == "parent"
    assert dict(replacement.arguments) == {"value": 2}
    assert action_digest(replacement) != action_digest(request)


def test_suite_counts_and_failure_policy() -> None:
    results = (
        ScenarioResult("pass", "Pass", "demo", ScenarioStatus.PASSED, 0.1),
        ScenarioResult("skip", "Skip", "demo", ScenarioStatus.SKIPPED, 0.1),
    )
    ordinary = SuiteResult("demo", results)
    strict = SuiteResult("demo", results, strict_skips=True)
    per_action = SuiteResult(
        "demo",
        results,
        approval_profile=ApprovalBarrierProfile.PER_ACTION,
    )

    assert ordinary.passed
    assert ordinary.approval_profile is ApprovalBarrierProfile.RUN_WIDE
    assert per_action.approval_profile is ApprovalBarrierProfile.PER_ACTION
    assert ordinary.exit_code == 0
    ordinary.raise_for_failure()
    assert strict.passed_count == 1
    assert strict.skipped_count == 1
    assert not strict.passed
    assert strict.exit_code == 1
    with pytest.raises(SuiteFailure, match="skip: skipped"):
        strict.raise_for_failure()


def test_suite_counts_failures_and_errors() -> None:
    suite = SuiteResult(
        "demo",
        (
            ScenarioResult("bad", "Bad", "demo", ScenarioStatus.FAILED, 0.1),
            ScenarioResult("boom", "Boom", "demo", ScenarioStatus.ERROR, 0.1),
        ),
    )
    assert suite.failed_count == 1
    assert suite.error_count == 1
    assert not suite.passed

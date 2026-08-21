from __future__ import annotations

from pathlib import Path

import pytest

from tools.audit_runtime_controls_wheel import run_audit as run_controls_audit
from tools.audit_runtime_wheel import run_audit


def test_runtime_wheel_audit_executes_effect_once(tmp_path: Path) -> None:
    result = run_audit(tmp_path)
    assert result["status"] == "passed"
    assert result["effect_count"] == 1
    assert result["events"] == [
        "approval_requested",
        "approved",
        "execution_started",
        "execution_succeeded",
        "result_replayed",
    ]


def test_runtime_controls_wheel_audit_blocks_pause_and_limit(tmp_path: Path) -> None:
    result = run_controls_audit(tmp_path)
    assert result["status"] == "passed"
    assert result["effect_count"] == 1
    assert result["blocked_limit"] == "one-refund-per-window"
    assert result["usage"] == {"actions": 1, "value": 2_500}
    assert result["control_events"] == [
        "limit_configured",
        "emergency_pause_set",
        "emergency_pause_cleared",
    ]


def test_langgraph_wheel_audit_executes_effect_once(tmp_path: Path) -> None:
    pytest.importorskip("langgraph", reason="LangGraph optional dependency is not installed")
    from tools.audit_langgraph_wheel import run_audit as run_langgraph_audit

    result = run_langgraph_audit(tmp_path)
    assert result["status"] == "passed"
    assert result["effect_count"] == 1
    assert result["events"] == [
        "approval_requested",
        "approved",
        "execution_started",
        "execution_succeeded",
        "result_replayed",
    ]


def test_pydantic_ai_wheel_audit_executes_effect_once(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai", reason="PydanticAI optional dependency is not installed")
    from tools.audit_pydantic_ai_wheel import run_audit as run_pydantic_ai_audit

    result = run_pydantic_ai_audit(tmp_path)
    assert result["status"] == "passed"
    assert result["effect_count"] == 1
    assert result["events"] == [
        "approval_requested",
        "approved",
        "execution_started",
        "execution_succeeded",
        "result_replayed",
    ]


def test_google_adk_wheel_audit_executes_effect_once(tmp_path: Path) -> None:
    pytest.importorskip("google.adk", reason="Google ADK optional dependency is not installed")
    from tools.audit_google_adk_wheel import run_audit as run_google_adk_audit

    result = run_google_adk_audit(tmp_path)
    assert result["status"] == "passed"
    assert result["effect_count"] == 1
    assert result["events"] == [
        "approval_requested",
        "approved",
        "execution_started",
        "execution_succeeded",
        "result_replayed",
    ]

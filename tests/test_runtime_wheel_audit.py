from __future__ import annotations

from pathlib import Path

import pytest

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

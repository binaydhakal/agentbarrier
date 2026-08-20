from __future__ import annotations

from pathlib import Path

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

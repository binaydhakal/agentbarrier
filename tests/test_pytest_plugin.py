from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_installed_pytest_entrypoint_loads_fixture(tmp_path: Path) -> None:
    consumer_test = tmp_path / "test_consumer.py"
    consumer_test.write_text(
        """
from agentbarrier.adapters.reference import ReferenceAdapter

def test_controls(agentbarrier):
    suite = agentbarrier.verify_sync(ReferenceAdapter())
    assert suite.passed_count == 10
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(consumer_test)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout

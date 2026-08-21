from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_container_uses_locked_non_root_runtime() -> None:
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "--extra postgres-binary" in dockerfile
    assert "COPY uv.lock" in dockerfile or "COPY pyproject.toml uv.lock" in dockerfile
    assert "FROM ${PYTHON_IMAGE} AS runtime" in dockerfile
    runtime_stage = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime", maxsplit=1)[1]
    assert "USER 10001:10001" in runtime_stage
    assert 'ENTRYPOINT ["agentbarrier"]' in runtime_stage
    assert "PYTHONDONTWRITEBYTECODE=1" in runtime_stage


def test_compose_preserves_migration_and_network_boundaries() -> None:
    compose = yaml.safe_load((ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    database = services["postgres"]
    migration = services["migrate"]
    api = services["api"]

    assert database["image"] == "postgres:18.4-alpine"
    assert "ports" not in database
    assert database["healthcheck"]["test"][0] == "CMD-SHELL"
    assert database["volumes"][0] == "postgres-data:/var/lib/postgresql"
    assert database["volumes"][1].endswith(
        ":/docker-entrypoint-initdb.d/10-agentbarrier-runtime.sh:ro"
    )
    assert migration["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "--postgres-create-schema" in migration["command"]
    assert "AGENTBARRIER_MIGRATION_POSTGRES_DSN" in migration["environment"]
    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["ports"] == ["127.0.0.1:8787:8787"]
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert "AGENTBARRIER_RUNTIME_POSTGRES_DSN" in api["environment"]
    assert set(api["environment"]).isdisjoint(migration["environment"])
    assert api["volumes"] == ["./config/approval-auth.json:/run/agentbarrier/approval-auth.json:ro"]
    assert database["networks"] == ["database"]
    assert migration["networks"] == ["database"]
    assert api["networks"] == ["database", "ingress"]
    assert compose["networks"]["database"]["internal"] is True
    assert compose["networks"]["ingress"] is None
    assert (ROOT / "deploy" / "postgres" / "10-agentbarrier-runtime.sh").stat().st_mode & 0o111


def test_deployment_examples_cannot_contain_a_working_credential() -> None:
    auth = json.loads(
        (ROOT / "deploy" / "config" / "approval-auth.example.json").read_text(encoding="utf-8")
    )
    assert auth["version"] == "2"
    assert auth["organizations"][0]["require_separate_approver"] is True
    assert auth["tokens"][0]["token_sha256"].startswith("REPLACE_")

    environment = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert "replace-with" in environment
    assert "AGENTBARRIER_MIGRATION_POSTGRES_DSN" in environment
    assert "AGENTBARRIER_RUNTIME_POSTGRES_DSN" in environment
    assert "deploy/.env" in (ROOT / ".gitignore").read_text(encoding="utf-8")

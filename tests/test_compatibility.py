from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agentbarrier.compatibility import (
    DEFAULT_ADAPTER_SPECS,
    MARKDOWN_END,
    MARKDOWN_START,
    AdapterSpec,
    dump_compatibility_evidence,
    evidence_has_errors,
    generate_compatibility_evidence,
    render_compatibility_section,
    replace_compatibility_section,
    select_adapter_specs,
    write_compatibility_outputs,
)
from agentbarrier.models import ApprovalBarrierProfile

ROOT = Path(__file__).parents[1]


def test_reference_evidence_is_deterministic_and_schema_valid() -> None:
    specs = select_adapter_specs(("reference",))
    first = generate_compatibility_evidence(specs=specs)
    second = generate_compatibility_evidence(specs=specs)
    schema = json.loads((ROOT / "docs/schemas/compatibility-v1.schema.json").read_text())

    Draft202012Validator(schema).validate(first)
    assert first == second
    assert first["profiles"] == ["run-wide", "per-action"]
    adapter = first["adapters"][0]
    assert adapter["distribution"]["name"] == "agentbarrier"
    assert adapter["profiles"]["run-wide"]["summary"]["passed"] == 11
    assert adapter["profiles"]["per-action"]["summary"]["passed"] == 11
    serialized = dump_compatibility_evidence(first)
    assert serialized.endswith("\n")
    assert "duration" not in serialized
    assert "timestamp" not in serialized
    assert "run_id" not in serialized


def test_missing_distribution_is_explicit_and_optionally_strict() -> None:
    evidence = generate_compatibility_evidence(
        specs=(
            AdapterSpec(
                key="missing",
                label="Missing",
                target="missing.module:Adapter",
                distribution="agentbarrier-package-that-does-not-exist",
            ),
        ),
        profiles=(ApprovalBarrierProfile.RUN_WIDE,),
    )

    adapter = evidence["adapters"][0]
    assert adapter["available"] is False
    assert "not installed" in adapter["unavailable_reason"]
    assert not evidence_has_errors(evidence, strict_missing=False)
    assert evidence_has_errors(evidence, strict_missing=True)


def test_markdown_generation_and_drift_check(tmp_path: Path) -> None:
    evidence = generate_compatibility_evidence(specs=select_adapter_specs(("reference",)))
    markdown = tmp_path / "compatibility.md"
    json_path = tmp_path / "nested/evidence.json"
    markdown.write_text(f"Before\n\n{MARKDOWN_START}\nstale\n{MARKDOWN_END}\n\nAfter\n")

    assert write_compatibility_outputs(
        evidence,
        json_path=json_path,
        markdown_path=markdown,
        check_outputs=False,
    )
    updated = markdown.read_text()
    assert "Canonical evidence: Python" in updated
    assert "| Reference |" in updated
    assert updated == replace_compatibility_section(updated, evidence)
    assert write_compatibility_outputs(
        evidence,
        json_path=json_path,
        markdown_path=markdown,
        check_outputs=True,
    )

    json_path.write_text("{}\n")
    assert not write_compatibility_outputs(
        evidence,
        json_path=json_path,
        markdown_path=markdown,
        check_outputs=True,
    )
    assert json_path.read_text() == "{}\n"

    write_compatibility_outputs(
        evidence,
        json_path=json_path,
        markdown_path=markdown,
        check_outputs=False,
    )
    markdown.write_text(updated.replace("| Reference |", "| Drifted |"))
    assert not write_compatibility_outputs(
        evidence,
        json_path=json_path,
        markdown_path=markdown,
        check_outputs=True,
    )
    assert json.loads(json_path.read_text()) == evidence


def test_committed_evidence_and_document_are_in_sync() -> None:
    evidence = json.loads((ROOT / "docs/compatibility.json").read_text())
    schema = json.loads((ROOT / "docs/schemas/compatibility-v1.schema.json").read_text())
    document = (ROOT / "docs/compatibility.md").read_text()

    Draft202012Validator(schema).validate(evidence)
    assert document == replace_compatibility_section(document, evidence)


def test_committed_crewai_evidence_and_document_are_in_sync() -> None:
    evidence = json.loads((ROOT / "docs/crewai-evaluation.json").read_text())
    schema = json.loads((ROOT / "docs/schemas/compatibility-v1.schema.json").read_text())
    document = (ROOT / "docs/crewai-evaluation.md").read_text()

    Draft202012Validator(schema).validate(evidence)
    assert evidence["adapters"][0]["key"] == "crewai"
    assert document == replace_compatibility_section(document, evidence)


def test_selection_and_profile_validation() -> None:
    assert select_adapter_specs(None) == DEFAULT_ADAPTER_SPECS
    assert select_adapter_specs(("crewai",))[0].distribution == "crewai"
    with pytest.raises(ValueError, match="unknown compatibility adapters"):
        select_adapter_specs(("unknown",))
    with pytest.raises(ValueError, match="at least one approval profile"):
        generate_compatibility_evidence(specs=(), profiles=())
    with pytest.raises(ValueError, match="must not be duplicated"):
        generate_compatibility_evidence(
            specs=(),
            profiles=(ApprovalBarrierProfile.RUN_WIDE, ApprovalBarrierProfile.RUN_WIDE),
        )


def test_unavailable_and_runtime_error_evidence_paths() -> None:
    future = AdapterSpec(
        key="future",
        label="Future",
        target="agentbarrier.adapters.reference:ReferenceAdapter",
        distribution="agentbarrier",
        minimum_python=(99, 0),
    )
    future_evidence = generate_compatibility_evidence(
        specs=(future,), profiles=(ApprovalBarrierProfile.RUN_WIDE,)
    )
    assert future_evidence["adapters"][0]["unavailable_reason"] == "requires Python >=99.0"
    assert "Unavailable" in render_compatibility_section(future_evidence)

    unloadable = AdapterSpec(
        key="unloadable",
        label="Unloadable",
        target="agentbarrier.compatibility:EVIDENCE_SCHEMA_VERSION",
        distribution="agentbarrier",
    )
    unloadable_evidence = generate_compatibility_evidence(
        specs=(unloadable,), profiles=(ApprovalBarrierProfile.RUN_WIDE,)
    )
    assert "TypeError while loading" in unloadable_evidence["adapters"][0]["unavailable_reason"]

    error_evidence = generate_compatibility_evidence(
        specs=select_adapter_specs(("reference",)),
        profiles=(ApprovalBarrierProfile.RUN_WIDE,),
    )
    error_evidence["adapters"][0]["profiles"]["run-wide"]["results"][0]["status"] = "error"
    assert evidence_has_errors(error_evidence, strict_missing=False)
    assert "**Error**" in render_compatibility_section(error_evidence)


def test_markdown_and_output_check_validation(tmp_path: Path) -> None:
    evidence = generate_compatibility_evidence(
        specs=select_adapter_specs(("reference",)),
        profiles=(ApprovalBarrierProfile.PER_ACTION,),
    )
    with pytest.raises(ValueError, match="run-wide profile evidence"):
        render_compatibility_section(evidence)
    with pytest.raises(ValueError, match="generated-section marker pair"):
        replace_compatibility_section("no markers", evidence)
    with pytest.raises(ValueError, match="--check requires"):
        write_compatibility_outputs(
            evidence,
            json_path=None,
            markdown_path=None,
            check_outputs=True,
        )

    missing_json = tmp_path / "missing.json"
    assert not write_compatibility_outputs(
        evidence,
        json_path=missing_json,
        markdown_path=None,
        check_outputs=True,
    )
    assert not missing_json.exists()

    missing_markdown = tmp_path / "missing.md"
    assert not write_compatibility_outputs(
        evidence,
        json_path=None,
        markdown_path=missing_markdown,
        check_outputs=True,
    )

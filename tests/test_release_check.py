from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_pypi_release import (
    distribution_hashes,
    published_hashes,
    record_result,
    verify_matching_release,
)


def test_distribution_and_published_hashes_match(tmp_path: Path) -> None:
    wheel = tmp_path / "agentbarrier-0.3.0-py3-none-any.whl"
    source = tmp_path / "agentbarrier-0.3.0.tar.gz"
    ignored = tmp_path / ".gitignore"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    ignored.write_text("*", encoding="utf-8")

    local = distribution_hashes(tmp_path)
    assert ".gitignore" not in local
    metadata = {
        "urls": [
            {"filename": name, "digests": {"sha256": digest}} for name, digest in local.items()
        ]
    }

    verify_matching_release(local, published_hashes(metadata))


def test_release_check_fails_closed_for_file_or_hash_drift() -> None:
    local = {"agentbarrier.whl": "expected"}

    with pytest.raises(ValueError, match="artifact set differs"):
        verify_matching_release(local, {})
    with pytest.raises(ValueError, match="artifact hashes differ"):
        verify_matching_release(local, {"agentbarrier.whl": "different"})


def test_release_check_rejects_incomplete_pypi_metadata() -> None:
    with pytest.raises(ValueError, match="without a SHA-256"):
        published_hashes({"urls": [{"filename": "agentbarrier.whl", "digests": {}}]})


def test_record_result_uses_github_output_format(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    record_result(output, published=True)
    record_result(output, published=False)
    assert output.read_text(encoding="utf-8") == "published=true\npublished=false\n"

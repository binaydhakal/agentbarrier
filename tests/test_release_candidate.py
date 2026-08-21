from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools.check_release_candidate import source_version, validate_candidate

ROOT = Path(__file__).resolve().parents[1]


def _write_source(root: Path, version: str, *, release_docs: bool = True) -> None:
    package = root / "src" / "agentbarrier"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "docs" / "releases").mkdir(parents=True)
    if release_docs:
        (root / "docs" / "releases" / f"{version}.md").write_text("release\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"## [{version}] - 2026-08-21\n", encoding="utf-8")


def _write_distributions(root: Path, version: str, *, metadata_version: str | None = None) -> Path:
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    recorded_version = metadata_version or version
    metadata = f"Name: agentbarrier\nVersion: {recorded_version}\n\n".encode()

    wheel = dist / f"agentbarrier-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(f"agentbarrier-{version}.dist-info/METADATA", metadata)

    sdist = dist / f"agentbarrier-{version}.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"agentbarrier-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    return dist


def test_release_candidate_binds_source_tag_metadata_docs_and_hashes(tmp_path: Path) -> None:
    _write_source(tmp_path, "1.0.0")
    dist = _write_distributions(tmp_path, "1.0.0")

    result = validate_candidate(
        source_root=tmp_path,
        dist_dir=dist,
        release_tag="v1.0.0",
        require_release_docs=True,
    )

    assert result["version"] == "1.0.0"
    assert set(result["artifacts"]) == {
        "agentbarrier-1.0.0-py3-none-any.whl",
        "agentbarrier-1.0.0.tar.gz",
    }
    assert all(len(digest) == 64 for digest in result["artifacts"].values())


def test_release_candidate_rejects_tag_metadata_and_artifact_drift(tmp_path: Path) -> None:
    _write_source(tmp_path, "1.0.0")
    dist = _write_distributions(tmp_path, "1.0.0")

    with pytest.raises(ValueError, match="does not match source version"):
        validate_candidate(source_root=tmp_path, dist_dir=dist, release_tag="v1.0.1")

    wheel = dist / "agentbarrier-1.0.0-py3-none-any.whl"
    wheel.unlink()
    with pytest.raises(ValueError, match="artifact set differs"):
        validate_candidate(source_root=tmp_path, dist_dir=dist)

    wheel_dist = _write_distributions(tmp_path, "1.0.0", metadata_version="1.0.1")
    with pytest.raises(ValueError, match="metadata version"):
        validate_candidate(source_root=tmp_path, dist_dir=wheel_dist)


def test_release_candidate_rejects_nonfinal_tag_and_missing_docs(tmp_path: Path) -> None:
    _write_source(tmp_path, "1.0.0.dev1", release_docs=False)
    dist = _write_distributions(tmp_path, "1.0.0.dev1")

    assert source_version(tmp_path) == "1.0.0.dev1"
    with pytest.raises(ValueError, match="not a final semantic version"):
        validate_candidate(source_root=tmp_path, dist_dir=dist, release_tag="v1.0.0.dev1")
    with pytest.raises(ValueError, match="release notes are missing"):
        validate_candidate(
            source_root=tmp_path,
            dist_dir=dist,
            require_release_docs=True,
        )


def test_publish_workflow_builds_and_checks_the_exact_release_tag() -> None:
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

    assert workflow.count("ref: ${{ github.event.release.tag_name }}") == 2
    assert '--release-tag "$RELEASE_TAG"' in workflow
    assert "contents: read" in workflow
    assert "--require-published" in workflow
    assert "id-token: write" not in workflow
    assert "gh-action-pypi-publish" not in workflow
    assert "password:" not in workflow
    assert "ref: main" not in workflow

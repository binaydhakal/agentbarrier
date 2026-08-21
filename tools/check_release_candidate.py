"""Fail closed unless built distributions match the intended source release."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

_FINAL_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def source_version(source_root: Path) -> str:
    """Read the literal package version without importing project code."""

    init_path = source_root / "src" / "agentbarrier" / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            return statement.value.value
        break
    raise ValueError(f"{init_path} does not contain a literal __version__ assignment")


def _metadata_version(payload: bytes, *, artifact: Path) -> str:
    metadata = BytesParser(policy=default).parsebytes(payload)
    if metadata.get("Name") != "agentbarrier":
        raise ValueError(f"{artifact.name} has unexpected project name {metadata.get('Name')!r}")
    version = metadata.get("Version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{artifact.name} has no valid Version metadata")
    return version


def wheel_version(path: Path) -> str:
    """Return the version from the wheel's core metadata."""

    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(members) != 1:
            raise ValueError(f"{path.name} must contain exactly one dist-info/METADATA file")
        return _metadata_version(archive.read(members[0]), artifact=path)


def sdist_version(path: Path) -> str:
    """Return the version from the source distribution's core metadata."""

    with tarfile.open(path, mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ValueError(f"{path.name} must contain exactly one PKG-INFO file")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError(f"{path.name} PKG-INFO could not be read")
        return _metadata_version(stream.read(), artifact=path)


def validate_candidate(
    *,
    source_root: Path,
    dist_dir: Path,
    expected_version: str | None = None,
    release_tag: str | None = None,
    require_release_docs: bool = False,
) -> dict[str, Any]:
    """Validate source, tag, filenames, metadata, release docs, and artifact hashes."""

    version = source_version(source_root)
    if expected_version is not None and version != expected_version:
        raise ValueError(f"source version {version!r} does not match {expected_version!r}")

    if release_tag is not None:
        if _FINAL_VERSION.fullmatch(version) is None:
            raise ValueError(f"release version {version!r} is not a final semantic version")
        if release_tag != f"v{version}":
            raise ValueError(
                f"release tag {release_tag!r} does not match source version v{version}"
            )

    expected_names = {
        f"agentbarrier-{version}-py3-none-any.whl",
        f"agentbarrier-{version}.tar.gz",
    }
    artifacts = sorted(
        path
        for path in dist_dir.iterdir()
        if path.is_file() and path.name.endswith((".whl", ".tar.gz", ".zip"))
    )
    observed_names = {path.name for path in artifacts}
    if observed_names != expected_names:
        raise ValueError(
            "candidate artifact set differs: "
            f"missing={sorted(expected_names - observed_names)}, "
            f"unexpected={sorted(observed_names - expected_names)}"
        )

    versions: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for artifact in artifacts:
        artifact_version = (
            wheel_version(artifact) if artifact.suffix == ".whl" else sdist_version(artifact)
        )
        if artifact_version != version:
            raise ValueError(
                f"{artifact.name} metadata version {artifact_version!r} does not match {version!r}"
            )
        versions[artifact.name] = artifact_version
        hashes[artifact.name] = hashlib.sha256(artifact.read_bytes()).hexdigest()

    if require_release_docs:
        release_notes = source_root / "docs" / "releases" / f"{version}.md"
        changelog = source_root / "CHANGELOG.md"
        if not release_notes.is_file():
            raise ValueError(f"release notes are missing: {release_notes}")
        if f"## [{version}]" not in changelog.read_text(encoding="utf-8"):
            raise ValueError(f"CHANGELOG.md has no {version} release heading")

    return {
        "artifacts": hashes,
        "metadata_versions": versions,
        "release_tag": release_tag,
        "version": version,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--release-tag")
    parser.add_argument("--require-release-docs", action="store_true")
    options = parser.parse_args(arguments)

    result = validate_candidate(
        source_root=options.source_root,
        dist_dir=options.dist_dir,
        expected_version=options.expected_version,
        release_tag=options.release_tag,
        require_release_docs=options.require_release_docs,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

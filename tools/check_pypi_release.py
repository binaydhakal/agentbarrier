"""Check whether locally built distributions already exist on PyPI unchanged."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def distribution_hashes(directory: Path) -> dict[str, str]:
    """Return SHA-256 hashes for every distribution in *directory*."""

    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith((".whl", ".tar.gz", ".zip"))
    )
    if not files:
        raise ValueError(f"no distributions found in {directory}")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def published_hashes(metadata: dict[str, Any]) -> dict[str, str]:
    """Extract filename-to-SHA-256 mappings from PyPI release metadata."""

    result: dict[str, str] = {}
    for artifact in metadata.get("urls", []):
        filename = artifact.get("filename")
        digest = artifact.get("digests", {}).get("sha256")
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise ValueError("PyPI metadata contains an artifact without a SHA-256 digest")
        result[filename] = digest
    return result


def verify_matching_release(
    local: dict[str, str],
    published: dict[str, str],
) -> None:
    """Fail unless PyPI contains exactly the locally built artifacts and hashes."""

    if local.keys() != published.keys():
        missing = sorted(local.keys() - published.keys())
        unexpected = sorted(published.keys() - local.keys())
        raise ValueError(f"PyPI artifact set differs: missing={missing}, unexpected={unexpected}")
    mismatched = sorted(name for name, digest in local.items() if published[name] != digest)
    if mismatched:
        raise ValueError(f"PyPI artifact hashes differ for: {mismatched}")


def fetch_release(project: str, version: str) -> dict[str, Any] | None:
    """Fetch release metadata, returning ``None`` only for a genuine 404."""

    url = f"https://pypi.org/pypi/{project}/{version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "agentbarrier-release-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload: Any = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if not isinstance(payload, dict):
        raise ValueError("PyPI returned invalid release metadata")
    return payload


def record_result(output: Path, *, published: bool) -> None:
    """Append the release state to a GitHub Actions output file."""

    with output.open("a", encoding="utf-8") as stream:
        stream.write(f"published={'true' if published else 'false'}\n")


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    options = parser.parse_args(arguments)

    version = options.version.removeprefix("v")
    local = distribution_hashes(options.dist_dir)
    metadata = fetch_release(options.project, version)
    if metadata is None:
        record_result(options.github_output, published=False)
        print(f"{options.project} {version} is not published; upload will proceed")
        return 0

    verify_matching_release(local, published_hashes(metadata))
    record_result(options.github_output, published=True)
    print(f"{options.project} {version} is already published with matching artifacts")
    return 0


if __name__ == "__main__":
    sys.exit(main())

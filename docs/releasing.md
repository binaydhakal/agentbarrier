# Maintainer release procedure

AgentBarrier releases are published deliberately from an audited, clean `main` revision. The local
PyPI CLI holds the maintainer credential; GitHub Actions receives no PyPI password and only verifies
that the public files match the release tag.

## Before approval

1. Finish the implementation, documentation, migration, and installed-wheel audits for the exact
   candidate revision.
2. Confirm the repository is clean and `main` matches `origin/main`.
3. Confirm every CI job for the candidate revision passed.
4. Confirm the version is absent from both GitHub Releases and PyPI.
5. Build into a new empty directory, run `twine check`, and bind the final tag to both artifact
   metadata files:

```bash
uv build --out-dir build/release
uv run twine check build/release/*
uv run python tools/check_release_candidate.py \
  --dist-dir build/release \
  --release-tag v1.0.0 \
  --require-release-docs
```

Record the displayed SHA-256 hashes in the approval request. Do not create a tag, upload a package,
or publish a GitHub release until the exact version is explicitly approved.

## Publish after approval

Create and push the annotated tag from the already-audited commit:

```bash
git tag --annotate v1.0.0 --message "AgentBarrier 1.0.0"
git push origin v1.0.0
```

Rebuild from the clean tagged revision and rerun the candidate check. Upload only that directory;
never use the repository's general `dist/` directory because it may contain older versions.

```bash
uv build --out-dir build/release
uv run twine check build/release/*
uv run twine upload --repository pypi build/release/*
```

Immediately verify that PyPI exposes the exact same filenames and hashes:

```bash
uv run python tools/check_pypi_release.py \
  --project agentbarrier \
  --version 1.0.0 \
  --dist-dir build/release \
  --github-output build/release/pypi-state \
  --require-published
```

Only after that verification succeeds, publish the GitHub release and attach the same artifacts:

```bash
gh release create v1.0.0 \
  build/release/agentbarrier-1.0.0-py3-none-any.whl \
  build/release/agentbarrier-1.0.0.tar.gz \
  --verify-tag \
  --title "AgentBarrier 1.0.0" \
  --notes-file docs/releases/1.0.0.md
```

The `Verify PyPI release` workflow rebuilds the tag without a publishing credential and compares
its artifact set and SHA-256 hashes with PyPI. Review that workflow to completion, then install the
public wheel into a fresh environment and run `agentbarrier --version` and the installed-wheel
runtime audit.

## Failure handling

PyPI files are immutable. Never replace or silently work around a mismatched upload. If publication
partly succeeds, preserve the logs and hashes, finish verifying the public artifacts, and publish a
patch release when code or metadata must change. Yank a release only for a material defect and
document the reason; yanking does not delete its files.

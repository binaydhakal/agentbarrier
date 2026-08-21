from __future__ import annotations

from pathlib import Path

import pytest

from agentbarrier.service.auth import (
    AuthenticationError,
    Principal,
    StaticBearerAuth,
    hash_bearer_token,
)

REVIEWER_TOKEN = "reviewer-token-0123456789"
READER_TOKEN = "reader-token-012345678901"


def auth_mapping() -> dict[str, object]:
    return {
        "version": "1",
        "tokens": [
            {
                "subject": "reviewer@example.com",
                "token_sha256": hash_bearer_token(REVIEWER_TOKEN),
                "scopes": ["actions:read", "actions:decide", "audit:read"],
            },
            {
                "subject": "read-only-service",
                "token_sha256": hash_bearer_token(READER_TOKEN).upper(),
                "scopes": ["actions:read"],
            },
        ],
    }


def test_static_bearer_auth_authenticates_digest_and_exact_scopes(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    import json

    path.write_text(json.dumps(auth_mapping()), encoding="utf-8")
    auth = StaticBearerAuth.from_file(path)

    reviewer = auth.authenticate(f"Bearer {REVIEWER_TOKEN}")
    reader = auth.authenticate(f"bearer {READER_TOKEN}")
    assert reviewer.subject == "reviewer@example.com"
    assert reviewer.scopes == frozenset({"actions:read", "actions:decide", "audit:read"})
    assert reader.subject == "read-only-service"
    StaticBearerAuth.require_scope(reader, "actions:read")
    with pytest.raises(AuthenticationError) as denied:
        StaticBearerAuth.require_scope(reader, "actions:decide")
    assert denied.value.status_code == 403
    assert denied.value.code == "insufficient_scope"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic abcdefghijklmnop",
        "Bearer short",
        "Bearer wrong-token-0123456789",
        "Bearer token with spaces 1234",
        "Bearer unicode-token-0123456-☃",
        f"Bearer {'x' * 513}",
    ],
)
def test_static_bearer_auth_rejects_malformed_or_unknown_tokens(
    authorization: str | None,
) -> None:
    auth = StaticBearerAuth.from_mapping(auth_mapping())
    with pytest.raises(AuthenticationError) as raised:
        auth.authenticate(authorization)
    assert raised.value.status_code == 401
    assert raised.value.code in {"missing_bearer_token", "invalid_bearer_token"}


@pytest.mark.parametrize(
    ("document", "error", "message"),
    [
        ([], TypeError, "JSON object"),
        ({"version": "3", "tokens": []}, ValueError, "version"),
        ({"version": "1", "tokens": "bad"}, TypeError, "list"),
        ({"version": "1", "tokens": []}, ValueError, "at least one"),
        ({"version": "1", "tokens": ["bad"]}, TypeError, "object"),
        (
            {
                "version": "1",
                "tokens": [{"subject": 1, "token_sha256": "0" * 64, "scopes": []}],
            },
            TypeError,
            "strings",
        ),
        (
            {
                "version": "1",
                "tokens": [{"subject": "x", "token_sha256": "bad", "scopes": []}],
            },
            ValueError,
            "64 hexadecimal",
        ),
        (
            {
                "version": "1",
                "tokens": [
                    {
                        "subject": "x",
                        "token_sha256": "0" * 64,
                        "scopes": ["unknown"],
                    }
                ],
            },
            ValueError,
            "unknown",
        ),
        (
            {
                "version": "1",
                "tokens": [
                    {
                        "subject": "x",
                        "token_sha256": "0" * 64,
                        "scopes": ["actions:read", "actions:read"],
                    }
                ],
            },
            ValueError,
            "duplicates",
        ),
        ({"version": "1", "tokens": [], "extra": True}, ValueError, "unknown"),
    ],
)
def test_static_bearer_auth_rejects_malformed_configuration(
    document: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        StaticBearerAuth.from_mapping(document)  # type: ignore[arg-type]


def test_static_bearer_auth_rejects_duplicate_digests_and_bad_principals() -> None:
    document = auth_mapping()
    tokens = document["tokens"]
    assert isinstance(tokens, list)
    duplicate = dict(tokens[0])
    duplicate["subject"] = "another"
    tokens.append(duplicate)
    with pytest.raises(ValueError, match="unique"):
        StaticBearerAuth.from_mapping(document)

    with pytest.raises(ValueError, match="subject"):
        Principal("", frozenset())
    with pytest.raises(ValueError, match="control"):
        Principal("bad\nsubject", frozenset())
    with pytest.raises(ValueError, match="16 to 512"):
        hash_bearer_token("short")


def test_static_bearer_auth_accepts_mcp_call_scope() -> None:
    token = "mcp-gateway-token-0123456789"
    auth = StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "mcp-client",
                    "token_sha256": hash_bearer_token(token),
                    "scopes": ["mcp:call"],
                }
            ],
        }
    )
    principal = auth.authenticate(f"Bearer {token}")
    StaticBearerAuth.require_scope(principal, "mcp:call")

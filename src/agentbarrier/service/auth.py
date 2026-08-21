"""Static, scoped bearer authentication for AgentBarrier service endpoints."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import NoReturn, cast

ALL_SERVICE_SCOPES = frozenset({"actions:read", "actions:decide", "audit:read", "mcp:call"})
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DUMMY_DIGEST = "0" * 64


def hash_bearer_token(token: str) -> str:
    """Return the SHA-256 value stored in an AgentBarrier service auth file."""

    _validate_presented_token(token)
    return sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated service identity and its exact endpoint scopes."""

    subject: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.subject.strip() or len(self.subject) > 128:
            raise ValueError("principal subject must contain 1 to 128 non-whitespace characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.subject):
            raise ValueError("principal subject must not contain control characters")
        unknown = self.scopes - ALL_SERVICE_SCOPES
        if unknown:
            raise ValueError(f"unknown AgentBarrier service scopes: {', '.join(sorted(unknown))}")

    def has_scope(self, scope: str) -> bool:
        """Return whether this identity carries one exact scope."""

        return scope in self.scopes


@dataclass(frozen=True, slots=True)
class _Credential:
    principal: Principal
    token_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if _DIGEST_PATTERN.fullmatch(self.token_digest) is None:
            raise ValueError("token_sha256 must be exactly 64 hexadecimal characters")
        object.__setattr__(self, "token_digest", self.token_digest.lower())


class AuthenticationError(Exception):
    """Internal bearer-auth failure with a safe client response shape."""

    def __init__(self, *, code: str, message: str, status_code: int, challenge: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.challenge = challenge


class StaticBearerAuth:
    """Authenticate tokens against precomputed SHA-256 digests in constant time."""

    def __init__(self, credentials: Sequence[_Credential]) -> None:
        if not credentials:
            raise ValueError("AgentBarrier service auth must configure at least one token")
        if len(credentials) > 100:
            raise ValueError("AgentBarrier service auth supports at most 100 static tokens")
        digests = [credential.token_digest for credential in credentials]
        if len(digests) != len(set(digests)):
            raise ValueError("token_sha256 values must be unique")
        self._credentials = tuple(credentials)

    @classmethod
    def from_file(cls, path: str | Path) -> StaticBearerAuth:
        """Load a strict versioned JSON auth file containing token digests."""

        data: object = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("AgentBarrier service auth document must be a JSON object")
        return cls.from_mapping(cast(Mapping[str, object], data))

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> StaticBearerAuth:
        """Parse and validate a strict auth mapping."""

        if not isinstance(data, Mapping):
            raise TypeError("AgentBarrier service auth document must be a JSON object")
        _validate_keys(data, {"version", "tokens"}, label="auth document")
        if data.get("version") != "1":
            raise ValueError("AgentBarrier service auth version must be '1'")
        raw_tokens = data.get("tokens")
        if not isinstance(raw_tokens, Sequence) or isinstance(raw_tokens, (str, bytes)):
            raise TypeError("AgentBarrier service auth tokens must be a list")
        credentials = tuple(cls._parse_credential(item) for item in raw_tokens)
        return cls(credentials)

    @staticmethod
    def _parse_credential(value: object) -> _Credential:
        if not isinstance(value, Mapping):
            raise TypeError("each AgentBarrier service token must be an object")
        item = cast(Mapping[str, object], value)
        _validate_keys(item, {"subject", "token_sha256", "scopes"}, label="token")
        subject = item.get("subject")
        digest = item.get("token_sha256")
        raw_scopes = item.get("scopes")
        if not isinstance(subject, str) or not isinstance(digest, str):
            raise TypeError("token subject and token_sha256 must be strings")
        if not isinstance(raw_scopes, Sequence) or isinstance(raw_scopes, (str, bytes)):
            raise TypeError("token scopes must be a list")
        if any(not isinstance(scope, str) for scope in raw_scopes):
            raise TypeError("every token scope must be a string")
        scopes = frozenset(cast(Sequence[str], raw_scopes))
        if len(scopes) != len(raw_scopes):
            raise ValueError("token scopes must not contain duplicates")
        return _Credential(
            principal=Principal(subject=subject, scopes=scopes),
            token_digest=digest,
        )

    def authenticate(self, authorization: str | None) -> Principal:
        """Authenticate one RFC 6750-style Authorization header."""

        if authorization is None:
            raise AuthenticationError(
                code="missing_bearer_token",
                message="a bearer token is required",
                status_code=401,
                challenge='Bearer realm="agentbarrier"',
            )
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            self._invalid_token()
        try:
            _validate_presented_token(token)
        except (TypeError, ValueError):
            self._invalid_token()
        digest = sha256(token.encode("ascii")).hexdigest()
        matched: Principal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(digest, credential.token_digest):
                matched = credential.principal
        hmac.compare_digest(digest, _DUMMY_DIGEST)
        if matched is None:
            self._invalid_token()
        return matched

    @staticmethod
    def require_scope(principal: Principal, scope: str) -> None:
        """Raise a standards-shaped error when an identity lacks one scope."""

        if not principal.has_scope(scope):
            raise AuthenticationError(
                code="insufficient_scope",
                message="the bearer token does not grant the required scope",
                status_code=403,
                challenge=f'Bearer error="insufficient_scope", scope="{scope}"',
            )

    @staticmethod
    def _invalid_token() -> NoReturn:
        raise AuthenticationError(
            code="invalid_bearer_token",
            message="the bearer token is invalid",
            status_code=401,
            challenge='Bearer realm="agentbarrier", error="invalid_token"',
        )


def _validate_presented_token(token: str) -> None:
    if not isinstance(token, str):
        raise TypeError("bearer token must be a string")
    if not 16 <= len(token) <= 512:
        raise ValueError("bearer token must contain 16 to 512 characters")
    if not token.isascii() or _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("bearer token contains invalid characters")


def _validate_keys(value: Mapping[str, object], allowed: set[str], *, label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings")
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"unknown {label} keys: {', '.join(unknown)}")

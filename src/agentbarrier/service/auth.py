"""Static, scoped bearer authentication for AgentBarrier service endpoints."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TypeVar, cast

from agentbarrier.models import Decision
from agentbarrier.runtime.models import DecisionAuthorization

if TYPE_CHECKING:
    from agentbarrier.runtime.models import RuntimeAction

ALL_SERVICE_SCOPES = frozenset({"actions:read", "actions:decide", "audit:read", "mcp:call"})
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DUMMY_DIGEST = "0" * 64
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDENTITY_KINDS = frozenset({"user", "service"})
_AuthItem = TypeVar("_AuthItem", bound="_Organization | _Role")


def hash_bearer_token(token: str) -> str:
    """Return the SHA-256 value stored in an AgentBarrier service auth file."""

    _validate_presented_token(token)
    return sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated service identity and its exact endpoint scopes."""

    subject: str
    scopes: frozenset[str]
    organization_id: str | None = None
    kind: str = "legacy"
    roles: frozenset[str] = frozenset()
    namespaces: frozenset[str] = frozenset()
    decisions: frozenset[Decision] = frozenset()
    require_separate_approver: bool = False

    def __post_init__(self) -> None:
        if not self.subject.strip() or len(self.subject) > 128:
            raise ValueError("principal subject must contain 1 to 128 non-whitespace characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.subject):
            raise ValueError("principal subject must not contain control characters")
        unknown = self.scopes - ALL_SERVICE_SCOPES
        if unknown:
            raise ValueError(f"unknown AgentBarrier service scopes: {', '.join(sorted(unknown))}")
        if self.organization_id is None:
            if self.kind != "legacy" or self.roles or self.namespaces or self.decisions:
                raise ValueError("legacy principals must not carry organization authorization")
        else:
            _validate_identifier(self.organization_id, name="organization id")
            if self.kind not in _IDENTITY_KINDS:
                raise ValueError("principal kind must be 'user' or 'service'")
            if not self.roles:
                raise ValueError("organization principals must carry at least one role")
            if not self.namespaces:
                raise ValueError("organization principals must carry at least one namespace")
            if ("actions:decide" in self.scopes) != bool(self.decisions):
                raise ValueError(
                    "organization principals must carry actions:decide exactly when they carry "
                    "approval decisions"
                )

    def has_scope(self, scope: str) -> bool:
        """Return whether this identity carries one exact scope."""

        return scope in self.scopes

    def can_access_action(self, action: RuntimeAction) -> bool:
        """Return whether this identity may discover one runtime action."""

        return self.organization_id is None or (
            action.organization_id == self.organization_id and action.namespace in self.namespaces
        )

    def decision_authorization(self) -> DecisionAuthorization:
        """Build the immutable constraints enforced by the runtime store."""

        if self.organization_id is None:
            raise ValueError("legacy principals do not carry decision authorization")
        return DecisionAuthorization(
            actor=self.subject,
            organization_id=self.organization_id,
            namespaces=self.namespaces,
            decisions=self.decisions,
            require_separate_approver=self.require_separate_approver,
            reviewer_subject=self.subject,
        )


@dataclass(frozen=True, slots=True)
class _Organization:
    identifier: str
    namespaces: frozenset[str]
    require_separate_approver: bool


@dataclass(frozen=True, slots=True)
class _Role:
    identifier: str
    scopes: frozenset[str]
    decisions: frozenset[Decision]


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
        version = data.get("version")
        if version == "2":
            return cls._from_v2(data)
        _validate_keys(data, {"version", "tokens"}, label="auth document")
        if version != "1":
            raise ValueError("AgentBarrier service auth version must be '1' or '2'")
        raw_tokens = data.get("tokens")
        if not isinstance(raw_tokens, Sequence) or isinstance(raw_tokens, (str, bytes)):
            raise TypeError("AgentBarrier service auth tokens must be a list")
        credentials = tuple(cls._parse_v1_credential(item) for item in raw_tokens)
        return cls(credentials)

    @staticmethod
    def _parse_v1_credential(value: object) -> _Credential:
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

    @classmethod
    def _from_v2(cls, data: Mapping[str, object]) -> StaticBearerAuth:
        _validate_keys(
            data,
            {"version", "organizations", "roles", "tokens"},
            label="auth document",
        )
        raw_organizations = _require_object_list(data.get("organizations"), name="organizations")
        raw_roles = _require_object_list(data.get("roles"), name="roles")
        raw_tokens = _require_object_list(data.get("tokens"), name="tokens")
        organizations = tuple(cls._parse_organization(item) for item in raw_organizations)
        roles = tuple(cls._parse_role(item) for item in raw_roles)
        organization_map = _unique_by_identifier(organizations, label="organization")
        role_map = _unique_by_identifier(roles, label="role")
        namespace_owners: dict[str, str] = {}
        for organization in organizations:
            for namespace in organization.namespaces:
                owner = namespace_owners.setdefault(namespace, organization.identifier)
                if owner != organization.identifier:
                    raise ValueError(
                        f"namespace {namespace!r} belongs to more than one organization"
                    )
        credentials = tuple(
            cls._parse_v2_credential(item, organizations=organization_map, roles=role_map)
            for item in raw_tokens
        )
        return cls(credentials)

    @staticmethod
    def _parse_organization(value: Mapping[str, object]) -> _Organization:
        _validate_keys(
            value,
            {"id", "namespaces", "require_separate_approver"},
            label="organization",
        )
        identifier = value.get("id")
        if not isinstance(identifier, str):
            raise TypeError("organization id must be a string")
        _validate_identifier(identifier, name="organization id")
        namespaces = _string_set(value.get("namespaces"), name="organization namespaces")
        if not namespaces:
            raise ValueError("organization namespaces must not be empty")
        separation = value.get("require_separate_approver", False)
        if not isinstance(separation, bool):
            raise TypeError("require_separate_approver must be a boolean")
        return _Organization(identifier, namespaces, separation)

    @staticmethod
    def _parse_role(value: Mapping[str, object]) -> _Role:
        _validate_keys(value, {"id", "scopes", "decisions"}, label="role")
        identifier = value.get("id")
        if not isinstance(identifier, str):
            raise TypeError("role id must be a string")
        _validate_identifier(identifier, name="role id")
        scopes = _string_set(value.get("scopes"), name="role scopes")
        unknown = scopes - ALL_SERVICE_SCOPES
        if unknown:
            raise ValueError(f"unknown AgentBarrier service scopes: {', '.join(sorted(unknown))}")
        raw_decisions = _string_set(value.get("decisions"), name="role decisions")
        try:
            decisions = frozenset(Decision(item) for item in raw_decisions)
        except ValueError as error:
            raise ValueError("role decisions must contain only 'approve' or 'reject'") from error
        if ("actions:decide" in scopes) != bool(decisions):
            raise ValueError(
                "roles must grant actions:decide exactly when they grant approval decisions"
            )
        return _Role(identifier, scopes, decisions)

    @staticmethod
    def _parse_v2_credential(
        value: Mapping[str, object],
        *,
        organizations: Mapping[str, _Organization],
        roles: Mapping[str, _Role],
    ) -> _Credential:
        _validate_keys(
            value,
            {"subject", "kind", "organization", "roles", "token_sha256"},
            label="token",
        )
        subject = value.get("subject")
        kind = value.get("kind")
        organization_id = value.get("organization")
        digest = value.get("token_sha256")
        if not all(isinstance(item, str) for item in (subject, kind, organization_id, digest)):
            raise TypeError("token subject, kind, organization, and token_sha256 must be strings")
        assert isinstance(subject, str)
        assert isinstance(kind, str)
        assert isinstance(organization_id, str)
        assert isinstance(digest, str)
        organization = organizations.get(organization_id)
        if organization is None:
            raise ValueError(f"token references unknown organization {organization_id!r}")
        if kind not in _IDENTITY_KINDS:
            raise ValueError("token kind must be 'user' or 'service'")
        role_ids = _string_set(value.get("roles"), name="token roles")
        if not role_ids:
            raise ValueError("token roles must not be empty")
        unknown_roles = role_ids - roles.keys()
        if unknown_roles:
            raise ValueError(f"token references unknown roles: {', '.join(sorted(unknown_roles))}")
        assigned = tuple(roles[item] for item in sorted(role_ids))
        principal = Principal(
            subject=subject,
            scopes=frozenset().union(*(role.scopes for role in assigned)),
            organization_id=organization.identifier,
            kind=kind,
            roles=role_ids,
            namespaces=organization.namespaces,
            decisions=frozenset().union(*(role.decisions for role in assigned)),
            require_separate_approver=organization.require_separate_approver,
        )
        return _Credential(principal=principal, token_digest=digest)

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


def _validate_identifier(value: str, *, name: str) -> None:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{name} must contain 1 to 128 letters, digits, dots, colons, underscores, or hyphens"
        )


def _require_object_list(value: object, *, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"AgentBarrier service auth {name} must be a list")
    if not value:
        raise ValueError(f"AgentBarrier service auth {name} must not be empty")
    if any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"every AgentBarrier service auth {name} entry must be an object")
    return tuple(cast(Mapping[str, object], item) for item in value)


def _string_set(value: object, *, name: str) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a list")
    if any(not isinstance(item, str) for item in value):
        raise TypeError(f"every {name} value must be a string")
    items = cast(Sequence[str], value)
    result = frozenset(items)
    if len(result) != len(items):
        raise ValueError(f"{name} must not contain duplicates")
    for item in result:
        _validate_identifier(item, name=name)
    return result


def _unique_by_identifier(values: Sequence[_AuthItem], *, label: str) -> dict[str, _AuthItem]:
    result: dict[str, _AuthItem] = {}
    for value in values:
        if value.identifier in result:
            raise ValueError(f"duplicate {label} id {value.identifier!r}")
        result[value.identifier] = value
    return result

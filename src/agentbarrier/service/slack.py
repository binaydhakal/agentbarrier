"""Durable Slack approval notifications and identity-bound interactive decisions."""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs

import anyio
import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentbarrier.errors import ApprovalAuthorizationError, InvalidActionState, RuntimeActionError
from agentbarrier.models import Decision
from agentbarrier.runtime import DecisionAuthorization, RuntimeAction, RuntimeStatus, RuntimeStore
from agentbarrier.runtime.models import canonical_json
from agentbarrier.service.api import SecurityHeadersMiddleware

_SLACK_SCHEMA_VERSION = "1"
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKSPACE_ID_PATTERN = re.compile(r"^T[A-Z0-9]{2,63}$")
_APP_ID_PATTERN = re.compile(r"^A[A-Z0-9]{2,63}$")
_CHANNEL_ID_PATTERN = re.compile(r"^[CGD][A-Z0-9]{2,63}$")
_USER_ID_PATTERN = re.compile(r"^[UW][A-Z0-9]{2,63}$")
_MESSAGE_TS_PATTERN = re.compile(r"^[0-9]{1,20}\.[0-9]{1,20}$")
_SLACK_SIGNATURE_PATTERN = re.compile(r"^v0=[0-9a-f]{64}$")
_SAFE_ERROR_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_INTERACTION_BYTES = 64 * 1024
_MAX_ARGUMENT_PREVIEW = 2_400
_MAX_CLOCK_SKEW_SECONDS = 5 * 60
_APPROVE_ACTION_ID = "agentbarrier_approve_v1"
_REJECT_ACTION_ID = "agentbarrier_reject_v1"
_SLACK_API_URL = "https://slack.com/api/"


@dataclass(frozen=True, slots=True)
class SlackReviewer:
    """One exact Slack member mapped to an auditable AgentBarrier subject."""

    user_id: str
    subject: str
    decisions: frozenset[Decision]

    def __post_init__(self) -> None:
        if _USER_ID_PATTERN.fullmatch(self.user_id) is None:
            raise ValueError("Slack reviewer user_id is invalid")
        if not self.subject.strip() or len(self.subject) > 128:
            raise ValueError("Slack reviewer subject must contain 1 to 128 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.subject):
            raise ValueError("Slack reviewer subject must not contain control characters")
        if not self.decisions:
            raise ValueError("Slack reviewer must allow at least one decision")


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Strict Slack app, channel, secret, reviewer, and retry configuration."""

    workspace_id: str
    app_id: str
    channel_id: str
    bot_token: str = field(repr=False)
    bot_token_env: str
    signing_secret: str = field(repr=False)
    signing_secret_env: str
    reviewers: tuple[SlackReviewer, ...]
    timeout_seconds: float = 2
    max_attempts: int = 5
    initial_backoff_seconds: float = 1
    max_backoff_seconds: float = 60
    organization_id: str | None = None
    namespaces: frozenset[str] = frozenset()
    require_separate_approver: bool = False

    def __post_init__(self) -> None:
        if _WORKSPACE_ID_PATTERN.fullmatch(self.workspace_id) is None:
            raise ValueError("Slack workspace_id is invalid")
        if _APP_ID_PATTERN.fullmatch(self.app_id) is None:
            raise ValueError("Slack app_id is invalid")
        if _CHANNEL_ID_PATTERN.fullmatch(self.channel_id) is None:
            raise ValueError("Slack channel_id is invalid")
        for environment_name, environment_value in (
            ("bot_token_env", self.bot_token_env),
            ("signing_secret_env", self.signing_secret_env),
        ):
            if _ENVIRONMENT_NAME_PATTERN.fullmatch(environment_value) is None:
                raise ValueError(f"Slack {environment_name} is invalid")
        if not self.bot_token.startswith("xoxb-") or not 20 <= len(self.bot_token) <= 512:
            raise ValueError("Slack bot token must be a valid xoxb token")
        _validate_secret(self.bot_token, name="bot token")
        if not 32 <= len(self.signing_secret) <= 512:
            raise ValueError("Slack signing secret must contain 32 to 512 characters")
        _validate_secret(self.signing_secret, name="signing secret")
        if not self.reviewers:
            raise ValueError("Slack config must contain at least one reviewer")
        if len(self.reviewers) > 100:
            raise ValueError("Slack config supports at most 100 reviewers")
        identifiers = [reviewer.user_id for reviewer in self.reviewers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Slack reviewer user IDs must be unique")
        if self.organization_id is None:
            if self.namespaces or self.require_separate_approver:
                raise ValueError(
                    "legacy Slack config cannot carry organization authorization settings"
                )
        else:
            if not self.organization_id.strip() or len(self.organization_id) > 128:
                raise ValueError("Slack organization_id must contain 1 to 128 characters")
            if any(
                ord(character) < 32 or ord(character) == 127 for character in self.organization_id
            ):
                raise ValueError("Slack organization_id must not contain control characters")
            if not self.namespaces or any(
                not item.strip()
                or len(item) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in item)
                for item in self.namespaces
            ):
                raise ValueError("Slack namespaces must contain at least one non-empty namespace")
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds >= 3
        ):
            raise ValueError("Slack timeout_seconds must be finite and between 0 and 3")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("Slack max_attempts must be between 1 and 20")
        for number_name, number_value in (
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("max_backoff_seconds", self.max_backoff_seconds),
        ):
            if not math.isfinite(number_value) or number_value <= 0:
                raise ValueError(f"Slack {number_name} must be finite and greater than zero")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("Slack max_backoff_seconds must not be below initial backoff")

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SlackConfig:
        data: object = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("Slack config must be a JSON object")
        return cls.from_mapping(cast(Mapping[str, object], data), environment=environment)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, object],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SlackConfig:
        if not isinstance(data, Mapping):
            raise TypeError("Slack config must be a JSON object")
        _validate_keys(
            data,
            {
                "version",
                "workspace_id",
                "app_id",
                "channel_id",
                "bot_token_env",
                "signing_secret_env",
                "reviewers",
                "timeout_seconds",
                "max_attempts",
                "initial_backoff_seconds",
                "max_backoff_seconds",
                "organization_id",
                "namespaces",
                "require_separate_approver",
            },
            label="Slack config",
        )
        version = data.get("version")
        if version not in {"1", "2"}:
            raise ValueError("Slack config version must be '1' or '2'")
        required = (
            "workspace_id",
            "app_id",
            "channel_id",
            "bot_token_env",
            "signing_secret_env",
        )
        if any(not isinstance(data.get(name), str) for name in required):
            raise TypeError("Slack identifiers and secret environment names must be strings")
        env = os.environ if environment is None else environment
        bot_token_env = cast(str, data["bot_token_env"])
        signing_secret_env = cast(str, data["signing_secret_env"])
        for name in (bot_token_env, signing_secret_env):
            if name not in env:
                raise ValueError(f"Slack secret environment variable {name!r} is not set")
        raw_reviewers = data.get("reviewers")
        if not isinstance(raw_reviewers, Sequence) or isinstance(raw_reviewers, (str, bytes)):
            raise TypeError("Slack reviewers must be a list")
        reviewers = tuple(cls._parse_reviewer(item) for item in raw_reviewers)
        attempts = data.get("max_attempts", 5)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise TypeError("Slack max_attempts must be an integer")
        organization_id: str | None = None
        namespaces: frozenset[str] = frozenset()
        separation = False
        if version == "2":
            raw_organization = data.get("organization_id")
            raw_namespaces = data.get("namespaces")
            raw_separation = data.get("require_separate_approver", False)
            if not isinstance(raw_organization, str):
                raise TypeError("Slack organization_id must be a string")
            if not isinstance(raw_namespaces, Sequence) or isinstance(raw_namespaces, (str, bytes)):
                raise TypeError("Slack namespaces must be a list")
            if any(not isinstance(item, str) for item in raw_namespaces):
                raise TypeError("every Slack namespace must be a string")
            namespaces = frozenset(cast(Sequence[str], raw_namespaces))
            if len(namespaces) != len(raw_namespaces):
                raise ValueError("Slack namespaces must not contain duplicates")
            if not isinstance(raw_separation, bool):
                raise TypeError("Slack require_separate_approver must be a boolean")
            organization_id = raw_organization
            separation = raw_separation
        elif any(
            name in data for name in ("organization_id", "namespaces", "require_separate_approver")
        ):
            raise ValueError("Slack organization authorization requires config version '2'")
        return cls(
            workspace_id=cast(str, data["workspace_id"]),
            app_id=cast(str, data["app_id"]),
            channel_id=cast(str, data["channel_id"]),
            bot_token=env[bot_token_env],
            bot_token_env=bot_token_env,
            signing_secret=env[signing_secret_env],
            signing_secret_env=signing_secret_env,
            reviewers=reviewers,
            organization_id=organization_id,
            namespaces=namespaces,
            require_separate_approver=separation,
            timeout_seconds=_number(data.get("timeout_seconds", 2), name="timeout_seconds"),
            max_attempts=attempts,
            initial_backoff_seconds=_number(
                data.get("initial_backoff_seconds", 1),
                name="initial_backoff_seconds",
            ),
            max_backoff_seconds=_number(
                data.get("max_backoff_seconds", 60),
                name="max_backoff_seconds",
            ),
        )

    @staticmethod
    def _parse_reviewer(value: object) -> SlackReviewer:
        if not isinstance(value, Mapping):
            raise TypeError("each Slack reviewer must be an object")
        item = cast(Mapping[str, object], value)
        _validate_keys(item, {"user_id", "subject", "decisions"}, label="Slack reviewer")
        user_id = item.get("user_id")
        subject = item.get("subject")
        raw_decisions = item.get("decisions", [decision.value for decision in Decision])
        if not isinstance(user_id, str) or not isinstance(subject, str):
            raise TypeError("Slack reviewer user_id and subject must be strings")
        if not isinstance(raw_decisions, Sequence) or isinstance(raw_decisions, (str, bytes)):
            raise TypeError("Slack reviewer decisions must be a list")
        if any(not isinstance(decision, str) for decision in raw_decisions):
            raise TypeError("every Slack reviewer decision must be a string")
        decisions = tuple(Decision(cast(str, decision)) for decision in raw_decisions)
        if len(decisions) != len(set(decisions)):
            raise ValueError("Slack reviewer decisions must not contain duplicates")
        return SlackReviewer(user_id, subject, frozenset(decisions))

    def reviewer(self, user_id: str) -> SlackReviewer | None:
        for reviewer in self.reviewers:
            if hmac.compare_digest(reviewer.user_id, user_id):
                return reviewer
        return None

    def can_access_action(self, action: RuntimeAction) -> bool:
        """Return whether this Slack deployment is authorized for one action."""

        return self.organization_id is None or (
            action.organization_id == self.organization_id and action.namespace in self.namespaces
        )


@dataclass(frozen=True, slots=True)
class SlackNotification:
    """One exclusively claimed outbound Slack notification."""

    action_id: str
    request_digest: str
    channel_id: str
    client_message_id: str
    attempts: int


@dataclass(frozen=True, slots=True)
class SlackNotificationSnapshot:
    """Inspectable Slack notification state without tokens or arguments."""

    action_id: str
    request_digest: str
    channel_id: str
    status: str
    attempts: int
    next_attempt_at_ns: int
    message_ts: str | None
    last_status_code: int | None
    last_error: str | None
    decided_by: str | None
    decision: str | None


class SlackNotificationStore:
    """Durable notification claims, retry state, message bindings, and interaction replay."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        claim_lease_seconds: float = 10,
    ) -> None:
        if not math.isfinite(claim_lease_seconds) or claim_lease_seconds <= 0:
            raise ValueError("Slack claim lease must be finite and greater than zero")
        self.path = str(path)
        self._clock_ns = clock_ns
        self._claim_lease_ns = int(claim_lease_seconds * 1_000_000_000)
        if self._claim_lease_ns < 1:
            raise ValueError("Slack claim lease must be at least one nanosecond")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    @property
    def claim_lease_seconds(self) -> float:
        return self._claim_lease_ns / 1_000_000_000

    def _initialize(self) -> None:
        with self._transaction():
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS slack_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._connection.execute(
                "SELECT value FROM slack_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO slack_metadata (key, value) VALUES ('schema_version', ?)",
                    (_SLACK_SCHEMA_VERSION,),
                )
            elif str(row["value"]) != _SLACK_SCHEMA_VERSION:
                raise RuntimeError(f"unsupported Slack schema version {row['value']!r}")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slack_notifications (
                    action_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at_ns INTEGER NOT NULL,
                    lease_expires_at_ns INTEGER,
                    claimed_by TEXT,
                    message_ts TEXT,
                    last_status_code INTEGER,
                    last_error TEXT,
                    decided_by TEXT,
                    decision TEXT,
                    decided_at_ns INTEGER
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slack_interactions (
                    signature_digest TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    processed_at_ns INTEGER NOT NULL
                )
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def enqueue(self, action: RuntimeAction, *, channel_id: str) -> bool:
        """Enqueue one pending exact action without duplicating an existing binding."""

        client_message_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"https://agentbarrier.dev/slack/{action.action_id}/{action.request_digest}",
            )
        )
        now = self._clock_ns()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM slack_notifications WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["request_digest"]) != action.request_digest
                    or str(row["channel_id"]) != channel_id
                    or str(row["client_message_id"]) != client_message_id
                ):
                    raise ValueError("Slack action notification binding changed")
                return False
            self._connection.execute(
                """
                INSERT INTO slack_notifications (
                    action_id, request_digest, channel_id, client_message_id,
                    status, attempts, next_attempt_at_ns
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?)
                """,
                (action.action_id, action.request_digest, channel_id, client_message_id, now),
            )
        return True

    def claim_due(self, *, worker_id: str) -> SlackNotification | None:
        """Claim one due or abandoned notification for exclusive delivery."""

        if not worker_id.strip():
            raise ValueError("Slack worker_id must not be empty")
        now = self._clock_ns()
        with self._transaction():
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'pending', claimed_by = NULL, lease_expires_at_ns = NULL,
                    next_attempt_at_ns = ?
                WHERE status = 'posting' AND lease_expires_at_ns <= ?
                """,
                (now, now),
            )
            row = self._connection.execute(
                """
                SELECT * FROM slack_notifications
                WHERE status = 'pending' AND next_attempt_at_ns <= ?
                ORDER BY next_attempt_at_ns, action_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'posting', attempts = ?, claimed_by = ?, lease_expires_at_ns = ?
                WHERE action_id = ?
                """,
                (attempts, worker_id, now + self._claim_lease_ns, row["action_id"]),
            )
            return SlackNotification(
                action_id=str(row["action_id"]),
                request_digest=str(row["request_digest"]),
                channel_id=str(row["channel_id"]),
                client_message_id=str(row["client_message_id"]),
                attempts=attempts,
            )

    def mark_posted(
        self,
        notification: SlackNotification,
        *,
        worker_id: str,
        message_ts: str,
        status_code: int,
    ) -> None:
        if _MESSAGE_TS_PATTERN.fullmatch(message_ts) is None:
            raise ValueError("Slack message timestamp is invalid")
        if not 100 <= status_code <= 599:
            raise ValueError("Slack HTTP status code must be between 100 and 599")
        with self._transaction():
            self._require_owned(notification.action_id, worker_id)
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'posted', message_ts = ?, last_status_code = ?, last_error = NULL,
                    claimed_by = NULL, lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (message_ts, status_code, notification.action_id),
            )

    def mark_failed(
        self,
        notification: SlackNotification,
        *,
        worker_id: str,
        config: SlackConfig,
        status_code: int | None,
        error: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> str:
        if _SAFE_ERROR_PATTERN.fullmatch(error) is None:
            raise ValueError("Slack failure error must be a safe code")
        terminal = not retryable or notification.attempts >= config.max_attempts
        status = "dead" if terminal else "pending"
        delay = min(
            config.initial_backoff_seconds * (2 ** (notification.attempts - 1)),
            config.max_backoff_seconds,
        )
        if retry_after_seconds is not None:
            delay = min(max(delay, retry_after_seconds), config.max_backoff_seconds)
        now = self._clock_ns()
        next_attempt = now if terminal else now + int(delay * 1_000_000_000)
        with self._transaction():
            self._require_owned(notification.action_id, worker_id)
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = ?, next_attempt_at_ns = ?, last_status_code = ?, last_error = ?,
                    claimed_by = NULL, lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (status, next_attempt, status_code, error, notification.action_id),
            )
        return status

    def mark_obsolete(
        self,
        notification: SlackNotification,
        *,
        worker_id: str,
        reason: str,
    ) -> None:
        if _SAFE_ERROR_PATTERN.fullmatch(reason) is None:
            raise ValueError("Slack obsolete reason must be a safe code")
        with self._transaction():
            self._require_owned(notification.action_id, worker_id)
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'obsolete', last_error = ?, claimed_by = NULL,
                    lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (reason, notification.action_id),
            )

    def require_message_binding(
        self,
        *,
        action_id: str,
        request_digest: str,
        channel_id: str,
        message_ts: str,
    ) -> SlackNotificationSnapshot:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM slack_notifications WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            raise KeyError("unknown Slack approval notification")
        snapshot = _notification_snapshot(row)
        if (
            not hmac.compare_digest(snapshot.request_digest, request_digest)
            or not hmac.compare_digest(snapshot.channel_id, channel_id)
            or snapshot.message_ts is None
            or not hmac.compare_digest(snapshot.message_ts, message_ts)
        ):
            raise ValueError("Slack interaction does not match the posted approval notification")
        if snapshot.status not in {"posted", "decided"}:
            raise ValueError("Slack approval notification is not interactive")
        return snapshot

    def interaction_outcome(self, signature_digest: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT outcome FROM slack_interactions WHERE signature_digest = ?",
                (signature_digest,),
            ).fetchone()
        return None if row is None else str(row["outcome"])

    def record_interaction(
        self,
        *,
        signature_digest: str,
        action_id: str,
        request_digest: str,
        user_id: str,
        decision: Decision,
        outcome: str,
    ) -> None:
        if not outcome.strip() or len(outcome) > 64:
            raise ValueError("Slack interaction outcome is invalid")
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO slack_interactions (
                    signature_digest, action_id, request_digest, user_id,
                    decision, outcome, processed_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signature_digest,
                    action_id,
                    request_digest,
                    user_id,
                    decision.value,
                    outcome,
                    self._clock_ns(),
                ),
            )

    def mark_decided(
        self,
        *,
        action_id: str,
        request_digest: str,
        decided_by: str,
        decision: Decision,
    ) -> None:
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM slack_notifications WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row["request_digest"]), request_digest):
                raise ValueError("Slack decision notification binding changed")
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'decided', decided_by = ?, decision = ?, decided_at_ns = ?
                WHERE action_id = ?
                """,
                (decided_by, decision.value, self._clock_ns(), action_id),
            )

    def snapshots(self) -> tuple[SlackNotificationSnapshot, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM slack_notifications ORDER BY action_id"
            ).fetchall()
        return tuple(_notification_snapshot(row) for row in rows)

    def retry_dead(self, action_id: str) -> SlackNotificationSnapshot:
        now = self._clock_ns()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM slack_notifications WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Slack notification {action_id!r}")
            if str(row["status"]) != "dead":
                raise ValueError("only a dead Slack notification can be retried")
            self._connection.execute(
                """
                UPDATE slack_notifications
                SET status = 'pending', attempts = 0, next_attempt_at_ns = ?,
                    claimed_by = NULL, lease_expires_at_ns = NULL, last_error = NULL
                WHERE action_id = ?
                """,
                (now, action_id),
            )
            updated = self._connection.execute(
                "SELECT * FROM slack_notifications WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if updated is None:  # pragma: no cover - same transaction and primary key
                raise RuntimeError("Slack notification disappeared while retrying")
            return _notification_snapshot(updated)

    def _require_owned(self, action_id: str, worker_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM slack_notifications WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown Slack notification {action_id!r}")
        if str(row["status"]) != "posting" or str(row["claimed_by"]) != worker_id:
            raise ValueError("Slack notification is not owned by this worker")
        return cast(sqlite3.Row, row)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SlackNotificationStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _notification_snapshot(row: sqlite3.Row) -> SlackNotificationSnapshot:
    return SlackNotificationSnapshot(
        action_id=str(row["action_id"]),
        request_digest=str(row["request_digest"]),
        channel_id=str(row["channel_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        next_attempt_at_ns=int(row["next_attempt_at_ns"]),
        message_ts=str(row["message_ts"]) if row["message_ts"] is not None else None,
        last_status_code=(
            int(row["last_status_code"]) if row["last_status_code"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        decided_by=str(row["decided_by"]) if row["decided_by"] is not None else None,
        decision=str(row["decision"]) if row["decision"] is not None else None,
    )


class SlackAPICaller(Protocol):
    """Async Slack Web API seam for deterministic tests."""

    async def __call__(
        self,
        *,
        method: str,
        payload: Mapping[str, object],
        bot_token: str,
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object], float | None]: ...


class SlackAPIError(Exception):
    """Sanitized Slack API failure suitable for durable retry state."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        safe_code = code if _SAFE_ERROR_PATTERN.fullmatch(code) is not None else "SlackAPIError"
        super().__init__(safe_code)
        self.code = safe_code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class SlackWorker:
    """Find pending approvals and deliver durable, bounded Slack notifications."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore,
        notification_store: SlackNotificationStore,
        config: SlackConfig,
        api_caller: SlackAPICaller | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.notification_store = notification_store
        self.config = config
        self._api_caller = api_caller or _call_slack_api
        self.worker_id = worker_id or str(uuid.uuid4())
        if not self.worker_id.strip():
            raise ValueError("Slack worker_id must not be empty")
        if config.timeout_seconds >= notification_store.claim_lease_seconds:
            raise ValueError("Slack claim lease must be longer than the API timeout")

    def sync_pending(self) -> int:
        inserted = 0
        for action in self.runtime_store.list_actions(status=RuntimeStatus.PENDING):
            if not self.config.can_access_action(action):
                continue
            inserted += int(
                self.notification_store.enqueue(action, channel_id=self.config.channel_id)
            )
        return inserted

    async def run_once(self) -> dict[str, int]:
        counts = {"enqueued": self.sync_pending(), "posted": 0, "retried": 0, "dead": 0}
        while True:
            notification = self.notification_store.claim_due(worker_id=self.worker_id)
            if notification is None:
                return counts
            try:
                action = self.runtime_store.get_action(notification.action_id)
            except KeyError:
                self.notification_store.mark_obsolete(
                    notification,
                    worker_id=self.worker_id,
                    reason="ActionMissing",
                )
                continue
            if not hmac.compare_digest(action.request_digest, notification.request_digest):
                self.notification_store.mark_obsolete(
                    notification,
                    worker_id=self.worker_id,
                    reason="BindingChanged",
                )
                continue
            if action.status is not RuntimeStatus.PENDING:
                self.notification_store.mark_obsolete(
                    notification,
                    worker_id=self.worker_id,
                    reason=f"Status{action.status.value.title()}",
                )
                continue
            payload = build_slack_notification(
                action,
                channel_id=notification.channel_id,
                client_message_id=notification.client_message_id,
            )
            try:
                status_code, result, retry_after = await self._api_caller(
                    method="chat.postMessage",
                    payload=payload,
                    bot_token=self.config.bot_token,
                    timeout_seconds=self.config.timeout_seconds,
                )
                _require_slack_ok(result, status_code=status_code, retry_after=retry_after)
                response_channel = result.get("channel")
                message_ts = result.get("ts")
                if response_channel != notification.channel_id or not isinstance(message_ts, str):
                    raise SlackAPIError(
                        "InvalidPostResponse",
                        status_code=status_code,
                        retryable=False,
                    )
            except SlackAPIError as error:
                outcome = self.notification_store.mark_failed(
                    notification,
                    worker_id=self.worker_id,
                    config=self.config,
                    status_code=error.status_code,
                    error=error.code,
                    retryable=error.retryable,
                    retry_after_seconds=error.retry_after_seconds,
                )
            except Exception as error:
                outcome = self.notification_store.mark_failed(
                    notification,
                    worker_id=self.worker_id,
                    config=self.config,
                    status_code=None,
                    error=_safe_error_code(type(error).__name__),
                    retryable=True,
                )
            else:
                self.notification_store.mark_posted(
                    notification,
                    worker_id=self.worker_id,
                    message_ts=message_ts,
                    status_code=status_code,
                )
                counts["posted"] += 1
                continue
            counts["dead" if outcome == "dead" else "retried"] += 1

    async def run_forever(self, *, poll_interval_seconds: float = 1) -> None:
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("Slack poll interval must be finite and greater than zero")
        while True:
            await self.run_once()
            await anyio.sleep(poll_interval_seconds)


def build_slack_notification(
    action: RuntimeAction,
    *,
    channel_id: str,
    client_message_id: str,
) -> dict[str, object]:
    """Build an accessible Block Kit message bound to one exact action digest."""

    arguments = canonical_json(dict(action.arguments), path="Slack approval arguments")
    metadata = (
        f"Tool: {action.tool_name}\nNamespace: {action.namespace}\n"
        f"Action: {action.action_id}\nPolicy rule: {action.policy_rule}\n"
        f"Request digest: {action.request_digest}"
    )
    metadata_safe = len(metadata) <= 3_000 and all(
        _is_safe_plain_text(value)
        for value in (
            action.tool_name,
            action.namespace,
            action.action_id,
            action.policy_rule,
        )
    )
    binding = json.dumps(
        {"action_id": action.action_id, "request_digest": action.request_digest},
        sort_keys=True,
        separators=(",", ":"),
    )
    interactive = (
        len(arguments) <= _MAX_ARGUMENT_PREVIEW
        and metadata_safe
        and len(action.action_id) <= 128
        and len(binding) <= 2_000
        and re.fullmatch(r"[0-9a-f]{64}", action.request_digest) is not None
    )
    if interactive:
        argument_text = arguments
        guidance = "Review the complete exact arguments below before choosing a decision."
    else:
        argument_text = (
            "This action cannot be represented completely within Slack's message limits. "
            "Review it in the AgentBarrier dashboard or CLI."
        )
        guidance = "Slack decisions are disabled because the complete action is too large."
    summary = (
        f"AgentBarrier approval required. Tool {_truncate_text(action.tool_name, 512)}; "
        f"namespace {_truncate_text(action.namespace, 512)}; "
        f"action {_truncate_text(action.action_id, 512)}; digest {action.request_digest}. "
        f"{guidance} "
        f"Arguments: {argument_text}"
    )
    blocks: list[dict[str, object]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "AgentBarrier approval required"},
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": metadata if metadata_safe else _bounded_metadata(action),
            },
        },
        {
            "type": "section",
            "text": {"type": "plain_text", "text": argument_text},
        },
        {
            "type": "context",
            "elements": [{"type": "plain_text", "text": guidance}],
        },
    ]
    if interactive:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"agentbarrier_{action.action_id}"[:255],
                "elements": [
                    _decision_button(
                        text="Approve",
                        action_id=_APPROVE_ACTION_ID,
                        value=binding,
                        style="primary",
                        title="Approve this exact action?",
                    ),
                    _decision_button(
                        text="Reject",
                        action_id=_REJECT_ACTION_ID,
                        value=binding,
                        style="danger",
                        title="Reject this exact action?",
                    ),
                ],
            }
        )
    return {
        "channel": channel_id,
        "client_msg_id": client_message_id,
        "text": summary[:4_000],
        "mrkdwn": False,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def build_slack_decision_update(action: RuntimeAction) -> dict[str, object]:
    decided_by = _truncate_text(action.decided_by or "unknown reviewer", 512)
    tool_name = _truncate_text(action.tool_name, 512)
    action_id = _truncate_text(action.action_id, 512)
    summary = (
        f"AgentBarrier action {action_id} is {action.status.value}; "
        f"tool {tool_name}; decision recorded by {decided_by}."
    )
    return {
        "text": summary[:4_000],
        "mrkdwn": False,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Action {action.status.value}"},
            },
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": (
                        f"Tool: {tool_name}\nAction: {action_id}\n"
                        f"Status: {action.status.value}\nRecorded by: {decided_by}\n"
                        f"Request digest: {action.request_digest}"
                    ),
                },
            },
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def _decision_button(
    *,
    text: str,
    action_id: str,
    value: str,
    style: str,
    title: str,
) -> dict[str, object]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
        "style": style,
        "accessibility_label": f"{text} this exact AgentBarrier action",
        "confirm": {
            "title": {"type": "plain_text", "text": title},
            "text": {
                "type": "plain_text",
                "text": "The decision is durable and bound to the displayed request digest.",
            },
            "confirm": {"type": "plain_text", "text": text},
            "deny": {"type": "plain_text", "text": "Cancel"},
        },
    }


@dataclass(frozen=True, slots=True)
class _ParsedInteraction:
    signature_digest: str
    user_id: str
    channel_id: str
    message_ts: str
    action_id: str
    request_digest: str
    decision: Decision


class SlackRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class SlackInteractionService:
    """Signed Slack interactivity endpoint over one open runtime and notification store."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore,
        notification_store: SlackNotificationStore,
        config: SlackConfig,
        api_caller: SlackAPICaller | None = None,
        clock_seconds: Callable[[], float] = time.time,
        path: str = "/slack/interactions",
        worker: SlackWorker | None = None,
        poll_interval_seconds: float = 1,
    ) -> None:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Slack interaction path must be absolute without query or fragment")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("Slack poll interval must be finite and greater than zero")
        self.runtime_store = runtime_store
        self.notification_store = notification_store
        self.config = config
        self._api_caller = api_caller or _call_slack_api
        self._clock_seconds = clock_seconds
        self.worker = worker
        self.poll_interval_seconds = poll_interval_seconds

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            if self.worker is None:
                yield
                return
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(self._run_worker)
                yield
                task_group.cancel_scope.cancel()

        app = Starlette(
            debug=False,
            lifespan=lifespan,
            routes=[Route(path, self.handle, methods=["POST"])],
            exception_handlers={
                SlackRequestError: _slack_request_error_handler,
                HTTPException: _slack_http_error_handler,
                Exception: _slack_internal_error_handler,
            },
        )
        app.add_middleware(SecurityHeadersMiddleware)
        self.app = app

    async def _run_worker(self) -> None:
        if self.worker is None:  # pragma: no cover - caller guards before task start
            return
        await self.worker.run_forever(poll_interval_seconds=self.poll_interval_seconds)

    async def handle(self, request: Request) -> Response:
        body = await _read_slack_body(request)
        parsed = _parse_interaction(
            request,
            body=body,
            config=self.config,
            now_seconds=self._clock_seconds(),
        )
        if self.notification_store.interaction_outcome(parsed.signature_digest) is not None:
            return Response(status_code=200)
        try:
            self.notification_store.require_message_binding(
                action_id=parsed.action_id,
                request_digest=parsed.request_digest,
                channel_id=parsed.channel_id,
                message_ts=parsed.message_ts,
            )
        except (KeyError, ValueError) as error:
            raise SlackRequestError(
                409,
                "notification_binding_failed",
                "the Slack message is not bound to this approval",
            ) from error
        reviewer = self.config.reviewer(parsed.user_id)
        if reviewer is None or parsed.decision not in reviewer.decisions:
            self.notification_store.record_interaction(
                signature_digest=parsed.signature_digest,
                action_id=parsed.action_id,
                request_digest=parsed.request_digest,
                user_id=parsed.user_id,
                decision=parsed.decision,
                outcome="unauthorized",
            )
            return Response(
                status_code=200,
                background=BackgroundTask(
                    self._post_ephemeral,
                    parsed,
                    "You are not authorized to make this AgentBarrier decision.",
                ),
            )

        try:
            action = self.runtime_store.get_action(parsed.action_id)
        except KeyError as error:
            raise SlackRequestError(
                409, "action_missing", "the runtime action no longer exists"
            ) from error
        if not hmac.compare_digest(action.request_digest, parsed.request_digest):
            raise SlackRequestError(409, "binding_changed", "the action binding changed")
        if not self.config.can_access_action(action):
            self.notification_store.record_interaction(
                signature_digest=parsed.signature_digest,
                action_id=parsed.action_id,
                request_digest=parsed.request_digest,
                user_id=parsed.user_id,
                decision=parsed.decision,
                outcome="unauthorized",
            )
            return Response(
                status_code=200,
                background=BackgroundTask(
                    self._post_ephemeral,
                    parsed,
                    "You are not authorized to make this AgentBarrier decision.",
                ),
            )
        decided_by = f"slack:{self.config.workspace_id}:{parsed.user_id}:{reviewer.subject}"
        try:
            if self.config.organization_id is None:
                decided = self.runtime_store.decide(
                    parsed.action_id,
                    parsed.decision,
                    decided_by=decided_by,
                    reason="Slack interactive decision",
                )
            else:
                decided = self.runtime_store.decide_authorized(
                    parsed.action_id,
                    parsed.decision,
                    authorization=DecisionAuthorization(
                        actor=decided_by,
                        reviewer_subject=reviewer.subject,
                        organization_id=self.config.organization_id,
                        namespaces=self.config.namespaces,
                        decisions=reviewer.decisions,
                        require_separate_approver=self.config.require_separate_approver,
                    ),
                    reason="Slack interactive decision",
                )
        except ApprovalAuthorizationError:
            self.notification_store.record_interaction(
                signature_digest=parsed.signature_digest,
                action_id=parsed.action_id,
                request_digest=parsed.request_digest,
                user_id=parsed.user_id,
                decision=parsed.decision,
                outcome="unauthorized",
            )
            return Response(
                status_code=200,
                background=BackgroundTask(
                    self._post_ephemeral,
                    parsed,
                    "You are not authorized to make this AgentBarrier decision.",
                ),
            )
        except (InvalidActionState, RuntimeActionError) as error:
            current = (
                error.action
                if isinstance(error, RuntimeActionError)
                else self.runtime_store.get_action(parsed.action_id)
            )
            if current.status in {RuntimeStatus.APPROVED, RuntimeStatus.REJECTED}:
                self.notification_store.mark_decided(
                    action_id=current.action_id,
                    request_digest=current.request_digest,
                    decided_by=current.decided_by or "runtime:unknown",
                    decision=(
                        Decision.APPROVE
                        if current.status is RuntimeStatus.APPROVED
                        else Decision.REJECT
                    ),
                )
            self.notification_store.record_interaction(
                signature_digest=parsed.signature_digest,
                action_id=parsed.action_id,
                request_digest=parsed.request_digest,
                user_id=parsed.user_id,
                decision=parsed.decision,
                outcome=f"stale_{current.status.value}",
            )
            return Response(
                status_code=200,
                background=BackgroundTask(self._update_message, parsed, current),
            )
        recorded_decision = (
            Decision.APPROVE if decided.status is RuntimeStatus.APPROVED else Decision.REJECT
        )
        recorded_by = decided.decided_by or decided_by
        self.notification_store.mark_decided(
            action_id=decided.action_id,
            request_digest=decided.request_digest,
            decided_by=recorded_by,
            decision=recorded_decision,
        )
        self.notification_store.record_interaction(
            signature_digest=parsed.signature_digest,
            action_id=parsed.action_id,
            request_digest=parsed.request_digest,
            user_id=parsed.user_id,
            decision=parsed.decision,
            outcome=decided.status.value,
        )
        return Response(
            status_code=200,
            background=BackgroundTask(self._update_message, parsed, decided),
        )

    async def _update_message(
        self,
        parsed: _ParsedInteraction,
        action: RuntimeAction,
    ) -> None:
        payload = build_slack_decision_update(action)
        payload.update({"channel": parsed.channel_id, "ts": parsed.message_ts})
        await _best_effort_slack_call(
            self._api_caller,
            method="chat.update",
            payload=payload,
            config=self.config,
        )

    async def _post_ephemeral(self, parsed: _ParsedInteraction, text: str) -> None:
        await _best_effort_slack_call(
            self._api_caller,
            method="chat.postEphemeral",
            payload={"channel": parsed.channel_id, "user": parsed.user_id, "text": text},
            config=self.config,
        )


def create_slack_app(
    *,
    runtime_store: RuntimeStore,
    notification_store: SlackNotificationStore,
    config: SlackConfig,
    api_caller: SlackAPICaller | None = None,
    clock_seconds: Callable[[], float] = time.time,
    path: str = "/slack/interactions",
    worker: SlackWorker | None = None,
    poll_interval_seconds: float = 1,
) -> Starlette:
    return SlackInteractionService(
        runtime_store=runtime_store,
        notification_store=notification_store,
        config=config,
        api_caller=api_caller,
        clock_seconds=clock_seconds,
        path=path,
        worker=worker,
        poll_interval_seconds=poll_interval_seconds,
    ).app


def _parse_interaction(
    request: Request,
    *,
    body: bytes,
    config: SlackConfig,
    now_seconds: float,
) -> _ParsedInteraction:
    timestamp = request.headers.get("x-slack-request-timestamp")
    signature = request.headers.get("x-slack-signature")
    verify_slack_signature(
        signing_secret=config.signing_secret,
        timestamp=timestamp,
        signature=signature,
        body=body,
        now_seconds=now_seconds,
    )
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise SlackRequestError(
            415,
            "unsupported_media_type",
            "Slack interactions must use application/x-www-form-urlencoded",
        )
    try:
        encoded = body.decode("utf-8")
        form = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise SlackRequestError(400, "invalid_form", "Slack interaction form is invalid") from error
    if set(form) != {"payload"} or len(form["payload"]) != 1:
        raise SlackRequestError(
            400,
            "invalid_form",
            "Slack interaction form must contain one payload field",
        )
    try:
        raw_payload: object = json.loads(form["payload"][0])
    except json.JSONDecodeError as error:
        raise SlackRequestError(
            400,
            "invalid_payload",
            "Slack interaction payload is not valid JSON",
        ) from error
    if not isinstance(raw_payload, Mapping):
        raise SlackRequestError(
            400,
            "invalid_payload",
            "Slack interaction payload must be an object",
        )
    payload = cast(Mapping[str, object], raw_payload)
    if payload.get("type") != "block_actions":
        raise SlackRequestError(400, "unsupported_interaction", "only block actions are supported")
    if payload.get("api_app_id") != config.app_id:
        raise SlackRequestError(403, "wrong_app", "Slack interaction app does not match")
    workspace_id = _nested_identifier(payload, "team", "id")
    if not hmac.compare_digest(workspace_id, config.workspace_id):
        raise SlackRequestError(403, "wrong_workspace", "Slack workspace does not match")
    channel_id = _nested_identifier(payload, "channel", "id")
    if not hmac.compare_digest(channel_id, config.channel_id):
        raise SlackRequestError(403, "wrong_channel", "Slack channel does not match")
    user_id = _nested_identifier(payload, "user", "id")
    message_ts = _nested_identifier(payload, "message", "ts")
    if (
        _USER_ID_PATTERN.fullmatch(user_id) is None
        or _MESSAGE_TS_PATTERN.fullmatch(message_ts) is None
    ):
        raise SlackRequestError(400, "invalid_identity", "Slack interaction identity is invalid")
    actions = payload.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)) or len(actions) != 1:
        raise SlackRequestError(400, "invalid_action", "Slack interaction must contain one action")
    raw_action = actions[0]
    if not isinstance(raw_action, Mapping):
        raise SlackRequestError(400, "invalid_action", "Slack action must be an object")
    action = cast(Mapping[str, object], raw_action)
    action_identifier = action.get("action_id")
    raw_value = action.get("value")
    if not isinstance(action_identifier, str) or not isinstance(raw_value, str):
        raise SlackRequestError(400, "invalid_action", "Slack button identity is invalid")
    if action_identifier == _APPROVE_ACTION_ID:
        decision = Decision.APPROVE
    elif action_identifier == _REJECT_ACTION_ID:
        decision = Decision.REJECT
    else:
        raise SlackRequestError(400, "unknown_action", "Slack action is not recognized")
    try:
        raw_binding: object = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise SlackRequestError(
            400, "invalid_binding", "Slack action binding is invalid"
        ) from error
    if not isinstance(raw_binding, Mapping) or set(raw_binding) != {
        "action_id",
        "request_digest",
    }:
        raise SlackRequestError(400, "invalid_binding", "Slack action binding is invalid")
    binding = cast(Mapping[str, object], raw_binding)
    action_id = binding.get("action_id")
    request_digest = binding.get("request_digest")
    if (
        not isinstance(action_id, str)
        or not action_id.strip()
        or len(action_id) > 128
        or not isinstance(request_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", request_digest) is None
    ):
        raise SlackRequestError(400, "invalid_binding", "Slack action binding is invalid")
    return _ParsedInteraction(
        signature_digest=sha256(cast(str, signature).encode("ascii")).hexdigest(),
        user_id=user_id,
        channel_id=channel_id,
        message_ts=message_ts,
        action_id=action_id,
        request_digest=request_digest,
        decision=decision,
    )


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now_seconds: float,
) -> None:
    """Verify Slack's v0 HMAC over the untouched body and reject old replay windows."""

    if timestamp is None or signature is None:
        raise SlackRequestError(401, "missing_signature", "Slack signature headers are required")
    if re.fullmatch(r"[0-9]{1,20}", timestamp) is None:
        raise SlackRequestError(401, "invalid_timestamp", "Slack request timestamp is invalid")
    if _SLACK_SIGNATURE_PATTERN.fullmatch(signature) is None:
        raise SlackRequestError(401, "invalid_signature", "Slack request signature is invalid")
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:  # pragma: no cover - guarded by the exact digit pattern
        raise SlackRequestError(
            401, "invalid_timestamp", "Slack request timestamp is invalid"
        ) from error
    if (
        not math.isfinite(now_seconds)
        or abs(now_seconds - timestamp_value) > _MAX_CLOCK_SKEW_SECONDS
    ):
        raise SlackRequestError(401, "stale_request", "Slack request is outside the replay window")
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode("ascii"),
            b"v0:" + timestamp.encode("ascii") + b":" + body,
            sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise SlackRequestError(401, "invalid_signature", "Slack request signature is invalid")


def _nested_identifier(payload: Mapping[str, object], parent: str, child: str) -> str:
    value = payload.get(parent)
    if not isinstance(value, Mapping):
        raise SlackRequestError(400, "invalid_payload", f"Slack payload {parent} is invalid")
    identifier = value.get(child)
    if not isinstance(identifier, str):
        raise SlackRequestError(
            400, "invalid_payload", f"Slack payload {parent}.{child} is invalid"
        )
    return identifier


async def _read_slack_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_INTERACTION_BYTES:
                raise SlackRequestError(413, "body_too_large", "Slack request body is too large")
        except ValueError as error:
            raise SlackRequestError(
                400,
                "invalid_content_length",
                "Content-Length must be an integer",
            ) from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_INTERACTION_BYTES:
            raise SlackRequestError(413, "body_too_large", "Slack request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _call_slack_api(
    *,
    method: str,
    payload: Mapping[str, object],
    bot_token: str,
    timeout_seconds: float,
) -> tuple[int, Mapping[str, object], float | None]:
    if method not in {"chat.postMessage", "chat.update", "chat.postEphemeral"}:
        raise ValueError("Slack API method is not allowed")
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                _SLACK_API_URL + method,
                json=dict(payload),
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
    except httpx.HTTPError as error:
        raise SlackAPIError(type(error).__name__, status_code=None, retryable=True) from error
    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
    try:
        data: object = response.json()
    except ValueError as error:
        raise SlackAPIError(
            "InvalidJSON",
            status_code=response.status_code,
            retryable=response.status_code == 429 or response.status_code >= 500,
            retry_after_seconds=retry_after,
        ) from error
    if not isinstance(data, Mapping):
        raise SlackAPIError(
            "InvalidResponse",
            status_code=response.status_code,
            retryable=response.status_code == 429 or response.status_code >= 500,
            retry_after_seconds=retry_after,
        )
    return response.status_code, cast(Mapping[str, object], data), retry_after


def _require_slack_ok(
    result: Mapping[str, object],
    *,
    status_code: int,
    retry_after: float | None,
) -> None:
    if 200 <= status_code < 300 and result.get("ok") is True:
        return
    raw_code = result.get("error")
    code = raw_code if isinstance(raw_code, str) else f"HTTP{status_code}"
    retryable_codes = {"ratelimited", "internal_error", "fatal_error", "service_unavailable"}
    raise SlackAPIError(
        code,
        status_code=status_code,
        retryable=status_code == 429 or status_code >= 500 or code in retryable_codes,
        retry_after_seconds=retry_after,
    )


async def _best_effort_slack_call(
    caller: SlackAPICaller,
    *,
    method: str,
    payload: Mapping[str, object],
    config: SlackConfig,
) -> None:
    try:
        status_code, result, retry_after = await caller(
            method=method,
            payload=payload,
            bot_token=config.bot_token,
            timeout_seconds=config.timeout_seconds,
        )
        _require_slack_ok(result, status_code=status_code, retry_after=retry_after)
    except Exception:
        return


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


async def _slack_request_error_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    item = cast(SlackRequestError, error)
    return JSONResponse(
        {"error": {"code": item.code, "message": item.message}},
        status_code=item.status_code,
    )


async def _slack_http_error_handler(request: Request, error: Exception) -> JSONResponse:
    del request
    item = cast(HTTPException, error)
    code = "route_not_found" if item.status_code == 404 else "method_not_allowed"
    return JSONResponse({"error": {"code": code}}, status_code=item.status_code)


async def _slack_internal_error_handler(request: Request, error: Exception) -> JSONResponse:
    del request, error
    return JSONResponse(
        {"error": {"code": "internal_error", "message": "Slack request failed closed"}},
        status_code=500,
    )


def _validate_secret(value: str, *, name: str) -> None:
    if not value.isascii() or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ValueError(f"Slack {name} must contain printable non-whitespace ASCII")


def _bounded_metadata(action: RuntimeAction) -> str:
    return (
        f"Tool: {_truncate_text(action.tool_name, 512)}\n"
        f"Namespace: {_truncate_text(action.namespace, 512)}\n"
        f"Action: {_truncate_text(action.action_id, 512)}\n"
        f"Policy rule: {_truncate_text(action.policy_rule, 512)}\n"
        f"Request digest: {action.request_digest}"
    )


def _truncate_text(value: str, maximum: int) -> str:
    normalized = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else "�" for character in value
    )
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1] + "…"


def _is_safe_plain_text(value: str) -> bool:
    return all(ord(character) >= 32 and ord(character) != 127 for character in value)


def _safe_error_code(value: str) -> str:
    return value if _SAFE_ERROR_PATTERN.fullmatch(value) is not None else "SlackAPIError"


def _validate_keys(value: Mapping[str, object], allowed: set[str], *, label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings")
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"unknown {label} keys: {', '.join(unknown)}")


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Slack {name} must be a number")
    return float(value)

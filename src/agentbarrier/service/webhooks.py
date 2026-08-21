"""Durable, signed outbound webhooks for runtime audit events."""

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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import anyio
import httpx

from agentbarrier import __version__
from agentbarrier.runtime import RuntimeAction, RuntimeEvent, RuntimeReceipt, RuntimeStore
from agentbarrier.runtime.models import canonical_json

_WEBHOOK_SCHEMA_VERSION = "1"
_ENDPOINT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|cookie|password|private[_-]?key|secret|token)($|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_NORMALIZED_KEYS = frozenset(
    {
        "apikey",
        "apitoken",
        "accesstoken",
        "authorization",
        "authtoken",
        "clientsecret",
        "cookie",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "token",
    }
)
_REDACTED = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    """Validated endpoint, signing secret, filters, and retry policy."""

    endpoint_id: str
    url: str
    secret: str = field(repr=False)
    secret_env: str
    events: frozenset[RuntimeEvent]
    redact_argument_paths: tuple[str, ...] = ()
    timeout_seconds: float = 10
    max_attempts: int = 5
    initial_backoff_seconds: float = 1
    max_backoff_seconds: float = 60
    start_from: str = "beginning"

    def __post_init__(self) -> None:
        if _ENDPOINT_ID_PATTERN.fullmatch(self.endpoint_id) is None:
            raise ValueError(
                "webhook endpoint id must contain 1 to 64 letters, numbers, dots, dashes, "
                "or underscores"
            )
        _validate_webhook_url(self.url)
        if not self.secret_env.strip():
            raise ValueError("webhook secret_env must not be empty")
        if not 32 <= len(self.secret) <= 512 or not self.secret.isascii():
            raise ValueError("webhook signing secret must contain 32 to 512 ASCII characters")
        if any(ord(character) < 33 or ord(character) > 126 for character in self.secret):
            raise ValueError("webhook signing secret must contain printable non-whitespace ASCII")
        if not self.events:
            raise ValueError("webhook endpoint must select at least one runtime event")
        for path in self.redact_argument_paths:
            _validate_redaction_path(path)
        if len(self.redact_argument_paths) != len(set(self.redact_argument_paths)):
            raise ValueError("webhook redaction paths must not contain duplicates")
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 30
        ):
            raise ValueError("webhook timeout_seconds must be finite and between 0 and 30")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("webhook max_attempts must be between 1 and 20")
        for name, value in (
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("max_backoff_seconds", self.max_backoff_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"webhook {name} must be finite and greater than zero")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("webhook max_backoff_seconds must not be below initial backoff")
        if self.start_from not in {"beginning", "latest"}:
            raise ValueError("webhook start_from must be 'beginning' or 'latest'")

    @property
    def config_digest(self) -> str:
        """Bind persisted delivery state to non-secret endpoint semantics."""

        encoded = json.dumps(
            {
                "endpoint_id": self.endpoint_id,
                "url": self.url,
                "secret_env": self.secret_env,
                "events": sorted(event.value for event in self.events),
                "redact_argument_paths": list(self.redact_argument_paths),
                "timeout_seconds": self.timeout_seconds,
                "max_attempts": self.max_attempts,
                "initial_backoff_seconds": self.initial_backoff_seconds,
                "max_backoff_seconds": self.max_backoff_seconds,
                "start_from": self.start_from,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Strict versioned collection of outbound webhook endpoints."""

    endpoints: tuple[WebhookEndpoint, ...]

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("webhook config must contain at least one endpoint")
        identifiers = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("webhook endpoint ids must be unique")

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> WebhookConfig:
        data: object = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("webhook config must be a JSON object")
        return cls.from_mapping(
            cast(Mapping[str, object], data),
            environment=environment,
        )

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, object],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> WebhookConfig:
        if not isinstance(data, Mapping):
            raise TypeError("webhook config must be a JSON object")
        _validate_keys(data, {"version", "endpoints"}, label="webhook config")
        if data.get("version") != "1":
            raise ValueError("webhook config version must be '1'")
        raw_endpoints = data.get("endpoints")
        if not isinstance(raw_endpoints, Sequence) or isinstance(raw_endpoints, (str, bytes)):
            raise TypeError("webhook endpoints must be a list")
        env = os.environ if environment is None else environment
        return cls(tuple(cls._parse_endpoint(item, env) for item in raw_endpoints))

    @staticmethod
    def _parse_endpoint(value: object, environment: Mapping[str, str]) -> WebhookEndpoint:
        if not isinstance(value, Mapping):
            raise TypeError("each webhook endpoint must be an object")
        item = cast(Mapping[str, object], value)
        _validate_keys(
            item,
            {
                "id",
                "url",
                "secret_env",
                "events",
                "redact_argument_paths",
                "timeout_seconds",
                "max_attempts",
                "initial_backoff_seconds",
                "max_backoff_seconds",
                "start_from",
            },
            label="webhook endpoint",
        )
        endpoint_id = item.get("id")
        url = item.get("url")
        secret_env = item.get("secret_env")
        if not all(isinstance(value, str) for value in (endpoint_id, url, secret_env)):
            raise TypeError("webhook endpoint id, url, and secret_env must be strings")
        endpoint_id = cast(str, endpoint_id)
        url = cast(str, url)
        secret_env = cast(str, secret_env)
        if secret_env not in environment:
            raise ValueError(f"webhook secret environment variable {secret_env!r} is not set")
        raw_events = item.get("events", [event.value for event in RuntimeEvent])
        raw_paths = item.get("redact_argument_paths", [])
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise TypeError("webhook endpoint events must be a list")
        if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
            raise TypeError("webhook endpoint redact_argument_paths must be a list")
        if any(not isinstance(event, str) for event in raw_events):
            raise TypeError("every webhook event must be a string")
        if any(not isinstance(path, str) for path in raw_paths):
            raise TypeError("every webhook redaction path must be a string")
        events = tuple(RuntimeEvent(cast(str, event)) for event in raw_events)
        if len(events) != len(set(events)):
            raise ValueError("webhook endpoint events must not contain duplicates")
        timeout = _number(item.get("timeout_seconds", 10), name="timeout_seconds")
        initial = _number(
            item.get("initial_backoff_seconds", 1),
            name="initial_backoff_seconds",
        )
        maximum = _number(
            item.get("max_backoff_seconds", 60),
            name="max_backoff_seconds",
        )
        attempts = item.get("max_attempts", 5)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise TypeError("webhook max_attempts must be an integer")
        start_from = item.get("start_from", "beginning")
        if not isinstance(start_from, str):
            raise TypeError("webhook start_from must be a string")
        return WebhookEndpoint(
            endpoint_id=endpoint_id,
            url=url,
            secret=environment[secret_env],
            secret_env=secret_env,
            events=frozenset(events),
            redact_argument_paths=tuple(cast(Sequence[str], raw_paths)),
            timeout_seconds=timeout,
            max_attempts=attempts,
            initial_backoff_seconds=initial,
            max_backoff_seconds=maximum,
            start_from=start_from,
        )


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One durable delivery attempt claimed by a worker."""

    delivery_id: str
    endpoint_id: str
    receipt_sequence: int
    event_id: str
    event_type: str
    body: bytes
    attempts: int


@dataclass(frozen=True, slots=True)
class WebhookDeliverySnapshot:
    """Inspectable delivery state without signing secret or request body."""

    delivery_id: str
    endpoint_id: str
    receipt_sequence: int
    event_id: str
    event_type: str
    status: str
    attempts: int
    next_attempt_at_ns: int
    last_status_code: int | None
    last_error: str | None
    delivered_at_ns: int | None


class WebhookDeliveryStore:
    """Concurrency-safe durable webhook checkpoints, claims, and outcomes."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        claim_lease_seconds: float = 60,
    ) -> None:
        if not math.isfinite(claim_lease_seconds) or claim_lease_seconds <= 0:
            raise ValueError("webhook claim_lease_seconds must be finite and greater than zero")
        self.path = str(path)
        self._clock_ns = clock_ns
        self._claim_lease_ns = int(claim_lease_seconds * 1_000_000_000)
        if self._claim_lease_ns < 1:
            raise ValueError("webhook claim_lease_seconds must be at least one nanosecond")
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
        self._initialize()

    @property
    def claim_lease_seconds(self) -> float:
        """Return the exclusive delivery-claim lease duration."""

        return self._claim_lease_ns / 1_000_000_000

    def _initialize(self) -> None:
        with self._transaction():
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS webhook_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._connection.execute(
                "SELECT value FROM webhook_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO webhook_metadata (key, value) VALUES ('schema_version', ?)",
                    (_WEBHOOK_SCHEMA_VERSION,),
                )
            elif str(row["value"]) != _WEBHOOK_SCHEMA_VERSION:
                raise RuntimeError(f"unsupported webhook schema version {row['value']!r}")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_endpoints (
                    endpoint_id TEXT PRIMARY KEY,
                    config_digest TEXT NOT NULL,
                    last_enqueued_sequence INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    endpoint_id TEXT NOT NULL,
                    receipt_sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    body BLOB NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at_ns INTEGER NOT NULL,
                    lease_expires_at_ns INTEGER,
                    claimed_by TEXT,
                    last_status_code INTEGER,
                    last_error TEXT,
                    delivered_at_ns INTEGER,
                    UNIQUE(endpoint_id, receipt_sequence),
                    FOREIGN KEY(endpoint_id) REFERENCES webhook_endpoints(endpoint_id)
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

    def register_endpoint(self, endpoint: WebhookEndpoint, *, current_sequence: int) -> int:
        """Register exact endpoint semantics and return its durable checkpoint."""

        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM webhook_endpoints WHERE endpoint_id = ?",
                (endpoint.endpoint_id,),
            ).fetchone()
            if row is not None:
                if str(row["config_digest"]) != endpoint.config_digest:
                    raise ValueError(
                        f"webhook endpoint {endpoint.endpoint_id!r} changed configuration; "
                        "use a new endpoint id or clear its delivery state intentionally"
                    )
                return int(row["last_enqueued_sequence"])
            checkpoint = current_sequence if endpoint.start_from == "latest" else 0
            self._connection.execute(
                """
                INSERT INTO webhook_endpoints (endpoint_id, config_digest, last_enqueued_sequence)
                VALUES (?, ?, ?)
                """,
                (endpoint.endpoint_id, endpoint.config_digest, checkpoint),
            )
            return checkpoint

    def enqueue(
        self,
        endpoint: WebhookEndpoint,
        events: Sequence[tuple[RuntimeReceipt, bytes]],
        *,
        observed_sequence: int,
    ) -> int:
        """Persist filtered event bodies and advance the endpoint checkpoint atomically."""

        inserted = 0
        now = self._clock_ns()
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM webhook_endpoints WHERE endpoint_id = ?",
                (endpoint.endpoint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown webhook endpoint {endpoint.endpoint_id!r}")
            checkpoint = int(row["last_enqueued_sequence"])
            for receipt, body in events:
                if receipt.sequence <= checkpoint or receipt.event not in endpoint.events:
                    continue
                event_id = f"runtime-receipt-{receipt.sequence}"
                delivery_id = sha256(f"{endpoint.endpoint_id}:{event_id}".encode()).hexdigest()
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO webhook_deliveries (
                        delivery_id, endpoint_id, receipt_sequence, event_id, event_type, body,
                        status, attempts, next_attempt_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (
                        delivery_id,
                        endpoint.endpoint_id,
                        receipt.sequence,
                        event_id,
                        receipt.event.value,
                        body,
                        now,
                    ),
                )
                inserted += int(cursor.rowcount > 0)
            if observed_sequence > checkpoint:
                self._connection.execute(
                    """
                    UPDATE webhook_endpoints SET last_enqueued_sequence = ? WHERE endpoint_id = ?
                    """,
                    (observed_sequence, endpoint.endpoint_id),
                )
        return inserted

    def claim_due(self, *, worker_id: str) -> WebhookDelivery | None:
        """Claim one due or abandoned delivery for bounded exclusive processing."""

        if not worker_id.strip():
            raise ValueError("webhook worker_id must not be empty")
        now = self._clock_ns()
        with self._transaction():
            self._connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', claimed_by = NULL, lease_expires_at_ns = NULL,
                    next_attempt_at_ns = ?
                WHERE status = 'delivering' AND lease_expires_at_ns <= ?
                """,
                (now, now),
            )
            row = self._connection.execute(
                """
                SELECT * FROM webhook_deliveries
                WHERE status = 'pending' AND next_attempt_at_ns <= ?
                ORDER BY receipt_sequence, endpoint_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"]) + 1
            self._connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'delivering', attempts = ?, claimed_by = ?, lease_expires_at_ns = ?
                WHERE delivery_id = ?
                """,
                (attempts, worker_id, now + self._claim_lease_ns, row["delivery_id"]),
            )
            return WebhookDelivery(
                delivery_id=str(row["delivery_id"]),
                endpoint_id=str(row["endpoint_id"]),
                receipt_sequence=int(row["receipt_sequence"]),
                event_id=str(row["event_id"]),
                event_type=str(row["event_type"]),
                body=bytes(row["body"]),
                attempts=attempts,
            )

    def mark_delivered(
        self,
        delivery_id: str,
        *,
        worker_id: str,
        status_code: int,
    ) -> None:
        """Finish one owned delivery successfully."""

        if not 100 <= status_code <= 599:
            raise ValueError("webhook HTTP status code must be between 100 and 599")
        now = self._clock_ns()
        with self._transaction():
            self._require_owned(delivery_id, worker_id)
            self._connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'delivered', delivered_at_ns = ?, last_status_code = ?,
                    last_error = NULL, claimed_by = NULL, lease_expires_at_ns = NULL
                WHERE delivery_id = ?
                """,
                (now, status_code, delivery_id),
            )

    def mark_failed(
        self,
        delivery: WebhookDelivery,
        *,
        worker_id: str,
        endpoint: WebhookEndpoint,
        status_code: int | None,
        error: str,
    ) -> str:
        """Schedule a bounded retry or make an exhausted delivery terminal."""

        if delivery.endpoint_id != endpoint.endpoint_id:
            raise ValueError("webhook delivery and endpoint do not match")
        if status_code is not None and not 100 <= status_code <= 599:
            raise ValueError("webhook HTTP status code must be between 100 and 599")
        if not error.strip() or len(error) > 128:
            raise ValueError("webhook failure error must contain 1 to 128 characters")
        now = self._clock_ns()
        terminal = delivery.attempts >= endpoint.max_attempts
        status = "dead" if terminal else "pending"
        delay_seconds = min(
            endpoint.initial_backoff_seconds * (2 ** (delivery.attempts - 1)),
            endpoint.max_backoff_seconds,
        )
        next_attempt = now if terminal else now + int(delay_seconds * 1_000_000_000)
        with self._transaction():
            self._require_owned(delivery.delivery_id, worker_id)
            self._connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = ?, next_attempt_at_ns = ?, last_status_code = ?, last_error = ?,
                    claimed_by = NULL, lease_expires_at_ns = NULL
                WHERE delivery_id = ?
                """,
                (status, next_attempt, status_code, error, delivery.delivery_id),
            )
        return status

    def snapshots(self) -> tuple[WebhookDeliverySnapshot, ...]:
        """Return delivery status without bodies, URLs, or secrets."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM webhook_deliveries ORDER BY receipt_sequence, endpoint_id"
            ).fetchall()
        return tuple(_snapshot_from_row(row) for row in rows)

    def retry_dead(self, *, endpoint_id: str, event_id: str) -> WebhookDeliverySnapshot:
        """Requeue one exact dead delivery with a fresh bounded attempt budget."""

        if not endpoint_id.strip() or not event_id.strip():
            raise ValueError("webhook endpoint id and event id must not be empty")
        now = self._clock_ns()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT * FROM webhook_deliveries
                WHERE endpoint_id = ? AND event_id = ?
                """,
                (endpoint_id, event_id),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown webhook delivery for endpoint {endpoint_id!r} and event {event_id!r}"
                )
            if str(row["status"]) != "dead":
                raise ValueError("only a dead webhook delivery can be retried manually")
            self._connection.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'pending', attempts = 0, next_attempt_at_ns = ?,
                    claimed_by = NULL, lease_expires_at_ns = NULL, delivered_at_ns = NULL
                WHERE delivery_id = ?
                """,
                (now, row["delivery_id"]),
            )
            updated = self._connection.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
                (row["delivery_id"],),
            ).fetchone()
            if updated is None:  # pragma: no cover - same transaction and primary key
                raise RuntimeError("webhook delivery disappeared while retrying")
            return _snapshot_from_row(updated)

    def _require_owned(self, delivery_id: str, worker_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown webhook delivery {delivery_id!r}")
        if str(row["status"]) != "delivering" or str(row["claimed_by"]) != worker_id:
            raise ValueError("webhook delivery is not owned by this worker")
        return cast(sqlite3.Row, row)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> WebhookDeliveryStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _snapshot_from_row(row: sqlite3.Row) -> WebhookDeliverySnapshot:
    return WebhookDeliverySnapshot(
        delivery_id=str(row["delivery_id"]),
        endpoint_id=str(row["endpoint_id"]),
        receipt_sequence=int(row["receipt_sequence"]),
        event_id=str(row["event_id"]),
        event_type=str(row["event_type"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        next_attempt_at_ns=int(row["next_attempt_at_ns"]),
        last_status_code=(
            int(row["last_status_code"]) if row["last_status_code"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        delivered_at_ns=(
            int(row["delivered_at_ns"]) if row["delivered_at_ns"] is not None else None
        ),
    )


class WebhookSender(Protocol):
    """Async HTTP seam used by the worker and deterministic tests."""

    async def __call__(
        self,
        *,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> int: ...


class WebhookWorker:
    """Poll runtime receipts, enqueue exact bodies, and deliver due events."""

    def __init__(
        self,
        *,
        runtime_store: RuntimeStore,
        delivery_store: WebhookDeliveryStore,
        config: WebhookConfig,
        sender: WebhookSender | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        worker_id: str | None = None,
    ) -> None:
        self.runtime_store = runtime_store
        self.delivery_store = delivery_store
        self.config = config
        self._endpoints = {endpoint.endpoint_id: endpoint for endpoint in config.endpoints}
        self._sender = sender or _send_http
        self._clock_ns = clock_ns
        self.worker_id = worker_id or str(uuid.uuid4())
        if not self.worker_id.strip():
            raise ValueError("webhook worker_id must not be empty")
        for endpoint in config.endpoints:
            if endpoint.timeout_seconds >= delivery_store.claim_lease_seconds:
                raise ValueError("webhook claim lease must be longer than every endpoint timeout")

    def sync_outbox(self) -> int:
        """Snapshot unseen runtime receipts into per-endpoint durable deliveries."""

        receipts = self.runtime_store.receipts()
        observed = receipts[-1].sequence if receipts else 0
        actions: dict[str, RuntimeAction] = {}
        inserted = 0
        for endpoint in self.config.endpoints:
            checkpoint = self.delivery_store.register_endpoint(
                endpoint,
                current_sequence=observed,
            )
            selected: list[tuple[RuntimeReceipt, bytes]] = []
            for receipt in receipts:
                if receipt.sequence <= checkpoint or receipt.event not in endpoint.events:
                    continue
                action = actions.get(receipt.action_id)
                if action is None:
                    action = self.runtime_store.get_action(receipt.action_id)
                    actions[receipt.action_id] = action
                selected.append((receipt, build_webhook_body(receipt, action, endpoint)))
            inserted += self.delivery_store.enqueue(
                endpoint,
                selected,
                observed_sequence=observed,
            )
        return inserted

    async def run_once(self) -> dict[str, int]:
        """Enqueue unseen events and process every delivery currently due."""

        counts = {"enqueued": self.sync_outbox(), "delivered": 0, "retried": 0, "dead": 0}
        while True:
            delivery = self.delivery_store.claim_due(worker_id=self.worker_id)
            if delivery is None:
                return counts
            endpoint = self._endpoints[delivery.endpoint_id]
            timestamp = str(self._clock_ns() // 1_000_000_000)
            headers = signature_headers(
                endpoint,
                body=delivery.body,
                event_id=delivery.event_id,
                timestamp=timestamp,
            )
            try:
                status_code = await self._sender(
                    url=endpoint.url,
                    body=delivery.body,
                    headers=headers,
                    timeout_seconds=endpoint.timeout_seconds,
                )
            except Exception as error:
                outcome = self.delivery_store.mark_failed(
                    delivery,
                    worker_id=self.worker_id,
                    endpoint=endpoint,
                    status_code=None,
                    error=type(error).__name__[:128],
                )
            else:
                if 200 <= status_code < 300:
                    self.delivery_store.mark_delivered(
                        delivery.delivery_id,
                        worker_id=self.worker_id,
                        status_code=status_code,
                    )
                    counts["delivered"] += 1
                    continue
                outcome = self.delivery_store.mark_failed(
                    delivery,
                    worker_id=self.worker_id,
                    endpoint=endpoint,
                    status_code=status_code,
                    error="HTTPError",
                )
            counts["dead" if outcome == "dead" else "retried"] += 1

    async def run_forever(self, *, poll_interval_seconds: float = 1) -> None:
        """Continuously process the durable outbox until cancelled."""

        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("webhook poll interval must be finite and greater than zero")
        while True:
            await self.run_once()
            await anyio.sleep(poll_interval_seconds)


def build_webhook_body(
    receipt: RuntimeReceipt,
    action: RuntimeAction,
    endpoint: WebhookEndpoint,
) -> bytes:
    """Build one canonical, redacted CloudEvents-shaped webhook body."""

    arguments = _redact_arguments(dict(action.arguments), endpoint.redact_argument_paths)
    payload: dict[str, Any] = {
        "specversion": "1.0",
        "id": f"runtime-receipt-{receipt.sequence}",
        "type": f"agentbarrier.runtime.{receipt.event.value}",
        "source": f"agentbarrier://{action.namespace}",
        "subject": action.action_id,
        "time_ns": receipt.timestamp_ns,
        "data": {
            "action": {
                "action_id": action.action_id,
                "namespace": action.namespace,
                "tool_name": action.tool_name,
                "arguments": arguments,
                "request_digest": action.request_digest,
                "policy_version": action.policy_version,
                "policy_rule": action.policy_rule,
                "event_status": _event_status(receipt.event),
            },
            "receipt": {
                "sequence": receipt.sequence,
                "event": receipt.event.value,
                "actor": receipt.actor,
                "detail": receipt.detail,
                "receipt_hash": receipt.receipt_hash,
                "previous_hash": receipt.previous_hash,
            },
        },
    }
    encoded = canonical_json(cast(Any, payload), path="webhook payload")
    return encoded.encode("utf-8")


def signature_headers(
    endpoint: WebhookEndpoint,
    *,
    body: bytes,
    event_id: str,
    timestamp: str,
) -> dict[str, str]:
    """Sign timestamp and byte-exact body using HMAC-SHA256."""

    signed = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(endpoint.secret.encode("ascii"), signed, sha256).hexdigest()
    return {
        "Content-Type": "application/cloudevents+json",
        "User-Agent": f"AgentBarrier-Webhooks/{__version__}",
        "X-AgentBarrier-Event-Id": event_id,
        "X-AgentBarrier-Timestamp": timestamp,
        "X-AgentBarrier-Signature": f"v1={digest}",
    }


async def _send_http(
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> int:
    async with httpx.AsyncClient(follow_redirects=False) as client:
        response = await client.post(
            url,
            content=body,
            headers=dict(headers),
            timeout=timeout_seconds,
        )
        return response.status_code


def _redact_arguments(arguments: dict[str, Any], paths: Sequence[str]) -> dict[str, Any]:
    copied = cast(dict[str, Any], json.loads(json.dumps(arguments, allow_nan=False)))

    def redact_sensitive(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = re.sub(r"[^A-Za-z0-9]", "", str(key)).lower()
                if (
                    _SENSITIVE_KEY_PATTERN.search(str(key))
                    or normalized_key in _SENSITIVE_NORMALIZED_KEYS
                ):
                    value[key] = _REDACTED
                else:
                    redact_sensitive(item)
        elif isinstance(value, list):
            for item in value:
                redact_sensitive(item)

    redact_sensitive(copied)
    for path in paths:
        current: object = copied
        segments = path.split(".")
        for segment in segments[:-1]:
            if not isinstance(current, dict) or segment not in current:
                current = None
                break
            current = current[segment]
        if isinstance(current, dict) and segments[-1] in current:
            current[segments[-1]] = _REDACTED
    return copied


def _event_status(event: RuntimeEvent) -> str:
    return {
        RuntimeEvent.POLICY_ALLOWED: "approved",
        RuntimeEvent.POLICY_DENIED: "denied",
        RuntimeEvent.APPROVAL_REQUESTED: "pending",
        RuntimeEvent.APPROVED: "approved",
        RuntimeEvent.REJECTED: "rejected",
        RuntimeEvent.EXPIRED: "expired",
        RuntimeEvent.EXECUTION_STARTED: "executing",
        RuntimeEvent.EXECUTION_SUCCEEDED: "succeeded",
        RuntimeEvent.EXECUTION_UNKNOWN: "unknown",
        RuntimeEvent.EXECUTION_ABANDONED: "unknown",
        RuntimeEvent.RECONCILIATION_COMMITTED: "succeeded",
        RuntimeEvent.RECONCILIATION_NOT_COMMITTED: "pending_or_approved",
        RuntimeEvent.RESULT_REPLAYED: "succeeded",
    }[event]


def _validate_webhook_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("webhook URL must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("webhook URL must not contain a query string or fragment")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("webhook URL must contain a valid port") from error
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("non-loopback webhook URLs must use HTTPS")


def _validate_redaction_path(path: str) -> None:
    if not path.strip() or any(not segment for segment in path.split(".")):
        raise ValueError("webhook redaction paths must contain non-empty dotted segments")


def _number(value: object, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"webhook {name} must be a number")
    return float(value)


def _validate_keys(value: Mapping[str, object], allowed: set[str], *, label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings")
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"unknown {label} keys: {', '.join(unknown)}")


__all__ = [
    "WebhookConfig",
    "WebhookDelivery",
    "WebhookDeliverySnapshot",
    "WebhookDeliveryStore",
    "WebhookEndpoint",
    "WebhookSender",
    "WebhookWorker",
    "build_webhook_body",
    "signature_headers",
]

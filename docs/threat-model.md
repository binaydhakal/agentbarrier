# Threat model

AgentBarrier has related test, enforcement, protocol, review, and notification boundaries. The test
harness finds lifecycle-control failures using harmless sentinel effects. The Python runtime layer
enforces deterministic policy immediately before a real function crosses a consequential effect
boundary. The MCP gateway applies the same runtime boundary before forwarding a tool call to an
upstream server. The approval API binds decisions to authenticated reviewers, and signed webhooks
export redacted runtime events to external systems. The approval dashboard provides a
server-rendered human review surface over the same authenticated runtime store. The Slack service
delivers exact pending actions and maps signed workspace-member interactions to runtime decisions.

## Security goals

- A gated effect does not commit before its exact tool and arguments are approved.
- Rejection is terminal for the action and its delegated descendants.
- Cancellation and timeout prevent in-flight work from committing later.
- Stable identities prevent duplicate effects across replay and unknown outcomes.
- A pending approval can enforce a strict run-wide barrier over sibling work.
- Decision receipts bind the action identity, tool, and canonical arguments.
- A runtime approval is usable only for the namespace, tool, arguments, idempotency key, and policy
  version that were reviewed.
- Only one worker can claim an approved runtime action, and completed results are replayed without
  executing the protected function again.
- A missing worker or uncertain post-effect result fails closed as `unknown` and is never retried
  automatically.
- Ordered runtime policy rejects malformed values and unknown fields instead of guessing intent.

## Trusted components

For the test harness, the scenario runner, temporary SQLite journal, and sentinel effect are
trusted. The adapter, framework, application middleware, scheduler, persistence implementation,
and cancellation propagation are under test.

For runtime enforcement, the host operating system, Python process, policy file, application wiring,
SQLite or PostgreSQL client library, database location and credentials, reviewer identity supplied
to the local CLI, and downstream idempotency lookup are trusted. The model and model-visible
conversation are not trusted to approve, identify, persist, or reconcile an action.

For the MCP gateway, the official MCP SDK, gateway configuration, upstream target, and configured
idempotency resolver are also trusted. The downstream MCP client, its JSON-RPC request identifier,
tool arguments, and arbitrary request metadata are untrusted. An idempotency value is identity, not
authority: policy still evaluates every exact request.

For the approval API, the static auth file, entropy of the original bearer tokens, TLS or trusted
local ingress, and service operator are trusted. Only token SHA-256 values are stored in the auth
file. Because an unsalted digest does not protect a weak token from offline guessing, operators must
generate high-entropy random tokens and protect the digest file as credential material.

For the approval dashboard, the same auth file and token entropy, the dashboard process, random
session generation, system clock, configured public origin, TLS ingress, browser same-origin and
cookie behavior, and human reviewer are trusted. The original bearer token is verified only at
sign-in; the process then keeps an opaque session digest, identity, scopes, CSRF value, and expiry
in memory.

For outbound webhooks, endpoint configuration, environment-provided signing secrets, worker network
access, system time, HTTP client, and receiver verification are trusted. Endpoint URLs are operator
configuration and can reach the worker's network; they must not be derived from model output. A
valid signature proves possession of the configured shared secret, not that the receiver will apply
an event safely or exactly once.

For Slack approvals, the trusted base additionally includes Slack's request-signing service, the
configured workspace and app, the bot token and signing secret, exact reviewer member IDs, the
private approval channel, HTTPS ingress that preserves the raw request body, and the separate Slack
notification database. Workspace administrators and allowed member sessions are authorization
principals, not untrusted model input.

An application result is meaningful only when the sentinel replaces the production tool at the
same complete-mediation boundary. Replacing a tool earlier tests planning but can miss a later
execution bypass. Replacing it later can allow unsafe work before observation.

Runtime protection is meaningful only when every route to the consequential function uses the
protected wrapper. Code with direct access to the original function, database credentials, payment
client, or runtime database can bypass or tamper with the boundary.

MCP protection is meaningful only when clients cannot reach the upstream server or its credentials
without crossing the gateway. A proxy endpoint is not complete mediation if the original stdio
command, URL, API token, or network route remains available to the agent.

## Runtime controls and residual risks

- Canonical JSON and request digests prevent an approval from being reused with changed bound data.
- SQLite immediate transactions serialize action creation, reviewer decisions, execution claims,
  reconciliation, and receipt insertion across processes.
- PostgreSQL uses a transaction-level advisory lock derived from its dedicated schema to preserve
  the same global invariants across processes. This serializes state-changing transactions within
  one schema and can become a throughput bottleneck; monitor lock waits and transaction latency.
- PostgreSQL schema creation and migrations are explicit. Live services validate the current schema
  and should use a distinct runtime identity without schema ownership or migration privileges. A
  compromised database identity can still bypass controls or recompute receipt chains within the
  rows it can modify.
- Emergency pauses and every matching execution limit are checked inside the execution-claim
  transaction. They stop work that has not started; they cannot revoke an effect that already
  crossed the boundary. Operators must isolate database write access because anyone who can alter
  the control tables can bypass or forge these controls.
- Value limits require a non-negative integer at an operator-configured argument path. Deployments
  must choose meaningful explicit units such as cents and ensure the downstream operation uses the
  same unit. Fixed windows are not rolling windows and may permit bursts across a window boundary.
- Unknown outcomes retain limit capacity because the effect may have committed. Capacity is
  released only after explicit `not_committed` reconciliation; incorrect absence evidence can
  therefore reopen capacity unsafely.
- Execution leases expose abandoned claims. Expiry produces `unknown`, not a retry. A lease is not a
  distributed lock renewal protocol, so long-running tools must choose an appropriate duration and
  operators must verify that an old worker cannot still commit before reconciling as
  `not_committed`.
- Receipt hashes detect accidental edits and unsophisticated tampering. They are not signatures or
  message authentication codes. A process that can rewrite the database can recompute the entire
  chain.
- The local CLI is a single-operator interface. It records the supplied reviewer name but does
  not authenticate that identity or enforce separation of duties. Limit database and shell access
  at the operating-system boundary.
- The database contains tool names, arguments, results, reviewer identities, reasons, and receipts.
  File permissions, encrypted storage, protected backups, retention policy, and secret-free tool
  arguments remain deployment responsibilities.
- Reconciliation trusts external evidence associated with the same business idempotency key. If a
  downstream system cannot prove absence, the action must remain `unknown`.
- Policy order is security-sensitive because the first matching rule wins. Review policy changes,
  use an explicit deny default, assign a new policy version, and validate against the published
  schema before deployment.
- MCP JSON-RPC request IDs are not treated as business idempotency keys. The gateway requires an
  explicit metadata value or configured argument path, binds it to the exact request, and fails
  closed when it is absent or changes meaning.
- The HTTP gateway binds to loopback by default, limits request bodies, and rejects non-loopback
  configuration without scoped bearer authentication. Native static tokens are an application
  boundary, not TLS, rotation, rate limiting, or an identity provider; deployments still require
  trusted ingress and network isolation from the upstream endpoint.
- Upstream bearer credentials are read from a named environment variable, sent only in an
  Authorization header, and never followed across redirects. Remote upstreams require HTTPS;
  plaintext HTTP is restricted to loopback. Process-environment access and destination allow-
  listing remain deployment responsibilities.
- Cancellation and upstream failures after an execution claim become `unknown`. The gateway
  cannot infer whether an external side effect committed from a closed stream or protocol error.
- Approval API reviewer identity comes only from the authenticated token subject. The JSON body
  cannot select or override it, and read, decision, and audit access use distinct exact scopes.
- Version 2 auth assigns exclusive organization namespaces, resolves scopes and decision powers
  from roles, filters action and audit discovery, and can forbid a requester subject from deciding
  its own action. The store repeats organization, namespace, decision, and requester checks inside
  the same transaction that changes action state. Version 1 auth is legacy global-trust mode.
- The API uses bearer headers rather than cookies, emits no permissive cross-origin headers, limits
  decision bodies, and returns no-store responses. It still requires TLS or a trusted local reverse
  proxy whenever traffic leaves loopback.
- The dashboard requires `actions:read` at sign-in and independently enforces `actions:decide` on
  every decision. Every state-changing form requires a random CSRF value; supplied browser origin
  metadata must match the configured public origin. Responses use no-store caching, a restrictive
  content security policy, frame denial, and same-origin isolation headers.
- Dashboard sessions are process-local and retain the scopes present at sign-in. Auth-file changes
  do not immediately revoke an existing session; urgent revocation requires a process restart, and
  deployments should choose a short session TTL. This release supports one dashboard process, not
  a load-balanced session cluster.
- Runtime observability excludes arguments, results, idempotency keys, request digests, people or
  service subjects, decision reasons, exception messages, and organization IDs by default.
  Telemetry hooks are failure-isolated and cannot change action outcomes. Exporters, processors,
  collectors, opt-in dimensions, backend access, sampling, and retention remain operator trust and
  privacy responsibilities; telemetry is not the authoritative audit trail.
- Action arguments, stored results, subjects, and decision reasons are intentionally visible to an
  authorized reviewer and must be treated as sensitive at the browser, proxy, logs, screenshots,
  and endpoint device. HTML escaping limits markup injection but does not make the data non-secret.
- Webhook bodies automatically redact common credential-shaped argument keys and configured dotted
  paths, omit business idempotency keys and execution results, and are signed over exact bytes.
  Application-specific sensitive paths remain an operator responsibility; receipt actors and
  details are audit data and must not contain secrets.
- Webhook delivery is at least once. Stable event IDs support receiver deduplication when a response
  is lost after acceptance. Bounded retries become dead letters, and only an explicit exact
  endpoint/event command grants a new bounded attempt budget.
- HTTPS is required off loopback and redirects are disabled, but a trusted operator can still
  configure an internal or sensitive network target. Isolate worker egress and allow-list receiver
  destinations where the deployment requires SSRF resistance.
- Slack interactions are verified with Slack's `v0` HMAC over the untouched body, bounded to a
  five-minute window, replay-checked by signature, and matched to the configured app, workspace,
  channel, posted message timestamp, action ID, and exact runtime digest. Reviewer identity and
  allowed decisions come only from the local member-ID allowlist. A copied button, forged payload,
  unauthorized member, changed digest, or different message fails closed.
- Exact arguments are visible to configured Slack channel members. Actions too large to display
  completely receive no Slack decision buttons. Use a private channel, restrict membership and
  retention, disable unnecessary integrations, and treat channel history, notifications, exports,
  screenshots, and member devices as sensitive. Slack availability or a failed message update does
  not change the authoritative runtime state.
- Slack notification delivery is at least once with stable client message IDs, crash-recoverable
  claims, bounded retries, and explicit dead-letter recovery. A lost API response may still create
  a duplicate message; only the exact recorded message timestamp is authorized to decide. This
  release uses a separate local SQLite notification database and supports one Slack service host,
  not independently scaled notification workers.
- The OpenAI Agents runtime builder excludes SDK-injected context from policy arguments and requires
  application business identity rather than an SDK tool-call ID. It is complete mediation only if
  every consequential route uses the returned `FunctionTool` and cannot call the original function
  or downstream credential directly.
- The LangGraph runtime builder excludes injected `ToolRuntime` and other schema-hidden values from
  policy arguments and requires application business identity rather than a model tool-call ID.
  Its fail-closed `ToolNode` disables exception-to-message conversion because every exception after
  an execution claim may represent an unknown outcome. Middleware that catches those exceptions or
  any route that calls the original function can bypass that protection.
- The PydanticAI runtime builder excludes injected `RunContext`, requires async callables, disables
  native approval, per-tool timeout, and retries, and translates framework control signals raised
  after a claim into a host-visible runtime failure. Toolset-level timeouts, execution hooks that
  convert exceptions, synchronous or blocking work inside an async function, and direct access to
  the original callable remain application-controlled bypass or late-effect risks.
- The Google ADK runtime builder excludes injected `ToolContext`, requires async non-streaming
  callables, and disables native confirmation. ADK callbacks can mutate arguments, skip execution,
  replace results, or suppress tool exceptions; an `on_tool_error_callback` must return `None` for
  protected-tool failures, and callbacks must never call the consequential operation directly.
  Blocking work hidden inside an async function and direct access to the original callable remain
  application-controlled bypass or late-effect risks.

## Out of scope

AgentBarrier does not:

- detect prompt injection, malware, secrets, or vulnerable dependencies;
- prove that a model will choose safe actions;
- sandbox malicious adapter or application code;
- authorize a real production operation;
- prove behavior outside the configured late-effect observation window;
- turn a failed guarantee into a vulnerability classification without an application threat model;
- sandbox or authenticate local runtime callers;
- stop code that bypasses the protected function wrapper;
- stop an MCP client that can bypass the gateway and call the upstream server directly;
- provide an identity provider, token rotation service, or TLS termination for the approval API or
  approval dashboard, or MCP gateway;
- provide centralized dashboard sessions, immediate session revocation, single sign-on, dynamic
  role administration, or an external identity provider;
- safely resume interactive MCP `InputRequiredResult` rounds after an execution claim;
- make an external webhook receiver trustworthy, available, or exactly once;
- make Slack, a workspace administrator, an allowed member session, or a channel member
  trustworthy, phishing-resistant, or always available;
- invent a reliable business idempotency key from a JSON-RPC request ID;
- secure, sign, encrypt, replicate, or retain the runtime database and its backups;
- authenticate reviewer names supplied to the trusted local CLI;
- guarantee exactly-once behavior in an external system that ignores the business idempotency key;
  or
- prove that an `unknown` effect did not commit without downstream evidence.

The bundled framework probes intentionally use deterministic plans and temporary effects. They
measure minimal framework behavior, not every application configuration.

## Safe operation

Never connect a conformance adapter to a production tool. The supplied `EffectProbe` writes only to
its temporary journal. Run application adapters in an isolated test environment with synthetic
credentials and data.

For runtime use, keep the database and policy outside model-writable locations, restrict them to the
service account, back up before migrations, inspect `database status`, and alert on `unknown`,
invalid receipt chains, rejected binding reuse, and repeated policy denials. Protect downstream
systems with their own authorization and idempotency enforcement as a second boundary.
Use the [production deployment and recovery guide](deployment.md) to exercise restore, upgrade,
rollback, credential rotation, and incident containment before carrying live consequential actions.

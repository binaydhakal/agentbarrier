# Approval dashboard

> The dashboard is under development for AgentBarrier 0.6.0. Its authenticated, single-node
> workflow is available on the main branch. Production deployments still need trusted TLS ingress,
> rate limiting, secret management, and network isolation.

The dashboard is a small server-rendered interface for people who need to inspect and decide exact
AgentBarrier actions without database or shell access. It reads the same runtime store as the
protected Python application or MCP gateway, shows the current emergency-pause and execution-limit
state, and records an authenticated reviewer identity on every decision.

It uses ordinary HTML forms and has no JavaScript dependency. Keyboard navigation, a skip link,
semantic tables and definition lists, explicit labels, visible focus styles, status announcements,
and responsive layouts are included in the supported interface.

## Install

Until 0.6.0 is released, install the optional service dependencies from the main branch:

```bash
python -m pip install 'agentbarrier[service] @ git+https://github.com/binaydhakal/agentbarrier.git'
```

After release, use `python -m pip install 'agentbarrier[service]'` from PyPI.

## Configure reviewer identities

The dashboard and approval API share the strict static auth-file format. Generate a high-entropy
credential, store only its SHA-256 value, and grant the reviewer the minimum scopes it needs. The
[approval API guide](approval-api.md#create-a-strong-bearer-credential) explains credential
generation and the complete file format.

A dashboard identity needs `actions:read` to sign in. Add `actions:decide` only when it may approve
or reject actions:

```json
{
  "version": "1",
  "tokens": [
    {
      "subject": "reviewer@example.com",
      "token_sha256": "REPLACE_WITH_64_HEXADECIMAL_CHARACTERS",
      "scopes": ["actions:read", "actions:decide"]
    },
    {
      "subject": "read-only-reviewer@example.com",
      "token_sha256": "REPLACE_WITH_ANOTHER_64_CHARACTER_VALUE",
      "scopes": ["actions:read"]
    }
  ]
}
```

The credential is submitted once at sign-in and exchanged for a random opaque browser session. The
server retains only the session-token digest, reviewer identity, scopes, CSRF value, and expiry. It
does not retain the original bearer credential or place it in a browser cookie.

## Run locally

Create the runtime database by starting the protected application, MCP gateway, or a database
command first. The dashboard deliberately refuses to create a misspelled or unexpected database.
Then run:

```bash
agentbarrier dashboard \
  --db agentbarrier.db \
  --auth-config approval-auth.json
```

Open `http://127.0.0.1:8788/dashboard/`. The loopback listener uses a non-secure, path-scoped cookie
so local HTTP development works. It is not suitable for remote traffic.

The queue defaults to pending actions and can filter every runtime status. It also shows active
emergency pauses, configured execution-limit windows, current usage, and receipt-chain validity.
Open an action to inspect its exact stored arguments and result before approving or rejecting it.
The authenticated subject—not a form field—becomes `decided_by` in the action and receipt.

## Deploy behind HTTPS

Terminate TLS at a trusted reverse proxy and keep the application listener on a private loopback or
container network. Configure the one exact browser origin and secure host cookies:

```bash
agentbarrier dashboard \
  --db /var/lib/agentbarrier/runtime.db \
  --auth-config /run/secrets/agentbarrier-approval-auth.json \
  --host 127.0.0.1 \
  --port 8788 \
  --public-origin https://approvals.example.com \
  --cookie-secure \
  --session-ttl 3600
```

`--public-origin` must be an origin only: scheme, hostname, and optional port, with no path, query,
credentials, or fragment. Secure cookies require an HTTPS origin. A non-loopback listener is
rejected unless both `--cookie-secure` and `--public-origin` are configured.

At ingress:

1. Allow only the intended team or private network to reach the dashboard.
2. Preserve the `/dashboard` path and do not rewrite form targets to another origin.
3. Rate-limit sign-in failures and decision requests.
4. Do not log request bodies, cookies, credentials, action arguments, results, or decision reasons.
5. Set conservative connection, header, and body timeouts and an overall request limit no larger
   than the dashboard's 16 KiB form limit.
6. Protect the auth file and runtime database from the model, agent tools, and untrusted service
   accounts; encrypt storage and backups according to the data they contain.

The application emits HSTS in secure-cookie mode, a restrictive content security policy, frame
denial, no-store caching, a no-referrer policy, browser permission denial, and cross-origin process
isolation headers. Every state-changing form requires a per-session CSRF value. Supplied `Origin`
or `Referer` metadata is checked against the configured public origin; same-origin opaque browser
origins are accepted only when browser fetch metadata identifies them as same-origin.

## Session behavior and limits

- Sessions are held in process memory and expire after eight hours by default. The maximum TTL is
  seven days. Restarting the process signs everyone out.
- One process supports at most 1,000 simultaneous sessions. There is no shared session backend, so
  do not run multiple dashboard workers behind load balancing in this release.
- Auth-file changes do not revoke an already-created session immediately. Use a short TTL and
  restart the dashboard when urgent revocation is required.
- Read-only reviewers can inspect actions but never receive decision forms, and the decision route
  independently enforces `actions:decide`.
- The queue intentionally displays at most 100 actions per filter. Use the paginated approval API
  for automation or complete historical access.
- The dashboard displays operational controls but does not change pauses or limits. Use the
  authenticated operating environment and `agentbarrier controls` commands for those changes.

This is a secure single-node reviewer surface, not an identity provider. Single sign-on,
organization membership, role administration, separation-of-duty rules, shared sessions, and
immediate centralized revocation remain 1.0 work.

## Security verification

The test suite covers credential exchange without token leakage, scoped authorization, exact HTML
escaping, CSRF and cross-origin failures, session expiry and capacity, strict form parsing, security
headers, error handling, runner configuration, and responsive accessible markup. Package CI also
installs the built wheel into a clean environment and completes a real sign-in → inspect →
approve lifecycle while verifying reviewer binding and the receipt chain.

# Database rollout gate

Use this order for an authorized deployment: **backup → preflight → apply → deploy services**.

1. Take and verify a restorable database backup before changing the schema.
2. With the target DATABASE_URL set, run `python scripts/migration_preflight.py`.
   It is read-only. Pending files, missing required migrations, duplicate check-in tags,
   checksum/lint errors or an inaccessible database produce a non-zero exit: **do not deploy**.
   Duplicate output contains counts only; `--verbose` includes tags for authorized manual repair.
3. Resolve 002's duplicate precondition manually, then apply reviewed migrations with
   `python -m shared.migrate apply`. The first preflight may report pending files; it never applies them.
   A failed later migration can leave earlier files committed, so a partial run is not deployment-ready.
4. Re-run preflight and require exit 0, confirming all files (including 001-004) are applied.
5. Deploy the services only after that successful gate and perform the DEV smoke checks.

`FGT_MIGRATIONS_ENABLED=0` is **not compatible with the new code on an old schema**.
Archive needs 003's `source_record_id`; callback tracking and membership provenance need 004.
`MEMBERSHIP_FLOW=legacy` does not remove those schema requirements.
If startup migrations are disabled operationally, migrations still must be applied and the
preflight must pass before the new writers start. Keep services using the prior release while
repairing migration prerequisites. This document authorizes no deployment or real-data changes.

## eBas transport gate

`EBAS_TRANSPORT` defaults to `stub`: validation, submission and confirmation use a
deterministic in-process fake transport, even if `EBAS_API_KEY` is configured. DEV keeps
this default. Stub results can update local membership state; they do not register anyone
with eBas. Startup logs only `eBas transport: stub` or `eBas transport: live`.

PROD must deliberately set `EBAS_TRANSPORT=live` in the backend service environment at
rollout, with the private eBas configuration and `INTEGRATION_ENGINE=n8n`.
`live` with `INTEGRATION_ENGINE=none` refuses startup. The explicit override
`EBAS_ALLOW_LIVE_WITH_ENGINE_NONE=1` permits real provider access despite that engine
setting and must only be used deliberately; it is not needed for DEV or stub operation.

## nginx validation and graceful reload

Keep the reviewed nginx 1.27.5 image and the existing certificate, htpasswd and
`ADMIN_AUTH_COOKIE_TOKEN` mounts/settings. `nginx.conf` is an envsubst **template**;
only `${ADMIN_AUTH_COOKIE_TOKEN}` is substituted. Do not validate the unrendered
template or substitute nginx's `$host`, `$scheme`, map or upstream variables.
Optional meetup (`meetup:8000`) and member-card (`member-card:8002`) names are resolved
through Docker DNS when requested, so either project may be absent during reload.

Before an authorized rollout, run `bash tests/security_smoke.sh --target isolated`.
It starts only disposable nginx/stub containers on its own network, publishes TLS on
loopback port 18443, creates its own certificate/htpasswd, renders through the official
entrypoint, tests syntax/reload with siblings absent, and removes its resources.
The 200 flow checks prove nginx routing; backend tests enforce `MEMBERSHIP_FLOW`.
The public allowlist is the union for off/legacy/v2, including legacy
`/api/ebas/register`; off mode still permits ordinary `/api/checkin/orchestrate`.
No application env files or provider credentials enter this test.

For an already running gateway, after placing the reviewed template, render the
configuration and require a successful syntax check **before** reloading workers:

```sh
docker compose -f docker-compose.prod.yml exec -T nginx sh -ec \
  '/docker-entrypoint.d/20-envsubst-on-templates.sh; nginx -t'
docker compose -f docker-compose.prod.yml exec -T nginx nginx -s reload
```

Run the second command only if the first exits 0. Existing workers continue serving
the previous configuration on a syntax failure. Restore the previous reviewed template,
render and test it, then reload if rollback is needed. Use the actual deployment's
Compose project selection; never run `compose down` for a config change. Changes to
the image, environment or mounts require a separately authorized container replacement.

For DEV, `nginx.dev.conf` is mounted as the already rendered config: run `nginx -t`
then `nginx -s reload` in that gateway. `bash tests/security_smoke.sh --target dev`
uses only GETs on 127.0.0.1:8088. The conductor's shadow gateway is supported with
`--target http://127.0.0.1:18088`; it mounts the worktree config and uses the existing
DEV applications. These checks never POST to the live-capable DEV backend.
Without an existing OAuth session, the DEV layout may return 302 to
`/admin/auth/login`; the smoke accepts that application response as well as 200.
DEV retains `/admin/` and explicit root Dash/SSE compatibility routes; PROD's public
check-in host denies those paths. Do not copy DEV exceptions to PROD.
DEV event selection keeps its application OAuth gate and requires no nginx Basic
auth or htpasswd mount. PROD Basic/cookie authentication is tested in isolation.

Venue limits use the connection IP, shared by players behind a NAT. Check-in allows
60/minute with burst 20; membership start 10/minute, verify/confirm 20/minute (burst 20),
resend 5/minute (burst 5), and legacy eBas/game writes 10/minute (burst 20). Polling has
its own 30/second budget (burst 120). The hotfix's general `one` zone stays 5/second.
nginx rejects excess public requests with 429 and `Retry-After: 60`; backend-provided
Retry-After headers pass through. Long check-in/membership calls have a 90-second read
timeout. Confirm these venue budgets against observed event traffic before tightening.

Check security headers on 200, 401, 404 and 429 responses. HSTS is TLS-only and CSP
starts in Report-Only mode (browser-console observation, no report collector). Review
Dash/inline-script violations before enabling enforcement. Event selection on the
PROD admin host uses the existing Basic/cookie realm and htpasswd; OAuth login,
callback/finalize, logout and me remain reachable without Basic auth. Internal n8n
callbacks continue using `backend:8000` directly, never the public check-in host.
Bypass-cookie issuance requires a successful 2xx/3xx response on a Basic-gated
route. A failed password, an open OAuth endpoint or SSE must never issue that cookie,
even when an Authorization header supplied a username. Include these negative checks
when reviewing future changes to the authentication maps.

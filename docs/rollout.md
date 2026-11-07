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

PROD binds `nginx.conf` as a **single file** at
`/etc/nginx/templates/default.conf.template`. A Git update may replace the host
file's inode while the running container keeps the old inode. A successful render,
syntax test and reload can then silently keep the old public routes. After placing
the reviewed template, compare these checksums **before rendering**:

```sh
sha256sum nginx.conf
docker compose -p fgt-checkin-system -f docker-compose.prod.yml exec -T nginx \
  sha256sum /etc/nginx/templates/default.conf.template
```

If they differ, reloading cannot pick up the host file. Preserve the current logs
first (below), obtain the user's OK for the brief interruption to all four hosts,
then recreate **only nginx**, keeping the reviewed image, environment and mounts:

```sh
docker compose -p fgt-checkin-system -f docker-compose.prod.yml up -d --no-deps --force-recreate nginx
```

Never run `compose down`. Repeat both checksum commands after recreation and require
an exact match. If the checksums already match, recreation is unnecessary. With a
matching template, render and require a successful syntax check before reloading:

```sh
docker compose -p fgt-checkin-system -f docker-compose.prod.yml exec -T nginx sh -ec \
  '/docker-entrypoint.d/20-envsubst-on-templates.sh; nginx -t'
docker compose -p fgt-checkin-system -f docker-compose.prod.yml exec -T nginx nginx -s reload
```

Run the second command only if the first exits 0. Existing workers continue serving
the previous configuration on a syntax failure. Restore the previous reviewed template,
repeat the host/container checksum gate, render and test it, then reload if rollback
is needed. Changes to the image, environment or mounts require separate authorization.

After every authorized recreation or reload, check the actual public hosts. Require
`https://www.checkin.fgctrollhattan.se/api/checkin/begin` and `/api/notify/update` to
return 404, `/health` to return 200, and meetup/membercard `/` to return 200. On
`https://admin.fgctrollhattan.se/auth/select-event`, require 401 without credentials
and 401 with a deliberately wrong Basic password, with **no** `Set-Cookie:
fgc_admin_auth` header in either response. Inspect only that header's presence;
never print or copy real cookies or credentials. Do not declare the allowlist live
until these checks pass; a successful `nginx -t` alone cannot prove the loaded routes.

Before ANY recreate of nginx/backend on PROD: `docker logs <container> > ~/logs/<container>-<date>.log`
(the json logs are deleted with the container). Preserve those logs before replacing
either container; persistent access logging is a separate follow-up task.

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

Venue limits use the connection IP, shared by players behind a NAT. The event has
15-70 players arriving in waves of 20-30, so size burst capacity for a whole wave:

- HTML pages use their own `pages` zone at 5/second, burst 100. Thirty players can
  request three pages each without consuming a static-asset, write or sibling budget.
  Siblings keep the hotfix's `one` zone at 5/second and their prior burst 30.
- DEV Dash, auth, assets, hot reload and SSE connections use a separate generous
  `dev_admin` zone at 50/second, burst 300. Public page rushes cannot consume it.
- Static assets have their own 50/second zone, burst 300. One cold page load needs
  about ten assets: 30 players mean 300 assets plus 30 HTML requests (330 total in
  about 20 seconds). Assets therefore cannot exhaust the HTML bucket.
- Orchestrate allows 60/minute with burst 40. Game saves have a separate zone at
  60/minute, burst 40: a legacy-registration probe cannot consume their quota.
- Legacy eBas registration stays strict at 10/minute, burst 30. It accommodates
  a wave of 30, while 100 registration attempts across a minute exceed the budget.
- Membership start stays 10/minute, verify/confirm 20/minute, each with burst 40 so
  30 first-time signups can start and verify together. Resend stays strict at
  5/minute (burst 5). Polling keeps its own 30/second budget, burst 120; 30 players
  polling one URL every ten seconds need about 3/second (three URLs need 9/second).

`tests/test_nginx_allowlist.py` models one source IP doing 780 requests: three HTML
pages, ten assets, orchestrate, v2 start/verify, a game save and three polling URLs at
0/10/20 seconds for each of 30 players. It requires zero rejections. A second model spaces 100 legacy
registrations evenly across 60 seconds and requires rejections while game saves work.
The isolated smoke also runs the 30 players concurrently from one container/IP,
requires all 780 responses to be 200 within 30 seconds, then probes legacy registration
and verifies independent game capacity. This proves gateway admission against stubs;
it does not measure backend/provider throughput or authorize requests to live providers.
nginx excess public requests return 429 and `Retry-After: 60`; backend Retry-After
headers pass through. Long check-in/membership calls have a 90-second read timeout.
Confirm steady-state budgets against observed venue traffic before tightening.

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

## Persistent nginx logs and rotation

The nginx service in both Compose files binds `./logs/nginx` to
`/var/log/nginx/fgt`. Provision that private directory before the separately authorized
container replacement that adds the mount. Give nginx's worker traversal rights:
the pinned image uses uid/gid 101, so provision with
`install -d -m 0750 -o 101 -g 101 logs/nginx` as root (verify `id nginx` if the image
changes). A root-owned 0750 directory would prevent worker reopening. A reload
cannot add a bind mount. The files remain on the host when the container is replaced;
they are ignored by git. Keep the existing pre-recreate backup gate during migration.

`access.log` is JSON with time, an IPv4 /24 prefix, host, method, canonical URI, route
group, status, body bytes, request time, upstream status/time and nginx limit status.
IPv6 is fully redacted. Query strings, cookies, credentials, names, attempt IDs and
confirmation capabilities are absent. Sensitive path segments become `*`; unknown
paths become `/[other]`. `error.log` receives the same sanitized JSON for 5xx responses,
plus native emergency diagnostics only. Native warnings/errors stay on stderr for
live debugging; those ephemeral Docker diagnostics can contain full IPs and URLs.
Never copy them into the persistent JSON files. `REJECTED` identifies nginx throttling;
an upstream 429 with a different limit status is an application response.

For nginx after this change, refine the earlier raw Docker-export example to retain
only sanitized stdout records (replace the container/date placeholders):

```sh
docker logs <nginx-container> 2>/dev/null | awk '/^\{"time":/' > ~/logs/nginx-<date>.log
```

Use `sudo bash scripts/rotate_nginx_logs.sh --container <nginx-container>` on the host.
It renames the active files, pre-creates replacements without truncation, and sends
`nginx -s reopen`; it does not restart workers. Pre-creation also handles Docker
Desktop's host-bind rename behavior. A failed signal retains the archives and active
files and a durable `.reopen.pending` marker. Every later run retries that signal
before retention or size pruning, even when replacement files are empty. A failed
retry keeps the marker and skips all archive deletion; resolve the reported failure.
The script needs Bash and the host's GNU core utilities, not logrotate in the image.
The log directory must be the same bind source used by the chosen container; pass
`--log-dir` when it differs from this checkout's `logs/nginx`.

Install both entries in the Pi's root crontab, replacing `/path/to/repo` and including
the deployment's Compose project selection if needed:

```cron
0 3 * * 0 cd /path/to/repo && bash scripts/rotate_nginx_logs.sh --container "$(docker compose -f docker-compose.prod.yml ps -q nginx)"
*/15 * * * * cd /path/to/repo && bash scripts/rotate_nginx_logs.sh --container "$(docker compose -f docker-compose.prod.yml ps -q nginx)" --if-large
```

The weekly job forces rotation; the 15-minute job rotates files at 10 MiB and performs
retention/size pruning even when no rotation is needed. Archives expire after 14 days,
dated from their first JSON request rather than the rename date. Combined archives
are capped at 50 MiB, deleting oldest first; the size cap can shorten retention.
Each active file can grow between checks, so this is a scheduled threshold rather
than a hard filesystem quota. Tune `--max-bytes` and `--max-archive-bytes` deliberately
for the Pi's SD-card budget. An interrupted job normally releases `.rotate.lock`;
after a forced kill, verify no rotation is running before removing a stale lock.

Count 429/5xx by canonical path for an event date (adjust `day`):

```sh
awk -v day=2026-11-14 'index($0,"\"time\":\""day) && match($0,/"status":[0-9]+/) {s=substr($0,RSTART+9,RLENGTH-9); if(s==429 || s~/^5/) {match($0,/"uri":"[^"]*"/); p=substr($0,RSTART+7,RLENGTH-8); n[s" "p]++}} END {for(k in n) print n[k],k}' logs/nginx/access.log* | sort -nr
```

The masked URI/route group and `limit_req_status` retain venue-rate evidence across
recreation. The access log is authoritative for counts; do not add the duplicated
5xx entries in `error.log` to the totals.

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
4. Re-run preflight and require exit 0, confirming all files (including 001-006) are applied.
5. Rebuild and deploy `backend` and `fgt_dashboard` together only after that successful gate,
   then perform the DEV smoke checks. Both services use the shared TO membership update;
   both must run the release that resolves legacy registration guards.

`FGT_MIGRATIONS_ENABLED=0` is **not compatible with the new code on an old schema**.
Archive needs 003's `source_record_id`; callback tracking and membership provenance need 004.
Legacy registration guards and TO member edits require 006's `legacy_register_attempts` table.
Apply 006 before either service starts the new code; a TO member toggle otherwise fails.
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

## Internal callback authentication gate

`INTERNAL_AUTH_MODE` defaults to `soft`. The shared dependency covers POST
`/api/checkin/begin`, `/api/integration/result`, `/api/checkin/{checkin_id}/member-status`,
`/api/startgg/registered-count`, `/api/notify/checkin` and `/api/notify/update`.
In `soft`, missing/invalid tokens retain
the existing warning and request behavior; an untrusted begin request still takes the
public ownership path. In `enforce`, a missing/invalid token receives 401 before the handler.
Startup reports `internal auth: soft` or `internal auth: enforce` without the token.
Enforce mode requires a configured non-empty `INTERNAL_API_TOKEN` or startup fails.

1. Configure the **same private, non-empty** `INTERNAL_API_TOKEN` in backend and n8n.
   Keep `INTERNAL_AUTH_MODE=soft` in the backend. Recreate affected services with the
   approved environment configuration so their running processes receive the values;
   do not print tokens or add them to Git. Import the reviewed workflow exports before
   observing soft mode, including Event Stats' `Report to Backend` node with its
   env-backed `X-Internal-Token` header.
2. Run one complete event in soft mode, including all internal callback/notification paths.
   Check backend logs for **zero** `missing or invalid token` warnings. Resolve every
   warning and repeat the observation before proceeding; successful requests alone do
   not prove that token configuration matches.
3. Only after that gate, set `INTERNAL_AUTH_MODE=enforce` in the backend environment and
   recreate the backend service. Keep the matching n8n token configured. Verify normal
   internal requests and confirm that missing/wrong tokens receive 401.

Rollback: set `INTERNAL_AUTH_MODE=soft` again and recreate the backend with the updated
environment. This requires **no image rebuild** and no database change. Keep the shared
token and existing nginx public-route restrictions in place. Changing an env file alone
does not update a running container. These instructions authorize no deployment.

The example env files disable n8n diagnostics and use `N8N_SECURE_COOKIE=true` for PROD
behind HTTPS. Local HTTP development can explicitly override the cookie setting to false.
Their workflow env allowlist contains only the variables referenced by exported workflows;
OAuth `STARTGG_CLIENT_SECRET` is not exposed to workflow expressions. Optional commented
`N8N_BASIC_AUTH_USER/PASSWORD` hints remain for outbound backend calls to Basic-protected
webhooks; these do not enable retired n8n UI Basic Auth. Optional `AIRTABLE_API_KEY/BASE_ID`
hints remain for `DATA_BACKEND=airtable`. Real `.env` and `n8n.env` files are configured privately.

## nginx validation and graceful reload

Keep the reviewed nginx 1.29.4 image pinned to
`nginx:1.29.4@sha256:c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec`,
and the existing certificate, htpasswd and
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

## n8n image pin, stopped-volume backup and rollback

The reviewed n8n pin is
`n8nio/n8n:2.4.8@sha256:cae1f28d585a99d9ce9a56f82fbfc99fc100862ffef988d73fa2048d2687b0cc`.
This is an OCI multi-architecture index: amd64 selects
`sha256:9166e30aa74282695e3d41bf26543ce1a02eb6d624745a9f71d0e4b0c8c995b8`,
arm64 selects `sha256:c69c5414600894cb3eaeabd116bacc892bd46db7dd8456aef5d9175bfb2859c6`.
The supplied PROD digest fragment matches the index; an architecture-specific child
digest is expected to differ. No PROD operation is authorized by this runbook.

Before changing a running n8n version:

1. Identify the exact container, image ID/repository digest, named volume and project.
   Inspect only the required metadata; do not dump environment values or credentials.
   Confirm that no other container uses the volume. **Never run two n8n instances
   against the same volume.** Preserve the previous image locally.
2. **Stop n8n and back up the entire volume before any version transition.** Copying
   only a live `database.sqlite` can omit SQLite WAL state. A stopped-volume tar retains
   the database, configuration, ownership and binary data together. Keep it outside Git.
   Record its SHA-256 and verify the copied SQLite database with a read-only integrity check.
3. Keep the exact same `N8N_ENCRYPTION_KEY`; a restored SQLite file without the matching
   key cannot decrypt its credentials. Preserve the volume configuration too. Compare
   key/configuration identity internally, never print the key or expanded compose config.
4. Change only the intended n8n image. For an existing DEV project, a read-only original
   compose file plus an in-memory stdin image override preserves its mounts, key and port.
   Use `up --detach --no-deps --pull never n8n` after prefetching the immutable image.
   Do not recreate nginx or dependencies as part of this n8n operation.
5. Require the expected `n8n --version`, readiness HTTP 200, completed migrations,
   preserved workflow IDs and successful workflow activation logs. In n8n 2.x an exported
   `active` flag alone does not prove runtime activation. Check the actual activation IDs
   or UI, and record migration, task-runner, deprecation and activation warnings.
   If a workflow cannot load, stop the new container and roll back; do not patch flows
   during the image rehearsal. Do not execute provider workflows with real credentials.

Rollback means **restore the full volume and the previous image**, not just swap images:

1. Stop the upgraded container and confirm no other volume user is running. Retain a
   migrated snapshot separately if needed for diagnosis.
2. Verify the original backup hash. Mount only the explicitly verified target volume
   into an offline restore helper, clear that volume and extract the complete original
   archive, retaining ownership. Check database/config bytes against the backup before start.
3. Start the exact previous image with the same encryption key. Require its old version,
   readiness, workflow activation IDs and a clean startup against the restored schema.
   An old binary pointed at a migrated 2.x database is not a rollback.
4. Only after that rollback succeeds, take a fresh stopped-volume backup and repeat the
   pinned upgrade. Keep the backup until the deployment has been accepted.

DEV rehearsal targets are exclusively `fgt-dev-n8n-1`, its local
`fgt-checkin-system_n8n_data` volume, and existing port 5679. The volume name also appears
in PROD configuration, so verify the Docker host/context and sole DEV container user;
the name alone does not identify a safe target. Leave Nimo and other containers untouched.

For this rehearsal, an offline `alpine:3.21` helper used `--network none --read-only`,
with the DEV volume mounted read-only for backup and writable only for the authorized
restore, plus a backup directory inside agent-hub. Backup commands have this shape:
Set `$volume` to the verified volume name and `$backupRoot` to an existing private
backup directory before using these commands.

```powershell
docker stop --timeout 30 fgt-dev-n8n-1
docker run --rm --network none --read-only --mount "type=volume,source=$volume,target=/data,readonly" --mount "type=bind,source=$backupRoot,target=/backup" alpine:3.21 tar -czf /backup/pre-upgrade.tar.gz -C /data .
Get-FileHash -LiteralPath "$backupRoot/pre-upgrade.tar.gz" -Algorithm SHA256
```

Use direct argument calls for the post-restore `sha256sum /data/database.sqlite /data/config`
check. A PowerShell pipeline can append CRLF to the last shell-script line; do not treat
that verification failure as permission to start the old binary without checking the bytes.
The rehearsal command ledger and safe activation/restore evidence are recorded in
agent-hub under `reports/T-051.commands.md` and `backups/T-051-20261107`.

The nginx pin follows the confirmed PROD image, independently of the mutable tag:
`nginx:1.29.4@sha256:c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec`.
It is an OCI index (amd64 child
`sha256:617fef3c6adb90788d06f7ebe634fe990a6cd1e63c815facaf5fcaf3fa1ea003`).
At verification time, `nginx:1.29.4` resolved to a different index; retain the reviewed
digest rather than resolving that tag again. The conductor applies the DEV nginx change.

### DEV rehearsal result, 2026-11-07

The stopped 1.122.5 volume contained 22 workflows and passed SQLite integrity checks.
The first 2.4.8 run completed all 12 migrations, kept all workflow IDs and activated
all five previously active workflows. The entire original volume was then restored:
database/config bytes matched the backup, the exact prior 1.122.5 image started with
readiness 200, no migrations and its original five activation IDs. The encryption key
environment matched at every transition without being printed.

After a fresh stopped-volume backup, the final 2.4.8 run was healthy with readiness 200,
all 12 migrations complete, no activation failures and the same seven registrations
as the first upgrade. The two extra registrations are internal callable workflows:
`FGC THN - Start.gg Check (Internal)` and `FGC THN - eBas Check (Internal)`.
Their nodes are execute-workflow triggers, code and HTTP requests; no webhook, schedule
or polling trigger was newly enabled. The conductor accepted this expected 2.x behavior.

The JavaScript runner registered successfully. Internal Python runner startup warned
that Python 3 was missing; no activated workflow contains a Python code node.
The deprecation search matched a debug registration of `workflow-hooks-deprecated-v2`,
not a deprecation warning. No provider workflow was executed during the rehearsal.
The initial and fresh rollback archives remain in agent-hub, outside the repository.

## Acceptance smoke gates

Before an authorized deployment, run the synthetic write gate only after DEV runs
the integration release with `INTEGRATION_ENGINE=none`. The conductor runs this
gate; builders must not run it against the existing live-capable DEV stack:

```sh
python scripts/acceptance_smoke.py --mode dev-write --profile dev --base http://localhost:8088
```

Both the public base and the admin API base must be `localhost` or `127.0.0.1` and
their `/health` responses must explicitly report `components.integration_engine`
as `none`. The admin API defaults to the existing DEV backend binding
`http://127.0.0.1:8001`; `--admin-base` can select another loopback HTTP origin.
Redirects cannot bypass the write guard. HTTP checks supply no credentials; the
helper uses the DEV backend's existing storage configuration without displaying it.

The only Docker target is the hard-coded `fgt-dev-backend-1`. Its helper checks
the engine again inside the container before any write. It snapshots only the
settings fields it changes, activates one unique `acceptance-<timestamp>-<random>`
event with a synthetic game, and keeps settings writes locked during the HTTP
checks. An exclusive session advisory lock serializes the complete helper lifecycle
before taking a snapshot; a busy helper or a previous active `acceptance-*` event
refuses a new run. Three synthetic identities use the `.invalid` email namespace; the flow
needs no PNR with integrations disabled. If a PNR is ever added to these checks,
use the published Skatteverket constant in `tests/fixtures`, never a real number.

Three distinct check-in IDs and tokens must match the current player's exact
synthetic name/tag/email in the helper. Each token must authorize its own status
URL and reject a different owned ID with 403; public status JSON stays private.
The script checks orchestrate, capability-signed status, game saves and the TO
payment API when exposed. A missing optional payment route is reported as SKIP.
The helper's `finally` path restores the previous settings and deletes only this
run's event rows whose synthetic name, tag and email all match. It never archives,
reopens or deletes historical events or changes a real player. EOF or its bounded
session timeout also triggers restoration. Cleanup/restoration must be confirmed;
a failed confirmation is a failed gate. Do not kill the helper/container mid-run.

As the final step after an authorized PROD deployment, use the credential-free
read-only profile. These commands are documentation, not deployment authorization:

```sh
python scripts/acceptance_smoke.py --mode readonly --profile prod \
  --base https://checkin.fgctrollhattan.se \
  --admin-base https://admin.fgctrollhattan.se \
  --meetup-base https://meetup.fgctrollhattan.se \
  --membercard-base https://membercard.fgctrollhattan.se
```

Only GET/HEAD are permitted in this mode. The script sends no cookies, Basic auth,
PNRs or other credentials, and follows only the declared origin or the public
check-in host's canonical `www` redirect. It checks health/pages/assets, public
internal-route 404s, anonymous admin 401 with no Set-Cookie, and security headers.
Sibling flags are optional; omitted siblings show SKIP. Responses, names, tokens
and personal data are never included in the compact PASS/FAIL/SKIP table.
Any failed check exits non-zero; a successful syntax check or deployment alone
does not replace this gate.

For DEV, use the explicit read-only profile:

```sh
python scripts/acceptance_smoke.py --mode readonly --profile dev \
  --base http://localhost:8088 --admin-base http://localhost:8088/admin
```

DEV expects `/admin/` to show the anonymous login landing page (200 with a local
`/auth/login` link and no dashboard data markers/scripts/tables), or redirect to
`/admin/auth/login` with 302. Neither response may have a Basic gate or Set-Cookie.
The root Dash layout/dependencies/assets must be reachable (200 or the same OAuth
302). Only DEV's explicit `/api/events/stream` exception expects token-gated 401;
PROD's public host must return 404 there. The admin SSE path is token-gated 401
with no Set-Cookie in both profiles. Every other internal public path must be
404; 401/405 is a failure because it indicates the request reached an upstream.
The old main-branch gateway can therefore report FAIL until integration is deployed.
Report those findings; do not weaken the gate or send credentials to make it pass.

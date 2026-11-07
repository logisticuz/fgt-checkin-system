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

## Internal callback authentication gate

`INTERNAL_AUTH_MODE` defaults to `soft`. The shared dependency covers POST
`/api/checkin/begin`, `/api/integration/result`, `/api/checkin/{checkin_id}/member-status`,
`/api/notify/checkin` and `/api/notify/update`. In `soft`, missing/invalid tokens retain
the existing warning and request behavior; an untrusted begin request still takes the
public ownership path. In `enforce`, a missing/invalid token receives 401 before the handler.
Startup reports `internal auth: soft` or `internal auth: enforce` without the token.
Enforce mode requires a configured non-empty `INTERNAL_API_TOKEN` or startup fails.

1. Configure the **same private, non-empty** `INTERNAL_API_TOKEN` in backend and n8n.
   Keep `INTERNAL_AUTH_MODE=soft` in the backend. Recreate affected services with the
   approved environment configuration so their running processes receive the values;
   do not print tokens or add them to Git. The exported workflows send `X-Internal-Token`.
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
OAuth `STARTGG_CLIENT_SECRET` is not exposed to workflow expressions. Retired basic-auth
and Airtable entries are omitted; real `.env` and `n8n.env` files must be configured privately.

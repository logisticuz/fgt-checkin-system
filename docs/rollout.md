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

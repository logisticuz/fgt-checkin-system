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

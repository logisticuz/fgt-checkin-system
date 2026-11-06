# FGT schema migrations

- Name files `NNN_<vertical>.sql`, with a unique zero-padded three-digit version.
- Files run in lexical order; use one owner per track to avoid version collisions.
- The baseline is `db/init.sql` plus the legacy `_run_migrations` function.
- New migrations must be additive; existing data and column types must be preserved.
- Never edit or rename an applied file. Add a new migration instead.
- History is stored in `fgt_schema_migrations`, with a SHA-256 digest for each file.
- Every apply run holds the FGT advisory lock; each file has its own transaction.
- The runner owns transaction boundaries; do not add explicit BEGIN/COMMIT statements.
- The sibling applications' `meetup_*` and `card_ids*` tables are off-limits.
- A reviewed exception requires `-- migrate:allow-destructive <reason>` on line 1.
  The lint override is logged at WARNING; it is not permission to modify sibling data.
- Run `python -m shared.migrate check`, `status`, or `apply` with `DATABASE_URL` set.
  `check` and `status` do not create tables or record migrations.
- Startup defaults to enabled; set `FGT_MIGRATIONS_ENABLED=0` for staged rollout.

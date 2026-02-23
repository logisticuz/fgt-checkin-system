# Postgres Migration Plan (FGT Check-in)

## Goal

Replace Airtable as the primary datastore with Postgres while keeping the
application behavior and UI flows unchanged for users.

## Current Status (2026-02-23)

- Phase 0: DONE
- Phase 1: DONE (dev + prod compose updated, env vars added)
- Phase 2: DONE (schema in place, JSONB fields and indexes implemented)
- Phase 3: DONE (`shared/storage.py` facade + Postgres adapter)
- Phase 4: DONE (sessions, audit, settings, checkins, archive/stats, players)
- Phase 5: DONE in DEV (smoke + E2E verified, n8n v5/v2 callbacks validated)
- Phase 6: PARTIAL (DEV cutover complete, PROD cutover pending)

Open items:
- Deploy latest backend + flows to PROD
- Activate v5/v2 workflows in PROD and disable legacy v4/v1
- Run post-cutover smoke in PROD and keep rollback path documented

## Scope

- Add Postgres to the existing Docker stack (dev + prod).
- Define Postgres schema aligned with `docs/event-history/06-final-spec.md`.
- Introduce a storage adapter to swap Airtable -> Postgres with minimal code churn.
- Cut over reads/writes in controlled steps.

## Assumptions

- Only one event exists in Airtable (minimal data to migrate).
- Airtable remains available as a fallback during rollout.
- No personal data migration beyond existing check-ins is required.

---

## Phase 0: Preparation

1. **Decide cutover strategy**
   - Recommended: single cutover after dev verification (no dual-write).
2. **Lock schema source of truth**
   - Use `docs/event-history/06-final-spec.md` as authoritative schema.
3. **Create a migration checklist**
   - Map each Airtable table/field to Postgres columns.

---

## Phase 1: Infrastructure (Docker + Env)

1. **Add Postgres service to Docker**
   - `docker-compose.dev.yml`: `postgres` service + volume
   - `docker-compose.prod.yml`: `postgres` service + volume
2. **Add env vars**
   - `DATABASE_URL` (preferred)
   - Or `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
3. **Network**
   - Ensure backend/dashboard can reach Postgres via service name

---

## Phase 2: Schema (DDL)

Create Postgres tables matching the Event History spec:

- `event_archive`
- `event_stats`
- `players`
- `audit_log`
- `sessions`
- `active_event_data` (for live check-ins)
- `settings` (active event configuration)

Notes:
- Use `jsonb` for: `games_breakdown`, `status_breakdown`, `startgg_snapshot`,
  `game_counts`, `member_history`.
- Use indexes on: `event_slug`, `player_uuid`, `timestamp`, `created_at`.

---

## Phase 3: Storage Adapter

1. **Create a backend-agnostic module**
   - `shared/storage.py` as a facade
2. **Implement Postgres adapter**
   - `shared/postgres_api.py` with the same function surface as `airtable_api.py`
3. **Feature flag**
   - `DATA_BACKEND=postgres|airtable`

Goal: switch backend by changing env var only.

---

## Phase 4: Incremental Cutover

Recommended order:

1. **Sessions + Audit Log**
   - Low risk, small tables
2. **Settings + Active Event Data**
   - Core runtime data
3. **Event Archive + Stats**
   - Core history data
4. **Players**
   - Cross-event aggregation

---

## Phase 5: Validation

1. **Functional smoke tests**
   - Check-in flow works
   - Dashboard loads
   - Audit log renders
2. **Data integrity**
   - Row counts align
   - Basic query sanity checks
3. **Performance**
   - Queries under expected load

---

## Phase 6: Cutover

1. Set `DATA_BACKEND=postgres` in dev
2. Verify end-to-end behavior
3. Promote to prod after dev validation
4. Keep Airtable read-only for fallback period

Status: DEV complete, PROD pending

---

## Risks & Mitigations

- **Schema mismatch** → Use a field-mapping checklist, review before cutover
- **Data loss** → Minimal data; export Airtable backup before cutover
- **Downtime** → Do a short maintenance window for prod switch
- **Inconsistent behavior** → Keep API signatures identical between backends

---

## Deliverables

- Docker Compose updates for Postgres
- Postgres DDL
- Storage adapter with `DATA_BACKEND` switch
- Validation checklist + notes

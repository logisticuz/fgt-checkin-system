# Changelog 2026-02-23 - n8n Migration: Renodla till integration-engine

## Problem
n8n hade fortfarande 7 Airtable-anrop (5 i Orchestrator v4, 2 i eBas Register). Med Postgres-migrationen klar
behover n8n bara orkestrara externa API-anrop (Start.gg, eBas), aldrig skriva till DB direkt.

## What changed

### Backend (main.py)

**New endpoint: `POST /api/checkin/orchestrate`**
- Ersatter frontend-anropet till `/n8n/webhook/checkin/validate`
- Flow: validate + sanitize -> begin_checkin() i Postgres (dedupe) -> forward till n8n v5 -> re-read checkin -> compute status -> SSE broadcast -> return
- Synkront (vanter pa n8n) - samma UX som forut
- Returnerar `{ ready, slug, missing[], member, payment_valid, startgg }` - kompatibelt med checkin.html
- Graceful degradation: om n8n-anropet misslyckas skapas checkin anda (status Pending)

**New endpoint: `POST /api/ebas/register`**
- Ersatter frontend-anropet till `/n8n/webhook/ebas/register`
- Flow: sanitize -> find checkin by tag+slug -> forward till n8n eBas v2 -> return result
- Returnerar samma format som gamla n8n-workflowen (success/error)

### Postgres API (shared/postgres_api.py)

**New function: `get_checkin_by_record_id(record_id)`**
- Lookup by primary key for fast re-read after integration results applied

**New function: `compute_checkin_status(checkin_fields, settings)`**
- Standalone status computation: Ready/Pending based on requirements
- Returns `{ status, ready, missing[] }`

### Frontend

**checkin.html (line 405)**
- `fetch('/n8n/webhook/checkin/validate...')` -> `fetch('/api/checkin/orchestrate', ...)`
- No token param needed (backend-to-backend auth)

**register.html (line 557)**
- `fetch('/n8n/webhook/ebas/register...')` -> `fetch('/api/ebas/register', ...)`
- Existing `/api/player/member` fallback call is now redundant but harmless (idempotent)

### n8n Workflows

**New: Checkin Orchestrator v5 (PG)** - `n8n/flows/FGC THN - Checkin Orchestrator v5 (PG).json`
- Webhook path: `checkin/validate-v5`
- Zero Airtable nodes (was: 5 in v4)
- Flow: Webhook -> Parse -> eBas + Start.gg (parallel) -> Combine -> Report to /api/integration/result (per source, parallel) -> Return summary
- Backend handles: dedupe, DB writes, status computation, SSE

**New: eBas Register v2 (PG)** - `n8n/flows/FGC THN - eBas Register v2 (PG).json`
- Webhook path: `ebas/register-v2`
- Zero Airtable nodes (was: 2 in v1)
- Flow: Webhook -> Normalize -> Call eBas API -> Parse -> Report member status to /api/checkin/{id}/member-status -> SSE notify -> Return
- Backend handles: DB writes

### Old workflows
- Left completely intact (active on old paths)
- Will be disabled after E2E testing confirms v5/v2 work correctly

## Architecture (before vs after)

**Before:**
```
Form -> n8n proxy -> n8n (loads settings from Airtable, checks APIs,
        saves to Airtable, dedupes in Airtable, notifies SSE) -> Form
```

**After:**
```
Form -> /api/checkin/orchestrate -> begin_checkin(Postgres)
        -> n8n v5 (checks APIs only, reports to /api/integration/result)
        -> re-read checkin -> compute status -> SSE -> Form
```

## Why this approach
- **Separation of concerns**: n8n = external API orchestration only; backend = all data/state
- **No Airtable dependency in n8n**: Clean cut for Postgres migration
- **Dedupe in backend**: Postgres UPSERT is atomic, no race condition issues (vs Airtable read-then-write)
- **Synkront UX**: Same user experience - form waits for status result
- **Graceful degradation**: If n8n is down, checkin still gets created (Pending status)
- **Old workflows intact**: Zero risk - can roll back by changing 2 URLs in templates

## Files changed
| File | Action |
|------|--------|
| `backend/main.py` | MODIFIED - +2 endpoints, +1 import block |
| `shared/postgres_api.py` | MODIFIED - +2 functions |
| `backend/templates/checkin.html` | MODIFIED - 1 URL change |
| `backend/templates/register.html` | MODIFIED - 1 URL change |
| `n8n/flows/FGC THN - Checkin Orchestrator v5 (PG).json` | NEW |
| `n8n/flows/FGC THN - eBas Register v2 (PG).json` | NEW |

## Next steps (E2E testing)
1. Import v5 + v2 workflows into n8n (via UI or API)
2. Activate v5 + v2 workflows
3. Test happy path: checkin -> Postgres row created -> n8n checks -> status Ready/Pending
4. Test API failure: n8n returns error -> checkin exists with Pending status
5. Test eBas register: personnummer -> Sverok API -> member=true in Postgres
6. When green: disable old v4 Orchestrator + v1 eBas Register workflows

## Rollback plan (quick)
1. Re-activate old workflows: `Checkin Orchestrator v4` and `eBas Register v1`.
2. Temporarily deactivate `Checkin Orchestrator v5 (PG)` and `eBas Register v2 (PG)`.
3. Revert frontend URLs in `backend/templates/checkin.html` and `backend/templates/register.html` to `/n8n/webhook/*` endpoints.
4. Restart backend container and run one smoke-checkin + one eBas register test.
5. Keep new flow JSON exports in repo for forensic diff/retry after incident is resolved.

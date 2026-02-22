# Changelog: 2026-02-22 - Event History + Postgres Migration (Phase 1)

**Deltagare:** Användare, Claude, Codex

## Sammanfattning

Sessionen flyttade projektet från Airtable-fokus till Postgres-fokus för Event History,
byggde färdigt auth/audit-grunden, implementerade första fungerande Postgres-backend,
och verifierade ett end-to-end arkivflöde i dev.

## Viktiga beslut

- `event_history` / `event_history_dashboard` ersätts konceptuellt av `event_archive` / `event_stats`
- `event_slug` är stabil nyckel, `event_display_name` är visningsnamn
- Start.gg snapshot ska sparas i `event_stats.startgg_snapshot`
- Full player-profil ska användas från start (inkl. persistent `player_uuid`)
- Arkivering ska vara idempotent via **replace mode** (ingen dublett-ackumulering i `event_archive`)
- Dev kör Postgres på host-port `5434` för att undvika konflikt med annat projekt

## Dokumentation skapad/uppdaterad

- `docs/event-history/06-final-spec.md` (definitiv spec)
- `docs/event-history/07-airtable-schema-diff.md` (konkret schema-checklista)
- `docs/event-history/02-data-model.md` (synkad med final spec)
- `docs/event-history/WORK_ORDER.md` (synkad med final spec)
- `docs/event-history/README.md` (uppdaterad med 06/07 + branch-policy)
- `docs/postgres-migration-plan.md` (ny övergripande PG-plan)
- `docs/README.md`, `docs/PROJECT_STATUS.md` uppdaterade med PG-planreferens

## Implementerat i kod

### Phase 1A/1B (auth + audit)

- Start.gg OAuth helper + auth endpoints
- Sessionhantering och audit-logik
- Dashboard UI: login/logout i header + Audit Log-flik

### Postgres Migration Phase 1

- Docker services för Postgres i dev/prod
- `db/init.sql` med `pgcrypto` + 7 tabeller:
  - `settings`, `active_event_data`, `event_archive`, `event_stats`, `players`, `sessions`, `audit_log`
- `shared/storage.py` facade med `DATA_BACKEND=airtable|postgres`
- `shared/postgres_api.py` med implementerade delar:
  - sessions/audit
  - settings/checkins
  - players/event_history reads
  - archive pipeline (`archive_event` + stats)

### Arkiveringsflöde (Postgres)

- Matchar/skapar spelare via tag/email
- Skriver `player_uuid` till `event_archive`
- Beräknar och upsertar `event_stats` inkl. `startgg_snapshot`
- Fallback från settings för saknade parametrar (`event_date`, `event_display_name`, `swish_expected_per_game`, `events_json`)
- Audit-loggar `event_archived` / `event_rearchived`
- Idempotency via replace mode: tidigare `event_archive`-rader för slug raderas före ny insert

## Verifiering i dev

- Postgres container uppe och healthy (`postgres:16`)
- Dashboard laddar mot Postgres (`DATA_BACKEND=postgres`)
- Settings läses från Postgres
- Dummy check-in synlig i dashboard
- `archive_event('dev-event-1')` körd framgångsrikt
- Verifierat att:
  - `event_archive` uppdateras
  - `event_stats` upsertas
  - `players` uppdateras utan dubbelräkning av totals vid re-archive
  - `audit_log` får korrekt action

## Commits under sessionen

- `4ea87f8` feat(auth): add start.gg oauth sessions and audit log ui
- `cda9b75` feat(db): add postgres infra and schema scaffolding
- `ae71ce9` feat(db): implement postgres sessions and audit log
- `5381048` feat(db): implement postgres settings and checkins
- `5ba459c` refactor(storage): use storage facade for data access
- `b73c6f8` feat(db): add postgres players and event history reads
- `e6dc5ab` fix(db): use postgres 16 image
- `7711e82` feat(db): implement postgres archive pipeline
- `b435a3e` fix(db): make archive_event idempotent by replace mode

## Kvarstående arbete (nästa steg)

- Koppla UI-knapp/arbetssätt för arkivering mot `archive_event`-flödet fullt ut
- Lägg till explicit "Reopen event"-flöde i UI/API
- Rensa upp kvarvarande Airtable-beroenden där de inte längre behövs
- Förbereda PR-beskrivning med testprotokoll och skärmbilder

## Notering

Flera changelog-filer och CSV-backupfiler ligger fortfarande untracked/dirty i worktree
och har medvetet lämnats orörda för att undvika orelaterade commits i denna feature.

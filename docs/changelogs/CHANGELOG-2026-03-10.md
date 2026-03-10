# Changelog 2026-03-10

## fix(insights): Insights tab crash + case-insensitive player grouping

### Problem
1. **Insights tab visade bara nollor** — alla KPI-kort, tabeller och spelare var tomma trots att event_stats och event_archive hade data. Inga felmeddelanden syntes i UI:t, men containerloggarna visade:
   ```
   NameError: cannot access free variable 'date' where it is not associated with a value in enclosing scope
   ```

2. **Dubbletter i spelar-listan** — spelare som checkat in med olika casing (t.ex. "Walvin" / "walvin", "Viktor Molina" / "viktor molina") räknades som separata personer i Insights Players-tabben.

### Root Cause

**Bug 1 (date shadow):** I `update_insights()` callback i `callbacks.py` definierades en lokal variabel `date = ev.get("event_date")` på rad 1484. Python's closure scoping såg detta som en lokal variabel för hela funktionen, vilket skuggade den importerade `datetime.date`-klassen. Den nestade funktionen `_as_date()` försökte använda `isinstance(v, date)` *innan* den lokala variabeln hade tilldelats — classic Python scoping bug. Dash fångade 500-felet och visade defaultlayouten med nollvärden.

**Bug 2 (case-sensitive GROUP BY):** `get_top_players_history()` i `postgres_api.py` grupperade med `GROUP BY 1, 2` (rakt av COALESCE-uttrycken), vilket är case-sensitive. "Walvin" och "walvin" blev två separata grupper.

### Changes

#### `fgt_dashboard/callbacks.py`
- Renamed local variable `date` → `ev_date_str` (line 1484) to avoid shadowing `datetime.date` import
- This unblocks the entire `update_insights()` callback from crashing

#### `shared/postgres_api.py`
- `get_top_players_history()`: Changed GROUP BY from positional (`GROUP BY 1, 2`) to `GROUP BY LOWER(...)` for case-insensitive player deduplication
- Used `MAX()` on display columns to pick a representative name/tag for each group
- Effect: "Walvin"/"walvin" now merge into one row showing 2 events attended instead of two rows with 1 each

### Testing
- Verified via direct SQL query: 57 rows collapsed to correct count with proper deduplication
- Insights tab now loads and shows KPIs, player leaderboard, game stats
- Dashboard container logs clean (no more NameError)

### Known Limitations
- **Spelling variants** (e.g. "Carl Jonson"/"Carl Jonsson", "Tage Stenan"/"Tage Stenman") are NOT merged automatically — these are genuine typos, not case differences
- A future "merge players" feature for TOs would allow manual deduplication of such cases (see TODO below)

### TODO (future)
- [ ] **Duplicate player warning for TOs** — detect potential duplicates (similar name/tag with different spelling) and surface a warning in the Insights Players tab or Settings
- [ ] **Manual player merge** — allow TOs to select two player entries and merge them (combine event history, keep preferred name/tag spelling)

---

## fix(event-lifecycle): archive/date fallback, coverage source guards, and admin UX safety

### Problem
- Archiving could fail with `event_date is required for archiving` when `settings.event_date` was missing.
- Live coverage could show mixed numbers (for example `34/33`) when selected event and active Start.gg snapshot did not match.
- Clearing event selection could unintentionally force redirect to `/auth/select-event` and interrupt dashboard flow.
- Auth select-event page in dev looked unstyled and had route mismatch (`/auth/select-event/save` vs `/admin/auth/select-event/save`).

### Changes

#### `shared/postgres_api.py`
- `archive_event()` now resolves date in safer fallback order:
  1) payload `event_date`
  2) `settings.event_date`
  3) Start.gg snapshot date (`event_date/date/startAt/start_at`, also inside `events[]`)
  4) final fallback to current UTC date with warning log

#### `fgt_dashboard/callbacks.py`
- Coverage logic now avoids cross-event contamination:
  - active event uses active settings snapshot only when snapshot slug matches selected event
  - archived/non-active event reads per-slug stats from `event_stats`
  - older snapshots without player metric show explicit slot-only fallback text
- Archive clear behavior now clears dashboard selection without clearing global auth-active slug (prevents forced redirect).
- Added explicit clear-event flow with confirmation and dashboard-only clear semantics.
- Added quick multi-select control to clear row selections without manual unticking row-by-row.

#### `fgt_dashboard/layout.py`
- Added `Clear Event` button beside `Archive Event`.
- Event dropdown is now clearable and shows a guidance placeholder when no event is selected.
- Added multi-select toggle button in the action row above table operations.

#### `fgt_dashboard/api.py`
- `/auth/select-event` redesigned to match dashboard dark UI style.
- Supports manual slug input in addition to approved-event dropdown.
- Manual input now accepts either plain slug or full Start.gg URL and extracts tournament slug.
- Fixed dev/prod route handling for submit/back/logout links.

### Result
- Archive flow is resilient even when event date metadata is incomplete.
- Coverage labels are consistent with selected event context (no more misleading >100% from mixed snapshots).
- Admins can clear selection safely without being kicked out of dashboard.
- Event selection UX is clearer and faster, especially in “no active event” bootstrap state.

---

## feat(data): Canonical player UUID foundation (Phase 1)

### Problem
- `player_uuid` existed in historical archives but not in active check-in rows, making identity tracking one-way and brittle.
- Reopen flow restored rows from archive without preserving canonical identity linkage.
- No safe utility existed to backfill/check UUID coverage on legacy rows.

### Changes

#### `db/init.sql`
- Added `player_uuid` column to `active_event_data` (with index) for canonical identity tracking during live events.

#### `shared/postgres_api.py`
- Added startup-safe/idempotent migration checks for `active_event_data.player_uuid` and index creation.
- Added `CANONICAL_PLAYER_ID_ENABLED` feature flag (default `true`) and startup log visibility.
- `begin_checkin()` now resolves and stores `player_uuid` via match-only lookup (`_find_player_uuid`) when feature flag is enabled.
- `reopen_event()` now preserves and restores `player_uuid` from `event_archive` back into `active_event_data`.

#### `scripts/backfill_player_uuid.py`
- New backfill script with dry-run default and explicit `--write` mode.
- Supports verbose logging and safe reporting before updates.

### Verification
- Backfill verification run showed archived rows already complete for the tested dataset.
- Backend startup and migration remained stable (no runtime errors).

### Rollback
- Set `CANONICAL_PLAYER_ID_ENABLED=false` to disable active check-in UUID assignment without code rollback.

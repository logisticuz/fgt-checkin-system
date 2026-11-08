# 2026-11-08 PROD rollout, waves 1 and 2

PROD (Raspberry Pi) moved from `a9f4223` (tag `prod-pre-2026-11-08`) to `aefabb4` (tag `dev-wave2-2026-11-08`).
72 merges: tasks T-001 to T-075 plus the 2026-05 SSL/OAuth hotfix.

- **Wave 1** was done at 11:00. It brought `30404b5` and migrations 000–013, set eBas to live, and recreated nginx and the n8n flows.
- **Wave 2** was done at 11:40. It brought `aefabb4`: T-071 (internal token) and T-075 (eBas-unknown badge). Only the dashboard was rebuilt.
- Both waves passed acceptance smoke 24/24. The owner's real test check-in reached Ready (member via eBas, Start.gg OK), and admin login via Start.gg worked. Wave 2 logged 0 internal-token warnings.

## Why this rollout
PROD had not been updated since May. The goal was to have one tested state for FightBox #5 (2026-11-14).
- **The problems we fixed:**
  - eBas registration showed false errors and sent duplicate submits (FightBox #3, issue #13).
  - Dashboard buttons could hit the wrong player.
  - Archiving could lose rows.
  - An eBas or Start.gg outage could look like "not a member" or "not registered".
- **How we tested it:** the work was built on DEV over two nights by an agent team, with cross-family code review. A dress rehearsal on DEV used the PROD path: a 25-person rush in 2 minutes from 16 IPs, eBas failures and duplicates. All 79 checks passed.

**The guiding principle: unknown is not no.** When an external service fails, the system says "unknown/pending" and never "no". A false "no" turns a real member away at the door. A "pending" only means a TO takes a second look.

## Player check-in
- **Status by id, not by name (T-024, T-032, T-033):** status pages poll by `checkin_id` plus a capability token. Name collisions can no longer show someone else's status.
- **Check-in carries through to /register (T-020):** `checkin_id` follows into the register page, so a membership attaches to the right check-in.
- **Kiosk hygiene (T-025, T-062, T-063):**
  - validation and error UX are clearer;
  - the browser can't go back to the previous player's page;
  - the register page returns to the start when idle;
  - small fixes from a Playwright walkthrough (T-061).
- **Tag hint (T-047):** a hint under the tag field says "same tag as Start.gg".
- **Degraded signal (T-030):** the orchestrate response carries an explicit degraded signal, so the player sees "pending" instead of an error.

## Membership and eBas
- **Re-check-in keeps state (T-004):** a new check-in never clears `member`, and status is recomputed in the same transaction.
- **Legacy registration (T-007, T-053, T-057):** no false errors and no duplicate submits (issue #13). An unknown outcome is never a hard error.
- **Unknown ≠ not a member (T-046, T-048, T-052):**
  - `lookup_state` is separate from `member`;
  - the n8n eBas check never answers "not a member" on an error, which also protects the member card;
  - the backend validates provider verdicts strictly.
- **eBas check without n8n (T-043):** the eBas check at check-in runs in the Python client, so no personnummer passes through n8n.
- **eBas client and DEV guard (T-010, T-041):**
  - a typed eBas client with strict response whitelisting;
  - an explicit transport gate, so DEV can't call the real eBas by accident.
- **Membership ownership (T-035, T-038, T-039):** a membership binds only to a check-in the person owns. All active-row writes run under the event lock.
- **eBas-unknown badge (T-075):** the TO table shows an amber "eBas svarade inte" badge, separate from "inte medlem".

## Start.gg
- **Admin login (T-060):** the OAuth callback rejected real Start.gg codes because of a 512-character limit; real codes are about 832. This blocked admin login and is fixed.
- **OAuth hardening (T-045, T-050):** the admin login now uses an OAuth `state` parameter and a `__Host-` state cookie.
- **Strict tag match (T-028, T-054):**
  - the tag must match exactly, and queries are built safely;
  - a timeout gives "unavailable", not "false";
  - Orchestrator v5 keeps the HTTP status, requires typed verdicts and checks callback acknowledgements.
- **Game names (T-065):** game names are normalised through `game_aliases` (migration 009), so "SF6", "Street Fighter 6" and "STREET FIGHTER 6 TOURNAMENT" are one game.

## TO dashboard
- **Right player (T-001):** the payment, delete and recheck buttons could act on the wrong player. They now always target the right row.
- **Status from the server (T-027):** status comes from the server and is recomputed when the requirements change. SSE updates are debounced.
- **Confirmations (T-029):** confirmation dialogs, a "paid the difference?" prompt and a clearer membership display.
- **Live duplicates (T-068):** a suspected duplicate, such as a tag typo, shows up live during the event.
- **Duplicate decisions (T-056, T-064):** "Not same" decisions stick, dismissals are cleaned up on merge, and Undo is audited.
- **Manual check-in games (T-066, T-066b):** game options come from the selected event and are deduplicated.
- **Data integrity (T-011):** the games cell is read-only, search is literal, and the CSV export guards against injection.
- **Clearer states (T-040):** clear "manual review" and "ownership unverified" states.
- **Auth (T-021, T-071):** the server decides who is logged in (`current_user()`), and internal dashboard calls send `X-Internal-Token`.

## Insights, statistics and archive
- **Safe archiving (T-026):** no DELETE without archived rows, and no lost concurrent check-ins.
- **Stats engine (T-015):** a deterministic stats recompute, player aggregates and a dry-run repair CLI. The repair on PROD data runs after the event, with a dry run first.
- **Automatic recompute (T-067):** stats recompute automatically after manual changes, and Insights gets a "things to check" box.
- **Check-in time in the archive (T-069a):** `event_archive.checked_in_at` (migration 013). The check-in time used to be lost when an event was archived.

## Security and privacy
- **XSS and errors (T-005, T-019):** XSS fixes on the status pages and `/auth/callback`, generic error messages, and API docs turned off.
- **Closed routes (T-008, T-022):** backend bypass routes are closed. `PATCH /api/player/games` is locked down: one row per (event, tag) and one payment rule.
- **No personnummer in n8n (T-016):** no personnummer in the n8n execution history, and callbacks carry the internal token.
- **Local-only ports (T-018):** backend and dashboard ports are bound to 127.0.0.1 on PROD.
- **nginx (T-044):** a public allowlist, rate limits per venue and security headers.
- **Internal auth (T-055):** internal API auth. It runs in `soft` mode now and switches to `enforce` after an event with 0 warnings.
- **Integration-scan fixes (T-036, T-037):** stale integration results no longer overwrite newer ones, membership can't be submitted after archiving, and migrations have a preflight.

## Operations and stability
- **Migrations (T-006):** a file-based migration runner with a lock and a log. It fails visibly.
- **One worker (T-007):** the backend runs with 1 worker, so SSE reaches every dashboard.
- **Backend structure (T-017, T-023):** `backend/main.py` is split into routers (a pure move), and async handlers make no blocking DB calls.
- **Pinned versions (T-031, T-051):**
  - base images and dependencies are pinned;
  - n8n runs the same version on DEV and PROD;
  - the n8n rollback was rehearsed.
- **Persistent logs (T-049):** nginx logs survive container recreation, with rotation and a privacy log format.
- **Acceptance smoke (T-059):** a smoke script, writing on DEV and read-only on PROD. It produced the 24/24.
- **May hotfix:** the 2026-05 SSL renewal and Start.gg OAuth hotfix is merged back into the code.

## Built but switched off on PROD
PROD runs `MEMBERSHIP_FLOW=legacy`. Membership v2 is in the code but off:
- T-009: the feature flag;
- T-012: the encrypted attempt store;
- T-013: the one-time-code kiosk step;
- T-014: the v2 endpoints;
- T-003: the verification sender.

It is switched on later, after the questions to Sverok/eBas, starting with whether eBas has a test environment.

## DEV and test only
- T-002, T-042, T-058: the test foundation and deterministic lock tests.
- T-061: the Playwright walkthrough.
- T-073: an eBas scenario stub.
- T-074: the FightBox dress-rehearsal simulator.

## What the rollout taught us (found by the PROD team)
- **n8n import IDs:** the flow files carry DEV workflow IDs, and importing them as they are would create duplicates on PROD. The import used stripped copies with the PROD IDs.
- **n8n import keys:** the flow files also carry `versionId`/`activeVersionId`, which caused a SQLite foreign-key failure. The import only works with id/name/nodes/connections/settings/description and `active=false`, followed by `publish:workflow` and an n8n restart. T-076 makes this a script.
- **Post-rollout review findings, for wave 3:**
  - secrets in access logs (T-081);
  - eBas fails open if `EBAS_TRANSPORT` is missing in production (T-082; not active, because PROD sets `live` explicitly);
  - the `EBAS_ASSOC` alias (T-078);
  - nginx log rotation is not scheduled (T-079).

## Next
Wave 3 (planned, not started) is built on DEV and deployed after 2026-11-14 in two PRs:
- **3a, security/ops:** T-082, T-078, T-081, T-079, T-080, T-083 and T-076/T-077;
- **3b, features:** T-070, T-069, T-072 and T-084 (GH#14), with migrations 014–016.

After that, the T-015 repair on PROD data and `INTERNAL_AUTH_MODE=enforce`.

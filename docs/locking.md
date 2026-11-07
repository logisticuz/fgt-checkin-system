# Active-event locking

All locks below are transaction-scoped PostgreSQL advisory locks.
Acquire locks in this order: event(s), global player-merge lock, then row locks.
Event key: `hashtext('fgt-event:' || event_slug)`.
Global key: `hashtext('fgt:player-merges')`.
Acquire multiple event keys using Python's sorted slug order in every writer, independent of DB collation.

| Writer | Event mode | Global mode |
| --- | --- | --- |
| begin_checkin | Shared | Shared |
| update_checkin (including toggles) | Shared, source + destination for moves | Shared |
| delete_checkin | Shared | None |
| reopen_event / restore INSERT | Exclusive | Exclusive |
| archive_event / clear-active | Exclusive | Exclusive |
| delete_archived_event | Exclusive | Exclusive |
| statistics-only retry / standalone derived write | None | Try Exclusive only while idle |
| plain attention acknowledgement | None | None (own acknowledgement row only) |
| draft policy/settings | None | None (own settings/context rows) |
| active-event selection/opening | None | Shared, before settings row locks |
| recompute_active_statuses | Exclusive | None |
| merge_players / undo_merge | Exclusive, all affected slugs | Exclusive |
| resolve_live_duplicate (T-068: stub + merge/dismissal + audit, one transaction) | Exclusive, affected slugs + the check-in's event | Exclusive |

ID-only writers read the slug without row locks, acquire event locks, then recheck the row.
Merge/undo discover affected slugs without row locks and recheck after acquiring global.
If a new slug appeared, roll back the lock-only savepoint and retry, at most three times.
Exhaustion raises a clear error and rolls back; merge/undo use a 10-second lock timeout.
Ordinary writers share the global lock and can proceed concurrently.

T067 refreshes all derived `event_stats` and `players` values while the caller holds
the global **exclusive** player-merge lock. This lock serializes derived writes for
every event; the refresh does not acquire additional event locks after it. An explicit
statistics-only retry first checks live admission, then tries the global exclusive lock
without waiting, without event locks. ANY active settings slug defers owner refresh/retry
and merge-triggered derived writes to a durable stale marker. No inline computation runs
while check-in is open. The active event's explicit archive END heals all markers.
Merge/undo retain their existing fact-serialization locks, but skip the derived refresh
while any event is active. Plain acknowledgement never takes the global lock: only its
own acknowledgement row is locked. The disabled owner button says "Recompute now" and
has the tooltip "Runs after the event is archived" while live admission is closed.
Opening/switching the active slug takes shared global mode before its settings row
lock, inside the same explicit transaction as the settings UPDATE and policy changes.
The lock survives pauses before UPDATE and is released only at commit/rollback, even
with an autocommit pool. This serializes admission with an idle refresh: the new
event cannot become live halfway through that refresh. Reopen already holds exclusive
global mode before changing settings. Ordinary policy edits do not take this lock.
Reopen takes exclusive global mode initially, before row locks; never upgrade a shared
global lock after locking rows. Active check-in writers retain their existing shared mode.

Archive/rearchive and merge/undo write the user's facts, then run the deterministic
refresh, including its context read, in a savepoint in that same transaction when admitted.
Success commits row-level before/after
audit. Failure rolls back only the refresh, stores a durable degraded marker and logs
only the exception class; the user's correction still commits. No job or post-commit
worker is involved. The next eligible trigger or an idle owner retry refreshes all events/profiles
and clears markers. Only changed derived rows are updated.

Reopen restores an editable active draft and event-specific verified policy into
`event_edit_context`; it preserves archived facts and the historical stats/snapshot.
Manual additions, removals and payment/member/game edits stay drafts until explicit
rearchive. Every activation loads the target slug's own saved policy; switching through
another draft never overwrites it. Tariff basis comes only from the verified snapshot
and is carried through payment/game-edit consumers. A person price stays per person.
Rearchive replaces the tracked mirror, including deliberate
deletions, then refreshes all later cohorts. Legacy NULL-source rows retain their fact
IDs through the separate restore map. Prices govern expected amounts only.
The mirror is persisted/rebuilt even when active rows are retained (clear_active=false).
For pre-011 retained drafts without a context, reopen and END recover links only from
exact active record IDs matching archived source IDs. Unmapped NULL-source facts stay
untouched when verified links exist. Missing/nonunique source links are ambiguous and
refuse END (and reopen with existing rows) without changing facts; the message directs
the TO to clear the draft, re-open the archive, and reapply corrections. An explicitly
additive draft can instead be opened with restore_active=false before adding new rows.
Deleting the last mapped row is allowed at END. Explicitly emptied archives are marked
attendance_recorded and derive zero counts; summary-only history without recorded rows
is preserved byte-for-byte and labelled "Summary only (no rows)". It is never zeroed by
refresh or its own no-edit reopen/END. Adding rows to a summary sets summary_only,
stores those facts, and keeps the summary authoritative until a future explicit owner
conversion. Insights and Things to check show "summary-only event got N rows: summary kept";
neither attendance_recorded nor participant/revenue/game totals are implicitly replaced.
Mark-not-recorded audits both the historical snapshot and full draft-policy row.
Reopen uses the event's date or its unambiguous archived-date fallback and clears an
unrelated settings date. An entirely unknown historical date requires an explicit TO
correction before rearchive; it never silently becomes today's date.

Merge refresh runs before capturing its undo fingerprints. A later statistics-only
heal may extend the newest fingerprint only if the entire player, archive and active
snapshots still match beforehand; the player row is locked while checking. Changed
identity or attendance facts never receive a fresh undo fingerprint. Dismissing a
duplicate pair does not trigger a refresh.

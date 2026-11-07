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
| statistics-only retry / standalone derived write | None | Exclusive |
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
statistics-only retry takes the global exclusive lock directly, without event locks.
Reopen takes exclusive global mode initially, before row locks; never upgrade a shared
global lock after locking rows. Active check-in writers retain their existing shared mode.

Archive/rearchive and merge/undo write the user's facts, then run the deterministic
refresh in a savepoint in that same transaction. Success commits row-level before/after
audit. Failure rolls back only the refresh, stores a durable degraded marker and logs
only the exception class; the user's correction still commits. No job or post-commit
worker is involved. The next trigger or an owner retry refreshes all events/profiles
and clears markers. Only changed derived rows are updated.

Reopen restores an editable active draft and event-specific verified policy into
`event_edit_context`; it preserves archived facts and the historical stats/snapshot.
Manual additions, removals and payment/member/game edits stay drafts until explicit
rearchive. Rearchive replaces the complete restored mirror, including deliberate
deletions, then refreshes all later cohorts. Legacy NULL-source rows retain their fact
IDs through the separate restore map. Prices govern expected amounts only.
Reopen uses the event's date or its unambiguous archived-date fallback and clears an
unrelated settings date. An entirely unknown historical date requires an explicit TO
correction before rearchive; it never silently becomes today's date.

Merge refresh runs before capturing its undo fingerprints. A later statistics-only
heal may extend the newest fingerprint only if the entire player, archive and active
snapshots still match beforehand; the player row is locked while checking. Changed
identity or attendance facts never receive a fresh undo fingerprint. Dismissing a
duplicate pair does not trigger a refresh.

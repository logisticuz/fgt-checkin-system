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
| reopen_event / restore INSERT | Exclusive | Shared |
| archive_event / clear-active | Exclusive | Exclusive |
| recompute_active_statuses | Exclusive | None |
| merge_players / undo_merge | Exclusive, all affected slugs | Exclusive |
| resolve_live_duplicate (T-068: stub + merge/dismissal + audit, one transaction) | Exclusive, affected slugs + the check-in's event | Exclusive |

ID-only writers read the slug without row locks, acquire event locks, then recheck the row.
Merge/undo discover affected slugs without row locks and recheck after acquiring global.
If a new slug appeared, roll back the lock-only savepoint and retry, at most three times.
Exhaustion raises a clear error and rolls back; merge/undo use a 10-second lock timeout.
Ordinary writers share the global lock and can proceed concurrently.

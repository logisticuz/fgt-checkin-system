-- migrate:allow-destructive Remove duplicate dismissals referring to player profiles that no longer exist
-- Keep every decision whose two original profiles still exist. Re-running is harmless.
DELETE FROM player_duplicate_dismissals AS dismissal
WHERE NOT EXISTS (SELECT 1 FROM players WHERE uuid = dismissal.uuid_a)
   OR NOT EXISTS (SELECT 1 FROM players WHERE uuid = dismissal.uuid_b);

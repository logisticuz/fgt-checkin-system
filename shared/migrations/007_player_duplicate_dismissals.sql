-- "Not same" decisions from the duplicate tool (T-056). Version 006 is reserved for T-053.
-- A pair is stored ordered (uuid_a < uuid_b, byte order = Python sort order) so (A, B) and (B, A) are the same row.
-- No foreign keys: a merge deletes the removed player and undo_merge restores the same uuid,
-- so the decision must survive that round trip. Readers join players to hide stale pairs.
-- No contact data: only the two player uuids, when, and the dashboard user's display name.
CREATE TABLE IF NOT EXISTS player_duplicate_dismissals (
    id           SERIAL PRIMARY KEY,
    uuid_a       TEXT NOT NULL,
    uuid_b       TEXT NOT NULL,
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dismissed_by TEXT NOT NULL DEFAULT '',
    CONSTRAINT player_duplicate_dismissals_ordered CHECK (uuid_a COLLATE "C" < uuid_b COLLATE "C"),
    CONSTRAINT player_duplicate_dismissals_pair_unique UNIQUE (uuid_a, uuid_b)
);

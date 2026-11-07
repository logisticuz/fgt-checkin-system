-- Live "possible duplicate" hints for the active event's check-ins (T-068).
-- 009 = T-065, 010 = T-015, 011 = reserved for T-067.
-- One row per (check-in, candidate player). candidate_uuid '' marks "checked, no candidate".
-- `fingerprint` is an opaque hash of the check-in's identity and the player/"Not same"
-- context when it was checked, so a row is only re-checked after one of those changed.
-- No contact data is stored: only ids, the match reasons and the confidence.
-- Readers re-check candidates and dismissals.
CREATE TABLE IF NOT EXISTS live_duplicate_hints (
    id             SERIAL PRIMARY KEY,
    event_slug     TEXT NOT NULL,
    record_id      TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    candidate_uuid TEXT NOT NULL DEFAULT '',
    reasons        TEXT[] NOT NULL DEFAULT '{}',
    confidence     TEXT NOT NULL DEFAULT 'low',
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT live_duplicate_hints_pair_unique UNIQUE (record_id, candidate_uuid)
);
CREATE INDEX IF NOT EXISTS live_duplicate_hints_event_idx ON live_duplicate_hints (event_slug);

-- The TO's decision lives only in the existing merge history. Archive resolves:
-- 1) a check-in the decision covered (record_id -> identity hash at decision time),
-- 2) a merged-away profile's tag/email to the profile that absorbed it.
ALTER TABLE merge_log ADD COLUMN IF NOT EXISTS confirmed_rows JSONB;
CREATE INDEX IF NOT EXISTS merge_log_confirmed_rows_idx ON merge_log USING gin (confirmed_rows);
CREATE INDEX IF NOT EXISTS players_lower_email_idx ON players (lower(email));
CREATE INDEX IF NOT EXISTS merge_log_removed_tag_idx
    ON merge_log (lower(removed_player_snapshot ->> 'tag'));
CREATE INDEX IF NOT EXISTS merge_log_removed_email_idx
    ON merge_log (lower(removed_player_snapshot ->> 'email'));

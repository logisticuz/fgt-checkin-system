-- Live "possible duplicate" hints for the active event's check-ins (T-068).
-- 009 = T-065, 010 = T-015, 011 = reserved for T-067.
-- One row per (check-in, candidate player). candidate_uuid '' marks "checked, no candidate".
-- `fingerprint` is the check-in's identity (name|tag|phone|player_uuid) when it was checked,
-- so a row is only re-checked after one of those fields changes. No contact data is stored:
-- only ids, the match reasons and the confidence. Readers re-check candidates and dismissals.
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

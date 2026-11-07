-- No FK to the active table: unresolved submissions must survive archive/reopen.
-- Provider inputs (including personal IDs and contact details) never belong here.
CREATE TABLE IF NOT EXISTS legacy_register_attempts (
    checkin_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN (
        'in_flight', 'accepted', 'rejected', 'unknown', 'resolved'
    )),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_by TEXT CHECK (resolved_by IN ('to', 'reconcile'))
);

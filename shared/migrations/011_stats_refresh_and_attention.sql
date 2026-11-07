-- Only failed refreshes leave a row. It is a visible degraded state, not a job queue.
ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS attendance_recorded BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS event_stats_refresh (
    event_slug TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    requested_by JSONB NOT NULL DEFAULT '{}',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    generation BIGINT NOT NULL DEFAULT 1,
    last_error_code TEXT
);

-- Reopened check-ins are drafts. Preserve the historical snapshot/rows while storing
-- the verified policy and restore-ID map separately until explicit re-archiving.
CREATE TABLE IF NOT EXISTS event_edit_context (
    event_slug TEXT PRIMARY KEY,
    policy JSONB NOT NULL DEFAULT '{}',
    baseline_policy JSONB NOT NULL DEFAULT '{}',
    settings_baseline JSONB NOT NULL DEFAULT '{}',
    restore_links JSONB NOT NULL DEFAULT '[]',
    complete_mirror BOOLEAN NOT NULL DEFAULT false,
    opened_by JSONB NOT NULL DEFAULT '{}',
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Store only an opaque issue key/fingerprint and the authenticated operator.
-- A changed fingerprint makes a previously acknowledged issue visible again.
CREATE TABLE IF NOT EXISTS event_stats_issue_ack (
    issue_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    acknowledged_by TEXT NOT NULL,
    acknowledged_name TEXT,
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolution TEXT NOT NULL
);

-- Correlate callbacks with the current run and retain authoritative membership decisions.
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_ebas_attempt_id TEXT;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_startgg_attempt_id TEXT;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_ebas_attempt_started_at TIMESTAMPTZ;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_startgg_attempt_started_at TIMESTAMPTZ;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_ebas_result_at TIMESTAMPTZ;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS last_startgg_result_at TIMESTAMPTZ;
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS member_source TEXT
    CHECK (member_source IN ('v2', 'to', 'ebas_check'));
ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS member_source TEXT
    CHECK (member_source IN ('v2', 'to', 'ebas_check'));

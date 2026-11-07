-- Preserve membership truth separately from the latest automated lookup outcome.
-- No default or backfill: existing rows remain NULL, interpreted as unknown by readers.
ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS member_lookup_state TEXT
    CHECK (member_lookup_state IN (
        'confirmed_member', 'confirmed_nonmember', 'unknown', 'skipped'
    ));
ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS member_lookup_state TEXT
    CHECK (member_lookup_state IN (
        'confirmed_member', 'confirmed_nonmember', 'unknown', 'skipped'
    ));

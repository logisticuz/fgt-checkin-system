-- Legacy archive rows remain unlinked; never infer their original check-in IDs.
ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS source_record_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS event_archive_source_record_unique
    ON event_archive (event_slug, source_record_id)
    WHERE source_record_id IS NOT NULL;

ALTER TABLE merge_log ADD COLUMN IF NOT EXISTS moved_archive_ids INTEGER[];
ALTER TABLE merge_log ADD COLUMN IF NOT EXISTS moved_active_ids TEXT[];
ALTER TABLE merge_log ADD COLUMN IF NOT EXISTS before_totals JSONB;

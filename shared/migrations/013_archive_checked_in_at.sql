-- NULL means the historical check-in time is unknown; no approximate backfill here.
ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS checked_in_at TIMESTAMPTZ;

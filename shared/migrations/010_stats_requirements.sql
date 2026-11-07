-- NULL means historically unknown, never "requirements were disabled".
ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS requirements_snapshot JSONB;
ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS same_series_return_rate NUMERIC(5,2);
ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS same_series_base_count INTEGER;
ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS same_series_returned_players INTEGER;

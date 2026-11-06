-- Stop concurrent inserts until the duplicate check and unique index commit together.
LOCK TABLE active_event_data IN SHARE MODE;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM active_event_data
        WHERE tag IS NOT NULL
        GROUP BY event_slug, lower(tag)
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'Duplicate check-in tags prevent the unique index. Run python scripts/report_duplicate_checkins.py --dry-run and resolve duplicates manually.';
    END IF;
END
$$;
CREATE UNIQUE INDEX IF NOT EXISTS active_event_data_event_tag_unique
    ON active_event_data (event_slug, lower(tag));

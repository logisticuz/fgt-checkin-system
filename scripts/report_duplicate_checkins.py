"""Read-only duplicate report: event slugs, tags and counts; never participant profiles."""

import argparse
import json
import os

import psycopg


def find_duplicate_tags(connection, event_slug=None):
    rows = connection.execute(
        """SELECT event_slug, lower(tag), count(*)
           FROM active_event_data
           WHERE tag IS NOT NULL AND (%s::text IS NULL OR event_slug = %s)
           GROUP BY event_slug, lower(tag)
           HAVING count(*) > 1
           ORDER BY event_slug, lower(tag)""",
        (event_slug, event_slug),
    ).fetchall()
    return [{"event_slug": slug, "tag": tag, "count": count} for slug, tag, count in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", default=True, help="Read-only (always enabled)"
    )
    parser.add_argument("--event", help="Limit the report to one event slug")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            connection.read_only = True
            duplicates = find_duplicate_tags(connection, args.event)
    except psycopg.Error:
        parser.exit(1, "Unable to read duplicate check-in tags; check database access.\n")
    print(json.dumps({"dry_run": True, "duplicates": duplicates}, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

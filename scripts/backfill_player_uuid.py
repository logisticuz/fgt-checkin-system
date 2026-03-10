"""
Backfill player_uuid on event_archive rows that are missing it.

Matches archive rows to existing players by tag (case-insensitive),
then email (case-insensitive). Does NOT create new player profiles —
only links to already-known players.

Usage:
    # Dry-run (default): show what would change, no writes
    python scripts/backfill_player_uuid.py

    # Write mode: actually update the rows
    python scripts/backfill_player_uuid.py --write

    # Verbose: show each row being matched
    python scripts/backfill_player_uuid.py --verbose

Runs against DATABASE_URL from environment (same as the app).
"""

import argparse
import os
import sys

# Allow importing shared module from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main():
    parser = argparse.ArgumentParser(description="Backfill player_uuid in event_archive")
    parser.add_argument(
        "--write", action="store_true", help="Actually write changes (default is dry-run)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show each row being matched"
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set in environment")
        sys.exit(1)

    import psycopg  # type: ignore

    conn = psycopg.connect(db_url, autocommit=False)
    cur = conn.cursor()

    # Load all players into a lookup table (case-insensitive)
    cur.execute("SELECT uuid, LOWER(COALESCE(tag, '')), LOWER(COALESCE(email, '')) FROM players")
    player_rows = cur.fetchall()

    tag_to_uuid = {}
    email_to_uuid = {}
    for p_uuid, p_tag, p_email in player_rows:
        if p_tag:
            tag_to_uuid.setdefault(p_tag, p_uuid)
        if p_email:
            email_to_uuid.setdefault(p_email, p_uuid)

    print(f"Loaded {len(player_rows)} player profiles")
    print(f"  - {len(tag_to_uuid)} unique tags")
    print(f"  - {len(email_to_uuid)} unique emails")
    print()

    # Find archive rows missing player_uuid
    cur.execute(
        """
        SELECT id, name, tag, email, event_slug
        FROM event_archive
        WHERE player_uuid IS NULL
        ORDER BY id
        """
    )
    missing_rows = cur.fetchall()

    total_missing = len(missing_rows)
    matched = 0
    unmatched = 0
    updates = []

    for row_id, name, tag, email, event_slug in missing_rows:
        found_uuid = None

        if tag:
            found_uuid = tag_to_uuid.get(tag.lower())

        if not found_uuid and email:
            found_uuid = email_to_uuid.get(email.lower())

        if found_uuid:
            matched += 1
            updates.append((found_uuid, row_id))
            if args.verbose:
                print(f"  MATCH  id={row_id}  tag={tag!r}  name={name!r}  -> {found_uuid[:8]}...")
        else:
            unmatched += 1
            if args.verbose:
                print(f"  MISS   id={row_id}  tag={tag!r}  name={name!r}  email={email!r}")

    print(f"Results:")
    print(f"  Total archive rows missing player_uuid: {total_missing}")
    print(f"  Matched to existing player:             {matched}")
    print(f"  No match found:                         {unmatched}")
    print()

    if not updates:
        print("Nothing to update.")
        conn.close()
        return

    if args.write:
        print(f"WRITING {len(updates)} updates...")
        cur.executemany(
            "UPDATE event_archive SET player_uuid = %s WHERE id = %s",
            updates,
        )
        conn.commit()
        print("Done. Changes committed.")
    else:
        print("DRY RUN — no changes written. Use --write to apply.")

    conn.close()


if __name__ == "__main__":
    main()

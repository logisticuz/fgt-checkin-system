"""Read-only deployment gate: migrations 001-004 and duplicate check-in tags."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import migrate

REQUIRED = frozenset({"001", "002", "003", "004"})


def inspect_database(connection, *, verbose=False):
    """Inspect one consistent read-only snapshot; never apply or create anything."""
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        states = migrate.status(connection, migrations_dir=ROOT / "shared" / "migrations")
        pending = [state.filename for state in states if state.applied_at is None]
        applied = {state.version for state in states if state.applied_at is not None}
        missing = sorted(REQUIRED - applied)
        exists = connection.execute("SELECT to_regclass('active_event_data')").fetchone()[0]
        duplicates = []
        if exists is not None:
            duplicates = connection.execute(
                """SELECT lower(tag), count(*) FROM active_event_data
                   WHERE tag IS NOT NULL GROUP BY event_slug, lower(tag)
                   HAVING count(*) > 1 ORDER BY count(*) DESC, lower(tag)"""
            ).fetchall()
        ready = not pending and not missing and exists is not None and not duplicates
        report = {
            "read_only": True,
            "ready_to_deploy": ready,
            "pending_migrations": pending,
            "required_unapplied": missing,
            "active_table_present": exists is not None,
            "duplicate_groups": len(duplicates),
            "duplicate_rows": sum(count for _, count in duplicates),
        }
        if verbose:
            report["duplicates"] = [{"tag": tag, "count": count} for tag, count in duplicates]
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Include duplicate tags in output")
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection:
            report = inspect_database(connection, verbose=args.verbose)
    except (psycopg.Error, migrate.MigrationError):
        print(
            json.dumps({"read_only": True, "ready_to_deploy": False, "error": "preflight_failed"})
        )
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["ready_to_deploy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

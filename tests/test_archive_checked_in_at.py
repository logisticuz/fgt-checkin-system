"""Archive arrival times use synthetic data in the disposable Postgres schema."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from shared import migrate
from shared import postgres_api as postgres
from tests.test_safe_archiving import archive, seed_archive, table_rows

pytestmark = pytest.mark.db
ARRIVAL = datetime(2026, 11, 7, 15, 12, 34, 567890, tzinfo=timezone.utc)
LATE_ARRIVAL = datetime(2026, 11, 8, 9, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def event_db(pg_db):
    migrate.apply(pg_db.pool)
    pg_db.seed_settings(active_event_slug="test-arrival", event_date="2026-11-07")
    return pg_db


def test_archive_copies_the_checkin_time_without_changing_archive_time(event_db):
    active = event_db.seed_checkin(event_slug="test-arrival", created=ARRIVAL)

    archive("test-arrival", clear_active=True)

    saved = table_rows(event_db, "event_archive")[0]
    assert saved["source_record_id"] == active["record_id"]
    assert saved["checked_in_at"] == ARRIVAL
    assert saved["archived_at"] > ARRIVAL
    assert event_db.checkins() == []


def test_rearchive_preserves_original_arrival_and_copies_a_new_late_arrival(event_db):
    original = event_db.seed_checkin(event_slug="test-arrival", created=ARRIVAL)
    archive("test-arrival", clear_active=True)
    original_archive = table_rows(event_db, "event_archive")[0]

    postgres.reopen_event("test-arrival")
    assert event_db.checkins()[0]["created"] == ARRIVAL
    with event_db.pool.connection() as connection:
        connection.execute(
            "UPDATE active_event_data SET created=%s, name=%s WHERE record_id=%s",
            (LATE_ARRIVAL, "Test Corrected Player", original["record_id"]),
        )
    added = event_db.seed_checkin(event_slug="test-arrival", created=LATE_ARRIVAL)
    archive("test-arrival", clear_active=True)

    rows = {row["source_record_id"]: row for row in table_rows(event_db, "event_archive")}
    assert len(rows) == 2
    assert rows[original["record_id"]]["id"] == original_archive["id"]
    assert rows[original["record_id"]]["checked_in_at"] == ARRIVAL
    assert rows[original["record_id"]]["name"] == "Test Corrected Player"
    assert rows[added["record_id"]]["checked_in_at"] == LATE_ARRIVAL


@pytest.mark.parametrize("source_record_id", ["test-legacy-source", None])
@pytest.mark.parametrize("checked_in_at", [None, ARRIVAL])
def test_legacy_roundtrip_preserves_known_or_unknown_arrival(
    event_db, source_record_id, checked_in_at
):
    archive_id = seed_archive(
        event_db,
        event_slug="test-arrival",
        source_record_id=source_record_id,
        checked_in_at=checked_in_at,
        tag="test-legacy-arrival",
    )

    postgres.reopen_event("test-arrival")
    restored = event_db.checkins()
    assert len(restored) == 1
    if checked_in_at is not None:
        assert restored[0]["created"] == checked_in_at
    else:
        assert restored[0]["created"] is not None
    with event_db.pool.connection() as connection:
        connection.execute(
            "UPDATE active_event_data SET created=%s WHERE record_id=%s",
            (LATE_ARRIVAL, restored[0]["record_id"]),
        )
    archive("test-arrival", clear_active=True)

    saved = table_rows(event_db, "event_archive")
    assert len(saved) == 1
    assert saved[0]["id"] == archive_id
    assert saved[0]["checked_in_at"] == checked_in_at


def test_migration_013_is_nullable_has_no_backfill_and_is_idempotent(pg_db, tmp_path):
    filename = "013_archive_checked_in_at.sql"
    migration_sql = (
        Path(__file__).resolve().parents[1] / "shared" / "migrations" / filename
    ).read_text(encoding="utf-8")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / filename).write_text(migration_sql, encoding="utf-8")

    with pg_db.pool.connection() as connection, connection.transaction(force_rollback=True):
        connection.execute("ALTER TABLE event_archive DROP COLUMN checked_in_at")
        legacy_id = connection.execute(
            "INSERT INTO event_archive (event_slug, name) VALUES (%s, %s) RETURNING id",
            ("test-migration-arrival", "Test Legacy Player"),
        ).fetchone()[0]
        assert [state.version for state in migrate.check(connection, migrations)] == ["013"]
        assert (
            connection.execute(
                """SELECT count(*) FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name='event_archive'
                 AND column_name='checked_in_at'"""
            ).fetchone()[0]
            == 0
        )

        assert migrate.apply(connection, migrations) == [filename]
        assert migrate.apply(connection, migrations) == []
        connection.execute(migration_sql, prepare=False)

        assert connection.execute(
            "SELECT checked_in_at FROM event_archive WHERE id=%s", (legacy_id,)
        ).fetchone() == (None,)
        assert (
            connection.execute(
                """SELECT data_type, is_nullable, column_default FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name='event_archive'
                 AND column_name='checked_in_at'"""
            ).fetchone()
            == ("timestamp with time zone", "YES", None)
        )

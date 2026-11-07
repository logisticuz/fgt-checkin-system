"""Deployment gate checks run against only the fixture's disposable PG schema."""

import json
from contextlib import nullcontext

import pytest

from scripts import migration_preflight as preflight

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def applied_schema(pg_db):
    # pg_db truncates migration history along with application rows before each test.
    preflight.migrate.apply(pg_db.pool)


def run_cli(database, monkeypatch, arguments=()):
    with database.pool.connection() as connection:
        monkeypatch.setattr(
            preflight.psycopg, "connect", lambda *args, **kwargs: nullcontext(connection)
        )
        return preflight.main(list(arguments))


def test_fully_applied_schema_passes_in_a_read_only_snapshot(pg_db, monkeypatch, capsys):
    original = preflight.migrate.status

    def read_only_status(connection, **kwargs):
        assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        return original(connection, **kwargs)

    monkeypatch.setattr(preflight.migrate, "status", read_only_status)
    assert run_cli(pg_db, monkeypatch) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_to_deploy"] is True
    assert result["pending_migrations"] == result["required_unapplied"] == []
    assert result["duplicate_groups"] == result["duplicate_rows"] == 0


@pytest.mark.parametrize("version", ["001", "002", "003", "004"])
def test_any_required_pending_migration_blocks_deployment(pg_db, monkeypatch, capsys, version):
    with pg_db.pool.connection() as connection:
        previous = connection.execute(
            "DELETE FROM fgt_schema_migrations WHERE version = %s RETURNING *", (version,)
        ).fetchone()
    try:
        assert run_cli(pg_db, monkeypatch) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["ready_to_deploy"] is False
        assert result["required_unapplied"] == [version]
        assert len(result["pending_migrations"]) == 1
        with pg_db.pool.connection() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM fgt_schema_migrations WHERE version=%s", (version,)
                ).fetchone()[0]
                == 0
            )
    finally:
        with pg_db.pool.connection() as connection:
            connection.execute(
                "INSERT INTO fgt_schema_migrations (version, filename, sha256, applied_at) VALUES (%s, %s, %s, %s)",
                previous,
            )


def test_duplicate_precondition_blocks_and_tags_require_verbose(pg_db, monkeypatch, capsys):
    pg_db.seed_settings()
    with pg_db.pool.connection() as connection:
        connection.execute("DROP INDEX active_event_data_event_tag_unique")
    try:
        first = pg_db.seed_checkin(tag="Synthetic-Private-Tag")
        second = pg_db.seed_checkin(event_slug=first["event_slug"], tag="synthetic-private-tag")
        assert run_cli(pg_db, monkeypatch) == 1
        output = capsys.readouterr().out
        result = json.loads(output)
        assert result["duplicate_groups"] == 1 and result["duplicate_rows"] == 2
        assert "synthetic-private-tag" not in output.lower() and "duplicates" not in result
        assert run_cli(pg_db, monkeypatch, ["--verbose"]) == 1
        verbose = json.loads(capsys.readouterr().out)
        assert verbose["duplicates"] == [{"tag": "synthetic-private-tag", "count": 2}]
        assert len(pg_db.checkins()) == 2
    finally:
        with pg_db.pool.connection() as connection:
            connection.execute(
                "DELETE FROM active_event_data WHERE lower(tag) = 'synthetic-private-tag'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX active_event_data_event_tag_unique ON active_event_data (event_slug, lower(tag))"
            )


def test_checksum_error_blocks_without_exposing_database_details(pg_db, monkeypatch, capsys):
    with pg_db.pool.connection() as connection:
        previous = connection.execute(
            "SELECT sha256 FROM fgt_schema_migrations WHERE version='004'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE fgt_schema_migrations SET sha256='synthetic-private-checksum' WHERE version='004'"
        )
    try:
        assert run_cli(pg_db, monkeypatch) == 1
        output = capsys.readouterr().out
        assert json.loads(output)["error"] == "preflight_failed"
        assert "synthetic-private-checksum" not in output
    finally:
        with pg_db.pool.connection() as connection:
            connection.execute(
                "UPDATE fgt_schema_migrations SET sha256=%s WHERE version='004'", (previous,)
            )

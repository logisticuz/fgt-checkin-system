"""Migration lint/CLI/startup checks and disposable-schema Postgres integration tests."""

import importlib
import logging
import runpy
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from unittest.mock import MagicMock

import psycopg
import psycopg_pool
import pytest

from shared import migrate

_PRODUCTION_LOCK_ID = migrate.LOCK_ID


@pytest.fixture(autouse=True)
def isolated_migration_directory(monkeypatch):
    monkeypatch.delenv("FGT_MIGRATIONS_DIR", raising=False)
    # Keep the real session lock/unlock path, with a key private to this test run.
    monkeypatch.setattr(
        migrate, "LOCK_ID", int.from_bytes(uuid.uuid4().bytes[:8], "big", signed=True)
    )


def write_migration(directory, filename, source):
    (directory / filename).write_text(source, encoding="utf-8")
    return filename


@pytest.mark.parametrize(
    "source",
    [
        "DROP TABLE fgt_example;",
        "dRoP\nTABLE fgt_example;",
        "TRUNCATE fgt_example;",
        "DELETE\nFROM fgt_example;",
        "ALTER TABLE fgt_example RENAME COLUMN first TO second;",
        "ALTER TABLE fgt_example ALTER COLUMN value TYPE bigint;",
        'ALTER TABLE fgt_example ALTER COLUMN "value" SET DATA TYPE bigint;',
        "ALTER TABLE fgt_example ALTER value TYPE bigint;",
        "ALTER TABLE meetup_events ADD COLUMN example text;",
        'ALTER TABLE public."meetup_events" ADD COLUMN example text;',
        'ALTER TABLE IF EXISTS ONLY "public"."MEETUP_events" ADD COLUMN example text;',
        "ALTER TABLE card_ids ADD COLUMN example text;",
        'ALTER TABLE public."card_ids_archive" ADD COLUMN example text;',
        "DELETE /* harmless separator */ FROM fgt_example;",
        "ALTER /* separator */ TABLE meetup_events ADD COLUMN example text;",
        "SELECT '--'; DROP TABLE fgt_example;",
        "SELECT '/*'; DROP TABLE fgt_example; SELECT '*/';",
        "SELECT $$--$$; DROP TABLE fgt_example;",
        "DO $$ BEGIN ALTER/* separator */TABLE meetup_events ADD COLUMN x int; END $$;",
        'ALTER TABLE U&"meetup_review_probe" ADD COLUMN note text;',
        'ALTER TABLE U&"meetup_\\0072eview_probe" ADD COLUMN note text;',
        "ALTER TABLE fgt_review_probe ALTER COLUMN mängd TYPE bigint;",
        "ALTER TABLE fgt_review_probe ALTER mängd TYPE bigint;",
        'ALTER TABLE "meétup_review_probe" ADD COLUMN note text;',
        "ALTER TABLE möte ADD COLUMN note text;",
        'ALTER TABLE "Other Application" ADD COLUMN note text;',
    ],
)
def test_lint_rejects_forbidden_changes(source):
    with pytest.raises(migrate.MigrationError, match="001_forbidden.sql"):
        migrate.lint(source, "001_forbidden.sql")


@pytest.mark.parametrize(
    "source",
    [
        "-- DROP TABLE example;\nSELECT 1;",
        "/* DELETE FROM example; */ CREATE TABLE example (id integer);",
        "/* outer /* inner */ DROP TABLE example; */ SELECT 1;",
        "ALTER TABLE example ADD COLUMN extra text;",
        "DO $$ BEGIN PERFORM 1; /* DROP TABLE example; */ END $$;",
        "SELECT 'text; COMMIT;';",
        'ALTER TABLE "players" ADD COLUMN extra text;',
        'ALTER TABLE "fgt_årlig" ADD COLUMN extra text;',
    ],
)
def test_lint_accepts_additive_sql_and_ignores_comments(source):
    migrate.lint(source, "001_additive.sql")


def test_destructive_override_requires_a_reason_and_logs_loudly(caplog):
    source = "-- migrate:allow-destructive remove a synthetic obsolete fixture\nDROP TABLE example;"
    with caplog.at_level(logging.WARNING, logger="shared.migrate"):
        migrate.lint(source, "001_override.sql")
    assert "DESTRUCTIVE MIGRATION OVERRIDE" in caplog.text
    assert "001_override.sql" in caplog.text
    assert "remove a synthetic obsolete fixture" in caplog.text


@pytest.mark.parametrize(
    "prefix",
    [
        "-- migrate:allow-destructive",
        "-- migrate:allow-destructive   ",
        "\n-- migrate:allow-destructive reason",
    ],
)
def test_override_must_be_first_line_with_a_nonempty_reason(prefix):
    with pytest.raises(migrate.MigrationError):
        migrate.lint(prefix + "\nDROP TABLE example;", "001_invalid_override.sql")


@pytest.mark.parametrize("statement", ["BEGIN", "START TRANSACTION", "COMMIT", "ROLLBACK", "END"])
def test_files_cannot_override_runner_transaction_boundaries(statement):
    with pytest.raises(migrate.MigrationError, match="transaction boundaries"):
        migrate.lint(
            "-- migrate:allow-destructive reviewed fixture change\nSELECT 1; " + statement + ";"
        )


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n"])
def test_comment_newlines_cannot_hide_destructive_or_transaction_sql(newline):
    with pytest.raises(migrate.MigrationError, match="DROP"):
        migrate.lint("-- ordinary comment" + newline + "DROP TABLE fgt_review_probe;")
    source = (
        "CREATE TEMP TABLE fgt_migration_commit_probe (id integer); -- comment"
        + newline
        + "COMMIT; SELECT 1/0;"
    )
    with pytest.raises(migrate.MigrationError, match="transaction boundaries"):
        migrate.lint(source)


@pytest.mark.parametrize(
    "source",
    [
        'ALTER TABLE U&"meetup_review_probe" ADD COLUMN note text;',
        'ALTER TABLE "meétup_review_probe" ADD COLUMN note text;',
        'ALTER TABLE "meetup_review_probe" ADD COLUMN note text;',
    ],
)
def test_override_does_not_allow_unsupported_identifiers_or_sibling_tables(source):
    with pytest.raises(migrate.MigrationError):
        migrate.lint("-- migrate:allow-destructive synthetic reviewed change\n" + source)


@pytest.mark.parametrize("service", ["backend", "dashboard"])
def test_migrations_are_available_in_shared_only_container_layout(tmp_path, service):
    shared = tmp_path / service / "shared"
    shared.mkdir(parents=True)
    shutil.copy2(migrate.__file__, shared / "migrate.py")
    shutil.copytree(migrate.MIGRATIONS_DIR, shared / "migrations")
    packaged = runpy.run_path(str(shared / "migrate.py"))
    assert not (shared.parent / "db").exists()
    files = packaged["_load_migrations"](packaged["MIGRATIONS_DIR"])
    names = [migration.filename for migration in files]
    # The baseline must ship and sort first; later track migrations (001_, 002_ …) may follow.
    assert names[0] == "000_baseline.sql"
    assert names == sorted(names)


def test_environment_overrides_migration_directory(monkeypatch, tmp_path):
    write_migration(tmp_path, "007_environment.sql", "SELECT 1;")
    monkeypatch.setenv("FGT_MIGRATIONS_DIR", str(tmp_path))
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (None,)
    assert [state.filename for state in migrate.check(connection)] == ["007_environment.sql"]


def test_missing_environment_directory_still_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("FGT_MIGRATIONS_DIR", str(tmp_path / "missing"))
    with pytest.raises(migrate.MigrationError, match="directory does not exist"):
        migrate.apply(None)


@pytest.mark.parametrize("filename", ["1_short.sql", "001.sql", "1000_long.sql"])
def test_invalid_filenames_fail_before_using_database(tmp_path, filename):
    write_migration(tmp_path, filename, "SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="NNN_<vertical>"):
        migrate.apply(None, tmp_path)


def test_duplicate_versions_fail_before_using_database(tmp_path):
    write_migration(tmp_path, "001_first.sql", "SELECT 1;")
    write_migration(tmp_path, "001_second.sql", "SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="duplicate migration version"):
        migrate.apply(None, tmp_path)


def test_all_files_are_linted_before_any_database_write(tmp_path):
    write_migration(tmp_path, "001_valid.sql", "CREATE TABLE example (id integer);")
    write_migration(tmp_path, "002_invalid.sql", "DROP TABLE example;")
    pool = MagicMock()
    connection = pool.connection.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.return_value = (None,)
    with pytest.raises(migrate.MigrationError, match="002_invalid.sql"):
        migrate.apply(pool, tmp_path)
    queries = [call.args[0] for call in connection.execute.call_args_list]
    assert queries
    assert all(query.startswith("SELECT") for query in queries)


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(migrate.MigrationError, match="directory does not exist"):
        migrate.apply(None, tmp_path / "missing")


@pytest.mark.parametrize(("enabled", "expected"), [(None, True), ("1", True), ("0", False)])
def test_storage_hook_runs_after_legacy_migrations_and_respects_gate(
    monkeypatch, enabled, expected
):
    postgres = importlib.import_module("shared.postgres_api")
    pool = MagicMock()
    events = []
    monkeypatch.setattr(postgres, "_pool", None)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", lambda **kwargs: pool)
    monkeypatch.setattr(postgres, "_run_migrations", lambda source: events.append("legacy"))
    monkeypatch.setattr(migrate, "apply", lambda source: events.append("files"))
    if enabled is None:
        monkeypatch.delenv("FGT_MIGRATIONS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("FGT_MIGRATIONS_ENABLED", enabled)
    assert postgres._get_pool() is pool
    assert events == (["legacy", "files"] if expected else ["legacy"])


def test_failed_startup_is_logged_and_cannot_be_bypassed_by_a_later_call(monkeypatch, caplog):
    postgres = importlib.import_module("shared.postgres_api")
    pool = MagicMock()
    monkeypatch.setattr(postgres, "_pool", None)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", lambda **kwargs: pool)
    monkeypatch.setattr(postgres, "_run_migrations", lambda source: None)
    monkeypatch.setenv("FGT_MIGRATIONS_ENABLED", "1")

    def fail_loading(directory):
        raise migrate.MigrationError("001_broken.sql: synthetic startup failure")

    monkeypatch.setattr(migrate, "_load_migrations", fail_loading)
    with caplog.at_level(logging.ERROR, logger="shared.migrate"):
        for _ in range(2):
            with pytest.raises(migrate.MigrationError, match="001_broken.sql"):
                postgres._get_pool()
            assert postgres._pool is None
    assert pool.close.call_count == 2
    assert "FGT file-based migrations failed" in caplog.text


@pytest.mark.parametrize("command", ["status", "apply", "check"])
def test_cli_selects_operation_without_real_connections(monkeypatch, capsys, command):
    connection = MagicMock()
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(psycopg, "connect", connect)
    operation = MagicMock()
    if command == "apply":
        operation.return_value = ["000_baseline.sql"]
    else:
        operation.return_value = [migrate.MigrationStatus("000", "000_baseline.sql", "digest")]
    monkeypatch.setattr(migrate, command, operation)
    assert migrate.main([command]) == 0
    operation.assert_called_once_with(connection)
    assert "000_baseline.sql" in capsys.readouterr().out


def test_cli_returns_nonzero_for_missing_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert migrate.main(["check"]) != 0


def test_cli_returns_nonzero_for_migration_errors(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(migrate, "check", MagicMock(side_effect=migrate.MigrationError("bad file")))
    assert migrate.main(["check"]) != 0


def test_apply_is_ordered_and_idempotent(pg_db, tmp_path):
    write_migration(tmp_path, "002_extra.sql", "ALTER TABLE migration_once ADD COLUMN extra text;")
    write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_once (id integer); INSERT INTO migration_once VALUES (1);",
    )
    assert migrate.apply(pg_db.pool, tmp_path) == ["001_first.sql", "002_extra.sql"]
    assert migrate.apply(pg_db.pool, tmp_path) == []
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM migration_once").fetchone()[0] == 1
        rows = connection.execute(
            "SELECT version, filename, length(sha256), applied_at "
            "FROM fgt_schema_migrations ORDER BY version"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("001", "001_first.sql", 64),
        ("002", "002_extra.sql", 64),
    ]
    assert all(isinstance(row[3], datetime) for row in rows)


def test_failed_file_rolls_back_and_releases_lock(pg_db, tmp_path):
    write_migration(tmp_path, "001_prior.sql", "CREATE TABLE migration_prior (id integer);")
    write_migration(
        tmp_path,
        "002_broken.sql",
        "CREATE TABLE migration_rollback (id integer); "
        "INSERT INTO migration_missing_table VALUES (1);",
    )
    with pg_db.pool.connection() as probe:
        with pytest.raises(migrate.MigrationError, match="002_broken.sql.*migration_missing_table"):
            migrate.apply(pg_db.pool, tmp_path)
        assert probe.execute("SELECT to_regclass('migration_rollback')").fetchone()[0] is None
        assert probe.execute(
            "SELECT filename FROM fgt_schema_migrations ORDER BY version"
        ).fetchall() == [("001_prior.sql",)]
        # Another suite may acquire the production key between apply() and this probe.
        with pg_db.pool.connection() as foreign:
            foreign.execute("SELECT pg_advisory_lock(%s)", (_PRODUCTION_LOCK_ID,))
            try:
                assert probe.execute(
                    "SELECT pg_try_advisory_lock(%s)", (migrate.LOCK_ID,)
                ).fetchone()[0]
                probe.execute("SELECT pg_advisory_unlock(%s)", (migrate.LOCK_ID,))
            finally:
                foreign.execute("SELECT pg_advisory_unlock(%s)", (_PRODUCTION_LOCK_ID,))


@pytest.mark.parametrize("edited_sql", ["SELECT 2;", "DROP TABLE example;"])
def test_edited_applied_file_is_rejected(pg_db, tmp_path, edited_sql):
    filename = write_migration(tmp_path, "001_original.sql", "SELECT 1;")
    migrate.apply(pg_db.pool, tmp_path)
    write_migration(tmp_path, filename, edited_sql)
    with pytest.raises(migrate.MigrationError, match="001_original.sql: edited after apply"):
        migrate.apply(pg_db.pool, tmp_path)
    with pytest.raises(migrate.MigrationError, match="edited after apply"):
        migrate.check(pg_db.pool, tmp_path)


def test_check_and_status_work_on_a_read_only_connection(pg_db, tmp_path):
    write_migration(tmp_path, "001_pending.sql", "CREATE TABLE migration_readonly (id integer);")
    with pg_db.pool.connection() as connection:
        connection.execute("SET default_transaction_read_only = on")
        try:
            states = migrate.status(connection, tmp_path)
            pending = migrate.check(connection, tmp_path)
            assert states == pending
            assert [(state.filename, state.applied_at) for state in states] == [
                ("001_pending.sql", None)
            ]
            assert (
                connection.execute("SELECT to_regclass('migration_readonly')").fetchone()[0] is None
            )
        finally:
            connection.execute("SET default_transaction_read_only = off")


def test_concurrent_apply_calls_hold_the_lock_for_the_entire_run(pg_db, tmp_path):
    write_migration(
        tmp_path,
        "001_first.sql",
        "CREATE TABLE migration_concurrent (id integer); "
        "INSERT INTO migration_concurrent VALUES (1); SELECT pg_sleep(0.1);",
    )
    write_migration(tmp_path, "002_second.sql", "INSERT INTO migration_concurrent VALUES (2);")
    barrier = Barrier(2)

    def apply_at_once():
        barrier.wait(timeout=5)
        return migrate.apply(pg_db.pool, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_at_once) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(map(len, results)) == [0, 2]
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM migration_concurrent").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM fgt_schema_migrations").fetchone()[0] == 2


def test_destructive_override_is_applied_and_recorded(pg_db, tmp_path, caplog):
    with pg_db.pool.connection() as connection:
        connection.execute("CREATE TABLE migration_override (id integer)")
    write_migration(
        tmp_path,
        "001_override.sql",
        "-- migrate:allow-destructive remove a synthetic test-owned table\n"
        "DROP TABLE migration_override;",
    )
    with caplog.at_level(logging.WARNING, logger="shared.migrate"):
        assert migrate.apply(pg_db.pool, tmp_path) == ["001_override.sql"]
    assert "DESTRUCTIVE MIGRATION OVERRIDE" in caplog.text
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT to_regclass('migration_override')").fetchone()[0] is None


def test_baseline_records_a_noop(pg_db, tmp_path):
    # Baseline-only directory, so later packaged migrations don't change this test's meaning.
    shutil.copy(migrate.MIGRATIONS_DIR / "000_baseline.sql", tmp_path / "000_baseline.sql")
    assert migrate.apply(pg_db.pool, tmp_path) == ["000_baseline.sql"]
    states = migrate.status(pg_db.pool, tmp_path)
    assert len(states) == 1
    assert states[0].filename == "000_baseline.sql"
    assert states[0].applied_at is not None
    assert states[0].applied_at.tzinfo is not None


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_cr_comments_cannot_drop_an_existing_table(pg_db, tmp_path, newline):
    with pg_db.pool.connection() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS fgt_review_probe (id integer)")
        connection.execute("INSERT INTO fgt_review_probe VALUES (1)")
    write_migration(
        tmp_path,
        "001_cr_drop.sql",
        "-- ordinary comment" + newline + "DROP TABLE fgt_review_probe;",
    )
    with pytest.raises(migrate.MigrationError, match="DROP"):
        migrate.apply(pg_db.pool, tmp_path)
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM fgt_review_probe").fetchone()[0] == 1


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_cr_commit_bypass_is_rejected_without_leaving_committed_changes(pg_db, tmp_path, newline):
    write_migration(
        tmp_path,
        "001_cr_commit.sql",
        "CREATE TEMP TABLE fgt_migration_commit_probe (id integer); -- comment"
        + newline
        + "COMMIT; SELECT 1/0;",
    )
    with pg_db.pool.connection() as connection:
        with pytest.raises(migrate.MigrationError, match="transaction boundaries"):
            migrate.apply(connection, tmp_path)
        assert (
            connection.execute(
                "SELECT to_regclass('pg_temp.fgt_migration_commit_probe')"
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize(
    "source",
    [
        'ALTER TABLE U&"meetup_review_probe" ADD COLUMN note text;',
        "ALTER TABLE fgt_review_probe ALTER COLUMN mängd TYPE bigint;",
    ],
)
def test_review_identifier_repros_never_modify_the_database(pg_db, tmp_path, source):
    with pg_db.pool.connection() as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS meetup_review_probe (id integer)')
        connection.execute('CREATE TABLE IF NOT EXISTS fgt_identifier_probe ("mängd" integer)')
    source = source.replace("fgt_review_probe", "fgt_identifier_probe")
    write_migration(tmp_path, "001_identifiers.sql", source)
    with pytest.raises(migrate.MigrationError):
        migrate.apply(pg_db.pool, tmp_path)
    with pg_db.pool.connection() as connection:
        columns = connection.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('meetup_review_probe', 'fgt_identifier_probe')"
        ).fetchall()
    assert set(columns) == {
        ("meetup_review_probe", "id", "integer"),
        ("fgt_identifier_probe", "mängd", "integer"),
    }

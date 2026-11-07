"""Migration 013 preserves exact Undo for unchanged pre-migration archive merges."""

from pathlib import Path

import pytest

from shared import migrate
from shared import postgres_api as postgres
from tests.test_archive_checked_in_at import ARRIVAL
from tests.test_safe_archiving import seed_archive, seed_player, table_rows
from tests.test_stats_recompute import private_stats_lock_namespace  # noqa: F401

pytestmark = pytest.mark.db
MIGRATION = (
    Path(__file__).resolve().parents[1] / "shared" / "migrations" / "013_archive_checked_in_at.sql"
)


@pytest.fixture
def undo_db(pg_db):
    migrate.apply(pg_db.pool)
    pg_db.seed_settings(active_event_slug=None)
    return pg_db


@pytest.fixture
def before_013(undo_db):
    with undo_db.pool.connection() as connection:
        connection.execute("ALTER TABLE event_archive DROP COLUMN checked_in_at")
        connection.execute("DELETE FROM fgt_schema_migrations WHERE version='013'")
    try:
        yield undo_db
    finally:
        with undo_db.pool.connection() as connection:
            connection.execute(MIGRATION.read_text(encoding="utf-8"), prepare=False)


def merged_players(database, **archive_fields):
    keep = seed_player(database, "test-timestamp-keep")
    removed = seed_player(database, "test-timestamp-remove")
    archive_id = seed_archive(
        database,
        event_slug="test-timestamp-undo",
        event_date="2026-11-07",
        player_uuid=removed["uuid"],
        **archive_fields,
    )
    result = postgres.merge_players(keep["uuid"], removed["uuid"])
    return result["merge_id"], archive_id, removed["uuid"]


@pytest.mark.parametrize("degrade_first", [False, True])
def test_pre013_merge_survives_migration_and_optional_stats_healing(
    before_013, monkeypatch, degrade_first
):
    with monkeypatch.context() as patch:
        if degrade_first:

            def fail(*args, **kwargs):
                raise RuntimeError("Synthetic aggregate failure")

            patch.setattr(postgres, "recompute_player_aggregates", fail)
        merge_id, archive_id, removed_uuid = merged_players(before_013)
    before = table_rows(before_013, "event_archive")
    assert "checked_in_at" not in before[0]

    assert migrate.apply(before_013.pool) == [MIGRATION.name]
    after = table_rows(before_013, "event_archive")
    assert [
        {key: value for key, value in row.items() if key != "checked_in_at"} for row in after
    ] == before
    assert all(row["checked_in_at"] is None for row in after)
    if degrade_first:
        assert postgres.refresh_archive_stats()["stale"] is False

    assert postgres.undo_merge(merge_id)["undone"] is True
    restored = table_rows(before_013, "event_archive")
    assert restored[0]["id"] == archive_id
    assert restored[0]["player_uuid"] == removed_uuid
    assert restored[0]["checked_in_at"] is None


@pytest.mark.parametrize("edit", ["payment", "arrival"])
def test_legacy_fingerprint_still_refuses_actual_edits_before_and_after_healing(before_013, edit):
    merge_id, archive_id, _ = merged_players(before_013)
    assert migrate.apply(before_013.pool) == [MIGRATION.name]
    with before_013.pool.connection() as connection:
        if edit == "payment":
            connection.execute(
                "UPDATE event_archive SET payment_amount=99 WHERE id=%s", (archive_id,)
            )
        else:
            connection.execute(
                "UPDATE event_archive SET checked_in_at=%s WHERE id=%s", (ARRIVAL, archive_id)
            )
    for heal in (False, True):
        if heal:
            assert postgres.refresh_archive_stats()["stale"] is False
        before = {
            table: table_rows(before_013, table)
            for table in ("players", "event_archive", "merge_log")
        }
        with pytest.raises(ValueError, match="changed after this merge"):
            postgres.undo_merge(merge_id)
        assert {table: table_rows(before_013, table) for table in before} == before


@pytest.mark.parametrize("initial_time", [None, ARRIVAL])
def test_post013_merge_fingerprint_includes_checked_in_at(undo_db, initial_time):
    merge_id, archive_id, _ = merged_players(undo_db, checked_in_at=initial_time)
    changed = ARRIVAL if initial_time is None else None
    with undo_db.pool.connection() as connection:
        connection.execute(
            "UPDATE event_archive SET checked_in_at=%s WHERE id=%s", (changed, archive_id)
        )
    with pytest.raises(ValueError, match="Player archive rows changed"):
        postgres.undo_merge(merge_id)
    assert table_rows(undo_db, "merge_log")[0]["undone"] is False

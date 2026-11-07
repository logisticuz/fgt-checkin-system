"""High-risk archive/merge tests use only synthetic rows in disposable Postgres schemas."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, local

import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared import migrate, postgres_api as postgres

pytestmark = pytest.mark.db


@pytest.fixture
def archive_db(pg_db):
    migrate.apply(pg_db.pool)
    return pg_db


@pytest.fixture
def event(archive_db):
    slug = "test-archive-" + uuid.uuid4().hex
    archive_db.seed_settings(active_event_slug=slug, event_date="2026-11-07")
    return slug


def archive(slug, **overrides):
    parameters = {
        "event_slug": slug,
        "event_date": "2026-11-07",
        "event_display_name": "Test Event",
        "swish_expected_per_game": 25,
        "startgg_snapshot": {},
    }
    parameters.update(overrides)
    return postgres.archive_event(**parameters)


def table_rows(database, table):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table)))
            return cursor.fetchall()


def seed_archive(database, **fields):
    values = {"event_slug": "test-archive", "name": "Test Archived Player", **fields}
    with database.pool.connection() as connection:
        return connection.execute(
            sql.SQL("INSERT INTO event_archive ({}) VALUES ({}) RETURNING id").format(
                sql.SQL(", ").join(map(sql.Identifier, values)),
                sql.SQL(", ").join(sql.Placeholder() for _ in values),
            ),
            list(values.values()),
        ).fetchone()[0]


def test_rearchiving_a_cleared_event_raises_without_changing_history(archive_db, event):
    archive_db.seed_checkin(event_slug=event)
    assert archive(event, clear_active=True)["cleared_active"] == 1
    before = {
        table: table_rows(archive_db, table)
        for table in ("event_archive", "event_stats", "players", "audit_log")
    }
    with pytest.raises(postgres.ArchiveError, match="no active rows"):
        archive(event, clear_active=True)
    assert {table: table_rows(archive_db, table) for table in before} == before


def test_rearchiving_the_same_active_ids_upserts_without_duplicate_rows(archive_db, event):
    row = archive_db.seed_checkin(event_slug=event)
    assert archive(event)["archived"] == 1
    first = table_rows(archive_db, "event_archive")[0]
    postgres.update_checkin(row["record_id"], {"name": "Updated Test Player"})
    assert archive(event)["replaced_rows"] == 1
    rows = table_rows(archive_db, "event_archive")
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["source_record_id"] == row["record_id"]
    assert rows[0]["name"] == "Updated Test Player"


def test_legacy_archive_rows_without_source_ids_are_never_replaced(archive_db, event):
    legacy_id = seed_archive(archive_db, event_slug=event, tag="legacy-test", payment_amount=99)
    legacy = table_rows(archive_db, "event_archive")[0]
    archive_db.seed_checkin(event_slug=event)
    archive(event)
    archive(event)
    rows = table_rows(archive_db, "event_archive")
    assert len(rows) == 2
    assert next(row for row in rows if row["id"] == legacy_id) == legacy
    assert legacy["source_record_id"] is None


def test_archive_failure_rolls_back_players_archive_and_active_changes(
    archive_db, event, monkeypatch
):
    archive_db.seed_checkin(event_slug=event)
    seed_archive(archive_db, event_slug=event, tag="legacy-test")
    before = {
        table: table_rows(archive_db, table)
        for table in ("active_event_data", "event_archive", "players", "event_stats")
    }

    def fail_archive(*args, **kwargs):
        raise RuntimeError("Synthetic failure after copying rows")

    # Factual archive failures remain atomic; T067 separately degrades stats-only failures.
    monkeypatch.setattr(postgres, "_finish_archive_edit", fail_archive)
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        archive(event, clear_active=True)
    assert {table: table_rows(archive_db, table) for table in before} == before


@pytest.mark.parametrize("identity", ["new_tag", "existing_tag", "existing_name"])
def test_checkin_waits_for_archive_and_survives_as_an_active_row(
    archive_db, event, monkeypatch, identity
):
    original = archive_db.seed_checkin(
        event_slug=event, tag="test-archived", name="Test Archived Player"
    )
    payload = {
        "new_tag": {"name": "Test Late Player", "tag": "test-late"},
        "existing_tag": {"name": "Test Repeat Player", "tag": "TEST-ARCHIVED"},
        "existing_name": {"name": "test archived player"},
    }[identity]
    barrier, release = Barrier(2), Event()
    original_match = postgres._match_or_create_player

    def pause_archive(*args, **kwargs):
        barrier.wait(timeout=5)
        assert release.wait(timeout=10)
        return original_match(*args, **kwargs)

    monkeypatch.setattr(postgres, "_match_or_create_player", pause_archive)
    with ThreadPoolExecutor(max_workers=2) as executor:
        archiving = executor.submit(archive, event, clear_active=True)
        try:
            barrier.wait(timeout=5)
            incoming = executor.submit(postgres.begin_checkin, event, payload)
            deadline = time.monotonic() + 5
            with archive_db.pool.connection() as probe:
                while not probe.execute(
                    """SELECT EXISTS (SELECT 1 FROM pg_locks
                       WHERE locktype = 'advisory' AND mode = 'ShareLock' AND NOT granted
                         AND objid = ((hashtext('fgt-event:' || %s)::bigint & 4294967295)::oid))""",
                    (event,),
                ).fetchone()[0]:
                    assert (
                        time.monotonic() < deadline
                    ), "Incoming check-in did not wait on the archive lock"
                    time.sleep(0.01)
            assert not incoming.done()
        finally:
            release.set()
        assert archiving.result(timeout=10)["archived"] == 1
        new_checkin = incoming.result(timeout=10)
    assert [row["record_id"] for row in archive_db.checkins()] == [new_checkin["checkin_id"]]
    assert new_checkin["created"] is True
    assert new_checkin["checkin_id"] != original["record_id"]
    assert [row["source_record_id"] for row in table_rows(archive_db, "event_archive")] == [
        original["record_id"]
    ]


def test_noncooperating_concurrent_insert_is_not_deleted(archive_db, event, monkeypatch):
    original = archive_db.seed_checkin(event_slug=event)
    barrier, release = Barrier(2), Event()
    original_match = postgres._match_or_create_player

    def pause_archive(*args, **kwargs):
        barrier.wait(timeout=5)
        assert release.wait(timeout=10)
        return original_match(*args, **kwargs)

    monkeypatch.setattr(postgres, "_match_or_create_player", pause_archive)
    with ThreadPoolExecutor(max_workers=1) as executor:
        archiving = executor.submit(archive, event, clear_active=True)
        try:
            barrier.wait(timeout=5)
            incoming = archive_db.seed_checkin(event_slug=event, tag="test-direct-late")
        finally:
            release.set()
        assert archiving.result(timeout=10)["archived"] == 1
    assert [row["record_id"] for row in archive_db.checkins()] == [incoming["record_id"]]
    assert [row["source_record_id"] for row in table_rows(archive_db, "event_archive")] == [
        original["record_id"]
    ]


def test_shared_event_lock_precedes_reading_checkin_requirements(archive_db, event):
    with archive_db.pool.connection() as connection:
        connection.execute(
            "UPDATE settings SET require_membership = false, require_startgg = false, require_payment = false"
        )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with archive_db.pool.connection() as updater:
            with updater.transaction():
                updater.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (event,)
                )
                updater.execute("UPDATE settings SET require_payment = true")
                incoming = executor.submit(
                    postgres.begin_checkin,
                    event,
                    {"name": "Test Requirement Player", "tag": "test-requirement-player"},
                )
                deadline = time.monotonic() + 5
                with archive_db.pool.connection() as probe:
                    while not probe.execute(
                        """SELECT EXISTS (SELECT 1 FROM pg_locks
                           WHERE locktype = 'advisory' AND mode = 'ShareLock' AND NOT granted
                             AND objid = ((hashtext('fgt-event:' || %s)::bigint & 4294967295)::oid))""",
                        (event,),
                    ).fetchone()[0]:
                        assert (
                            time.monotonic() < deadline
                        ), "Check-in read requirements before locking"
                        time.sleep(0.01)
                assert not incoming.done()
        result = incoming.result(timeout=10)
    row = archive_db.checkins()[0]
    assert row["record_id"] == result["checkin_id"]
    assert row["status"] == "Pending"


def seed_player(database, tag, **overrides):
    values = {
        "name": "Test Player " + tag,
        "tag": tag,
        "email": tag + "@example.invalid",
        "telephone": "0000000000",
        "games_played": ["Test Game"],
        "game_counts": Jsonb({"Test Game": 1}),
        "total_events": 1,
        "total_paid": "25.25",
        "events_list": Jsonb(["shared-test-event"]),
        "notes": "Synthetic profile notes",
        "member_history": Jsonb({"2026": True}),
        **overrides,
    }
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO players ({}) VALUES ({}) RETURNING *").format(
                    sql.SQL(", ").join(map(sql.Identifier, values)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in values),
                ),
                list(values.values()),
            )
            return cursor.fetchone()


def test_standalone_player_lookup_keeps_tag_priority_and_email_fallback(archive_db):
    tagged = seed_player(archive_db, "test-tag-priority")
    emailed = seed_player(archive_db, "test-email-fallback")
    assert postgres._find_player_uuid(tagged["tag"].upper(), emailed["email"]) == tagged["uuid"]
    assert postgres._find_player_uuid("test-unmatched", emailed["email"].upper()) == emailed["uuid"]
    assert postgres._find_player_uuid(None, "test-missing@example.invalid") is None
    assert postgres._find_player_uuid(None, None) is None


@pytest.mark.parametrize("match_by", ["tag", "email"])
def test_saturated_checkins_keep_player_links_without_nested_pool_checkouts(
    archive_db, event, monkeypatch, match_by
):
    size = archive_db.pool.max_size
    players = [seed_player(archive_db, f"test-pool-player-{index}") for index in range(size)]
    barrier, state, checkouts = Barrier(size), local(), []
    original_lookup = postgres._find_player_uuid

    class CountingPool:
        @contextmanager
        def connection(self):
            checkouts.append(1)
            depth = getattr(state, "depth", 0)
            with archive_db.pool.connection(timeout=0.25 if depth else 5) as connection:
                state.depth = depth + 1
                try:
                    yield connection
                finally:
                    state.depth = depth

    def synchronized_lookup(*args, **kwargs):
        # Every outer check-in owns a connection before any player lookup starts.
        barrier.wait(timeout=5)
        return original_lookup(*args, **kwargs)

    monkeypatch.setattr(postgres, "_get_pool", lambda: CountingPool())
    monkeypatch.setattr(postgres, "_find_player_uuid", synchronized_lookup)
    monkeypatch.setattr(postgres, "CANONICAL_PLAYER_ID_ENABLED", True)

    def checkin(player):
        payload = {"name": player["name"], match_by: player[match_by].upper()}
        return postgres.begin_checkin(event, payload)

    with ThreadPoolExecutor(max_workers=size) as executor:
        results = list(executor.map(checkin, players))
    assert [result["player_uuid"] for result in results] == [player["uuid"] for player in players]
    assert len(checkouts) == size
    stored = {row["record_id"]: row["player_uuid"] for row in archive_db.checkins()}
    assert stored == {result["checkin_id"]: result["player_uuid"] for result in results}


@pytest.fixture
def merge_data(archive_db):
    keep = seed_player(archive_db, "test-keep", is_member=False)
    removed = seed_player(
        archive_db, "test-remove", total_events=2, total_paid="30.50", is_member=True
    )
    keeper_archive = seed_archive(
        archive_db, event_slug="shared-test-event", player_uuid=keep["uuid"], tag=keep["tag"]
    )
    moved_archive = seed_archive(
        archive_db, event_slug="shared-test-event", player_uuid=removed["uuid"], tag=removed["tag"]
    )
    keeper_active = archive_db.seed_checkin(
        event_slug="keep-active", player_uuid=keep["uuid"], tag=removed["tag"]
    )
    moved_active = archive_db.seed_checkin(
        event_slug="remove-active", player_uuid=removed["uuid"], tag="test-different-tag"
    )
    return {
        "keep": keep,
        "remove": removed,
        "keeper_archive": keeper_archive,
        "moved_archive": moved_archive,
        "keeper_active": keeper_active,
        "moved_active": moved_active,
    }


def test_undo_latest_unchanged_merge_restores_exact_rows_profiles_and_totals(
    archive_db, merge_data
):
    postgres.refresh_archive_stats()
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data")
    }
    result = postgres.merge_players(merge_data["keep"]["uuid"], merge_data["remove"]["uuid"])
    log = table_rows(archive_db, "merge_log")[0]
    assert log["moved_archive_ids"] == [merge_data["moved_archive"]]
    assert log["moved_active_ids"] == [merge_data["moved_active"]["record_id"]]
    undone = postgres.undo_merge(result["merge_id"])
    assert undone["archive_reverted"] == undone["active_reverted"] == 1
    assert {table: table_rows(archive_db, table) for table in before} == before
    assert table_rows(archive_db, "merge_log")[0]["undone"] is True
    with pytest.raises(ValueError, match="already been undone"):
        postgres.undo_merge(result["merge_id"])


def test_undo_older_merge_is_refused(archive_db, merge_data):
    older = postgres.merge_players(merge_data["keep"]["uuid"], merge_data["remove"]["uuid"])
    first, second = seed_player(archive_db, "test-third"), seed_player(archive_db, "test-fourth")
    postgres.merge_players(first["uuid"], second["uuid"])
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data", "merge_log")
    }
    with pytest.raises(ValueError, match="newest merge"):
        postgres.undo_merge(older["merge_id"])
    assert {table: table_rows(archive_db, table) for table in before} == before


@pytest.mark.parametrize(
    "changed", ["profile", "archive", "active", "new_archive", "restored_player"]
)
def test_undo_changed_merge_is_refused_without_partial_writes(archive_db, merge_data, changed):
    result = postgres.merge_players(merge_data["keep"]["uuid"], merge_data["remove"]["uuid"])
    with archive_db.pool.connection() as connection:
        if changed == "profile":
            # Detect edits even when a writer fails to bump updated_at.
            connection.execute(
                "UPDATE players SET total_paid = total_paid + 1 WHERE uuid = %s",
                (merge_data["keep"]["uuid"],),
            )
        elif changed == "archive":
            connection.execute(
                "UPDATE event_archive SET payment_amount = 99 WHERE id = %s",
                (merge_data["moved_archive"],),
            )
        elif changed == "active":
            connection.execute(
                "UPDATE active_event_data SET payment_amount = 99 WHERE record_id = %s",
                (merge_data["moved_active"]["record_id"],),
            )
        elif changed == "new_archive":
            seed_archive(archive_db, player_uuid=merge_data["keep"]["uuid"])
        else:
            seed_player(archive_db, "test-recreated", uuid=merge_data["remove"]["uuid"])
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data", "merge_log")
    }
    with pytest.raises(ValueError, match="changed after"):
        postgres.undo_merge(result["merge_id"])
    assert {table: table_rows(archive_db, table) for table in before} == before


def test_undo_legacy_log_without_exact_metadata_is_refused(archive_db, merge_data):
    result = postgres.merge_players(merge_data["keep"]["uuid"], merge_data["remove"]["uuid"])
    with archive_db.pool.connection() as connection:
        connection.execute(
            "UPDATE merge_log SET before_totals = NULL, moved_archive_ids = NULL, moved_active_ids = NULL WHERE id = %s",
            (result["merge_id"],),
        )
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data")
    }
    with pytest.raises(ValueError, match="metadata is unavailable"):
        postgres.undo_merge(result["merge_id"])
    assert {table: table_rows(archive_db, table) for table in before} == before

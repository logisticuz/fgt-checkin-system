"""Archive, restore and row writes serialize through real transaction event locks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, current_thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from psycopg.errors import LockNotAvailable

from shared import postgres_api as postgres
from tests.test_safe_archiving import archive, archive_db, event, merge_data, table_rows

pytestmark = pytest.mark.db


def _wait_for_lock(database, key, future, mode):
    deadline = monotonic() + 5
    with database.pool.connection() as probe:
        while monotonic() < deadline:
            if future.done():
                future.result()
                pytest.fail("Writer completed without waiting for the event lock")
            row = probe.execute(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                "AND mode = %s AND NOT granted "
                "AND objid = ((hashtext(%s)::bigint & 4294967295)::oid)",
                (mode, key),
            ).fetchone()
            if row:
                return row[0]
            sleep(0.01)
    pytest.fail("Writer did not wait for the event advisory lock")


def _wait_for_event_lock(database, slug, future, mode):
    return _wait_for_lock(database, "fgt-event:" + slug, future, mode)


def _wait_for_row_lock(database, slug, future):
    deadline = monotonic() + 5
    with database.pool.connection() as probe:
        while monotonic() < deadline:
            if future.done():
                future.result()
                pytest.fail("Writer completed without waiting for the check-in row lock")
            if probe.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks waiting JOIN pg_locks event_lock "
                "ON event_lock.pid = waiting.pid WHERE NOT waiting.granted "
                "AND waiting.locktype IN ('transactionid', 'tuple') "
                "AND event_lock.granted AND event_lock.locktype = 'advisory' "
                "AND event_lock.objid = "
                "((hashtext('fgt-event:' || %s)::bigint & 4294967295)::oid))",
                (slug,),
            ).fetchone()[0]:
                return
            sleep(0.01)
    pytest.fail("Writer did not wait for the check-in row lock")


def _pause_archive(monkeypatch):
    barrier, release = Barrier(2), Event()
    original = postgres._match_or_create_player

    def paused(*args, **kwargs):
        barrier.wait(timeout=5)
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(postgres, "_match_or_create_player", paused)
    return barrier, release


def _observe_commands(database, monkeypatch, *, before=None, after=None):
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def __enter__(self):
            self.cursor.__enter__()
            return self

        def __exit__(self, *args):
            return self.cursor.__exit__(*args)

        def execute(self, query, *args, **kwargs):
            if before is not None:
                before(str(query))
            self.cursor.execute(query, *args, **kwargs)
            if after is not None:
                after(str(query))
            return self

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def cursor(self, *args, **kwargs):
            return Cursor(self.connection.cursor(*args, **kwargs))

    @contextmanager
    def connection():
        with database.pool.connection() as conn:
            yield Connection(conn)

    monkeypatch.setattr(postgres, "_get_pool", lambda: SimpleNamespace(connection=connection))


def test_reopen_waits_for_archive_then_restores_its_snapshot(archive_db, event, monkeypatch):
    original = archive_db.seed_checkin(event_slug=event)
    archive(event)  # Existing history plus an active row reproduces the lost-restore race.
    barrier, release = _pause_archive(monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as workers:
        archiving = workers.submit(archive, event, clear_active=True)
        try:
            barrier.wait(timeout=5)
            reopening = workers.submit(postgres.reopen_event, event)
            _wait_for_event_lock(archive_db, event, reopening, "ExclusiveLock")
        finally:
            release.set()
        assert archiving.result(timeout=10)["cleared_active"] == 1
        result = reopening.result(timeout=10)

    assert result["reopened"] is True
    assert result["restored_rows"] == 1
    rows = archive_db.checkins()
    assert len(rows) == 1
    assert rows[0]["record_id"] != original["record_id"]
    assert rows[0]["event_slug"] == event
    assert table_rows(archive_db, "event_archive")[0]["source_record_id"] == original["record_id"]
    assert postgres.get_active_slug() == event


def test_archive_waits_until_reopen_has_restored_rows(archive_db, event, monkeypatch):
    archive_db.seed_checkin(event_slug=event)
    archive(event, clear_active=True)
    barrier, release = Barrier(2), Event()

    def pause_reopen(query):
        if current_thread().name.startswith("t035-reopen") and (
            "SELECT COUNT(*) FROM active_event_data" in query
        ):
            barrier.wait(timeout=5)
            assert release.wait(timeout=10)

    _observe_commands(archive_db, monkeypatch, after=pause_reopen)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="t035-reopen") as restorer:
        with ThreadPoolExecutor(max_workers=1) as archiver:
            reopening = restorer.submit(postgres.reopen_event, event)
            try:
                barrier.wait(timeout=5)
                archiving = archiver.submit(archive, event, clear_active=True)
                _wait_for_event_lock(archive_db, event, archiving, "ExclusiveLock")
            finally:
                release.set()
            assert reopening.result(timeout=10)["restored_rows"] == 1
            assert archiving.result(timeout=10)["cleared_active"] == 1

    assert archive_db.checkins() == []
    assert len(table_rows(archive_db, "event_archive")) == 2


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_row_writer_waits_for_archive_and_rechecks_the_deleted_row(
    archive_db, event, monkeypatch, operation
):
    original = archive_db.seed_checkin(event_slug=event)
    barrier, release = _pause_archive(monkeypatch)

    with ThreadPoolExecutor(max_workers=2) as workers:
        archiving = workers.submit(archive, event, clear_active=True)
        try:
            barrier.wait(timeout=5)
            if operation == "update":
                writing = workers.submit(
                    postgres.update_checkin, original["record_id"], {"member": True}
                )
            else:
                writing = workers.submit(postgres.delete_checkin, original["record_id"])
            _wait_for_event_lock(archive_db, event, writing, "ShareLock")
        finally:
            release.set()
        assert archiving.result(timeout=10)["archived"] == 1
        assert not writing.result(timeout=10)

    assert archive_db.checkins() == []
    archived = table_rows(archive_db, "event_archive")
    assert len(archived) == 1
    assert archived[0]["source_record_id"] == original["record_id"]
    assert archived[0]["member"] is False


def test_archive_waits_for_update_and_copies_its_committed_value(archive_db, event, monkeypatch):
    original = archive_db.seed_checkin(event_slug=event)
    barrier, release = Barrier(2), Event()
    compute = postgres.compute_checkin_status

    def paused(*args, **kwargs):
        if current_thread().name.startswith("t035-update"):
            barrier.wait(timeout=5)
            assert release.wait(timeout=10)
        return compute(*args, **kwargs)

    monkeypatch.setattr(postgres, "compute_checkin_status", paused)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="t035-update") as updater:
        with ThreadPoolExecutor(max_workers=1) as archiver:
            writing = updater.submit(
                postgres.update_checkin, original["record_id"], {"member": True}
            )
            try:
                barrier.wait(timeout=5)
                archiving = archiver.submit(archive, event, clear_active=True)
                _wait_for_event_lock(archive_db, event, archiving, "ExclusiveLock")
            finally:
                release.set()
            assert writing.result(timeout=10)["fields"]["member"] is True
            assert archiving.result(timeout=10)["cleared_active"] == 1

    assert archive_db.checkins() == []
    assert table_rows(archive_db, "event_archive")[0]["member"] is True


def test_row_move_locks_both_source_and_destination_events(archive_db, event):
    original = archive_db.seed_checkin(event_slug=event)
    destination = event + "-moved"
    with ThreadPoolExecutor(max_workers=1) as workers:
        with archive_db.pool.connection() as blocker, blocker.transaction():
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (destination,)
            )
            writing = workers.submit(
                postgres.update_checkin, original["record_id"], {"event_slug": destination}
            )
            _wait_for_event_lock(archive_db, destination, writing, "ShareLock")
        assert writing.result(timeout=10)["fields"]["event_slug"] == destination
    assert archive_db.checkins()[0]["event_slug"] == destination


def test_missing_row_update_and_delete_keep_their_existing_results(archive_db):
    assert postgres.update_checkin("unknown-synthetic-record", {"member": True}) is None
    assert postgres.delete_checkin("unknown-synthetic-record") is False


@pytest.mark.parametrize("operation", ["merge", "undo"])
@pytest.mark.parametrize("slugs", [("keep-active", "remove-active"), ("z-event", "Ä-event")])
def test_player_merge_locks_sorted_events_before_the_global_lock(
    archive_db, merge_data, operation, slugs
):
    keep, removed = merge_data["keep"]["uuid"], merge_data["remove"]["uuid"]
    with archive_db.pool.connection() as connection:
        for uuid, slug in zip((keep, removed), slugs):
            connection.execute(
                "UPDATE active_event_data SET event_slug = %s WHERE player_uuid = %s", (slug, uuid)
            )
    first_slug, blocked_slug = sorted(slugs)
    merge_id = None
    if operation == "undo":
        merge_id = postgres.merge_players(keep, removed)["merge_id"]

    with ThreadPoolExecutor(max_workers=1) as workers:
        with archive_db.pool.connection() as blocker, blocker.transaction():
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (blocked_slug,)
            )
            if operation == "merge":
                writing = workers.submit(postgres.merge_players, keep, removed)
            else:
                writing = workers.submit(postgres.undo_merge, merge_id)
            pid = _wait_for_event_lock(archive_db, blocked_slug, writing, "ExclusiveLock")
            with archive_db.pool.connection() as probe:
                assert probe.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND granted "
                    "AND locktype = 'advisory' AND mode = 'ExclusiveLock' "
                    "AND objid = ((hashtext('fgt-event:' || %s)::bigint & 4294967295)::oid))",
                    (pid, first_slug),
                ).fetchone()[0]
                assert not probe.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND granted "
                    "AND locktype = 'advisory' "
                    "AND objid = ((hashtext('fgt:player-merges')::bigint & 4294967295)::oid))",
                    (pid,),
                ).fetchone()[0]
        result = writing.result(timeout=10)

    if operation == "merge":
        assert result["active_rows_updated"] == 1
    else:
        assert result["active_reverted"] == 1


def test_merge_covers_a_new_event_committed_while_waiting_for_player_lock(
    archive_db, merge_data, monkeypatch
):
    barrier, release = Barrier(2), Event()
    lookup = postgres._find_player_uuid
    keep, removed = merge_data["keep"], merge_data["remove"]
    new_slug = "late-merge-event"

    def paused(*args, **kwargs):
        barrier.wait(timeout=5)
        assert release.wait(timeout=10)
        return lookup(*args, **kwargs)

    monkeypatch.setattr(postgres, "_find_player_uuid", paused)
    monkeypatch.setattr(postgres, "CANONICAL_PLAYER_ID_ENABLED", True)
    with ThreadPoolExecutor(max_workers=2) as workers:
        incoming = workers.submit(
            postgres.begin_checkin, new_slug, {"name": "Test Late Player", "tag": removed["tag"]}
        )
        try:
            barrier.wait(timeout=5)
            merging = workers.submit(postgres.merge_players, keep["uuid"], removed["uuid"])
            _wait_for_lock(archive_db, "fgt:player-merges", merging, "ExclusiveLock")
        finally:
            release.set()
        new_row = incoming.result(timeout=10)
        assert merging.result(timeout=10)["active_rows_updated"] == 2

    rows = {row["record_id"]: row for row in archive_db.checkins()}
    assert rows[new_row["checkin_id"]]["player_uuid"] == keep["uuid"]
    assert rows[new_row["checkin_id"]]["event_slug"] == new_slug


def test_shared_global_lock_allows_concurrent_checkins(archive_db, event, monkeypatch):
    barrier = Barrier(2)
    lookup = postgres._find_player_uuid

    def simultaneous_lookup(*args, **kwargs):
        # Both writers hold event/global locks before reaching this real lookup.
        barrier.wait(timeout=5)
        return lookup(*args, **kwargs)

    monkeypatch.setattr(postgres, "_find_player_uuid", simultaneous_lookup)
    monkeypatch.setattr(postgres, "CANONICAL_PLAYER_ID_ENABLED", True)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(
            postgres.begin_checkin, event, {"name": "Test One", "tag": "test-one"}
        )
        second = workers.submit(
            postgres.begin_checkin, event, {"name": "Test Two", "tag": "test-two"}
        )
        ids = {first.result(timeout=10)["checkin_id"], second.result(timeout=10)["checkin_id"]}

    assert len(ids) == 2
    assert {row["record_id"] for row in archive_db.checkins()} == ids


@pytest.mark.parametrize("operation", ["merge", "undo"])
def test_stuck_admin_lock_times_out_without_partial_changes(archive_db, merge_data, operation):
    keep, removed = merge_data["keep"]["uuid"], merge_data["remove"]["uuid"]
    merge_id = None
    if operation == "undo":
        merge_id = postgres.merge_players(keep, removed)["merge_id"]
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data", "merge_log")
    }
    with ThreadPoolExecutor(max_workers=1) as workers:
        with archive_db.pool.connection() as blocker, blocker.transaction():
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", ("remove-active",)
            )
            if operation == "merge":
                writing = workers.submit(postgres.merge_players, keep, removed)
            else:
                writing = workers.submit(postgres.undo_merge, merge_id)
            _wait_for_event_lock(archive_db, "remove-active", writing, "ExclusiveLock")
            with pytest.raises(LockNotAvailable, match="lock timeout"):
                writing.result(timeout=13)
    assert {table: table_rows(archive_db, table) for table in before} == before


@pytest.mark.parametrize("operation", ["merge", "undo"])
def test_event_coverage_retry_is_bounded_and_does_not_partially_merge(
    archive_db, merge_data, monkeypatch, operation
):
    keep, removed = merge_data["keep"], merge_data["remove"]
    merge_id = None
    if operation == "undo":
        merge_id = postgres.merge_players(keep["uuid"], removed["uuid"])["merge_id"]
    before = {
        table: table_rows(archive_db, table)
        for table in ("players", "event_archive", "active_event_data", "merge_log")
    }
    arrivals = []

    def incoming_before_global_lock(query):
        if query != "SELECT pg_advisory_xact_lock(hashtext('fgt:player-merges'))":
            return
        index = len(arrivals)
        arrivals.append(
            postgres.begin_checkin(
                f"retry-event-{index}",
                {"name": "Test Arrival", "tag": f"test-arrival-{index}", "email": keep["email"]},
            )["checkin_id"]
        )

    monkeypatch.setattr(postgres, "CANONICAL_PLAYER_ID_ENABLED", True)
    _observe_commands(archive_db, monkeypatch, before=incoming_before_global_lock)
    with pytest.raises(ValueError, match=f"retry the {operation}"):
        if operation == "merge":
            postgres.merge_players(keep["uuid"], removed["uuid"])
        else:
            postgres.undo_merge(merge_id)

    assert len(arrivals) == 3
    assert {
        table: table_rows(archive_db, table) for table in ("players", "event_archive", "merge_log")
    } == {table: before[table] for table in ("players", "event_archive", "merge_log")}
    rows = {row["record_id"]: row for row in archive_db.checkins()}
    assert {key: value for key, value in rows.items() if key not in arrivals} == {
        row["record_id"]: row for row in before["active_event_data"]
    }
    assert all(rows[record_id]["player_uuid"] == keep["uuid"] for record_id in arrivals)


@pytest.mark.parametrize("operation", ["move", "delete"])
def test_repeat_checkin_inserts_in_its_event_after_the_matched_row_moves_or_vanishes(
    archive_db, event, monkeypatch, operation
):
    original = archive_db.seed_checkin(event_slug=event, name="Test Original", tag="test-repeat")
    destination = event + "-other"
    barrier, release = Barrier(2), Event()
    compute = postgres.compute_checkin_status

    def pause_move(*args, **kwargs):
        if current_thread().name.startswith("t035-row-writer"):
            barrier.wait(timeout=5)
            assert release.wait(timeout=10)
        return compute(*args, **kwargs)

    def pause_delete(query):
        if current_thread().name.startswith("t035-row-writer") and query.startswith(
            "DELETE FROM active_event_data"
        ):
            barrier.wait(timeout=5)
            assert release.wait(timeout=10)

    if operation == "move":
        monkeypatch.setattr(postgres, "compute_checkin_status", pause_move)
    else:
        _observe_commands(archive_db, monkeypatch, after=pause_delete)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="t035-row-writer") as writer:
        with ThreadPoolExecutor(max_workers=1) as checkins:
            if operation == "move":
                writing = writer.submit(
                    postgres.update_checkin, original["record_id"], {"event_slug": destination}
                )
            else:
                writing = writer.submit(postgres.delete_checkin, original["record_id"])
            try:
                barrier.wait(timeout=5)
                incoming = checkins.submit(
                    postgres.begin_checkin, event, {"name": "Test Repeat", "tag": "test-repeat"}
                )
                _wait_for_row_lock(archive_db, event, incoming)
            finally:
                release.set()
            assert writing.result(timeout=10)
            result = incoming.result(timeout=10)

    assert result["created"] is True
    assert result["event_slug"] == event
    assert result["checkin_id"] != original["record_id"]
    rows = {row["record_id"]: row for row in archive_db.checkins()}
    assert rows[result["checkin_id"]]["event_slug"] == event
    assert rows[result["checkin_id"]]["name"] == "Test Repeat"
    if operation == "move":
        assert rows[original["record_id"]]["event_slug"] == destination
        assert rows[original["record_id"]]["name"] == "Test Original"
    else:
        assert original["record_id"] not in rows


def test_repeat_lookup_holds_the_event_qualified_row_lock_until_commit(
    archive_db, event, monkeypatch
):
    original = archive_db.seed_checkin(event_slug=event, name="Test Original", tag="test-repeat")
    destination = event + "-other"
    barrier, release = Barrier(2), Event()

    def pause_lookup(query):
        if current_thread().name.startswith("t035-repeat") and query.startswith(
            "SELECT record_id FROM active_event_data WHERE LOWER("
        ):
            barrier.wait(timeout=5)
            assert release.wait(timeout=10)

    _observe_commands(archive_db, monkeypatch, after=pause_lookup)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="t035-repeat") as checkins:
        with ThreadPoolExecutor(max_workers=1) as writer:
            incoming = checkins.submit(
                postgres.begin_checkin, event, {"name": "Test Repeat", "tag": "test-repeat"}
            )
            try:
                barrier.wait(timeout=5)
                moving = writer.submit(
                    postgres.update_checkin, original["record_id"], {"event_slug": destination}
                )
                _wait_for_row_lock(archive_db, event, moving)
            finally:
                release.set()
            result = incoming.result(timeout=10)
            assert moving.result(timeout=10)

    assert result["created"] is False
    assert result["checkin_id"] == original["record_id"]
    assert result["event_slug"] == event
    assert archive_db.checkins()[0]["event_slug"] == destination
    assert archive_db.checkins()[0]["name"] == "Test Repeat"

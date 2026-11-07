"""Dashboard refreshes authoritative rows and recomputes active statuses coherently."""

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, current_thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared import postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard, make_row

ROOT = Path(__file__).resolve().parents[1]


def observe_batch_connection(pg_db, monkeypatch):
    ready, process_ids = Event(), []

    @contextmanager
    def connection():
        with pg_db.pool.connection() as conn:
            if current_thread().name.startswith("t027-recompute"):
                process_ids.append(conn.info.backend_pid)
                ready.set()
            yield conn

    monkeypatch.setattr(
        postgres, "_get_pool", lambda: SimpleNamespace(connection=connection)
    )
    return ready, process_ids


def batch_waits_on_advisory_lock(pg_db, ready, process_ids, future):
    assert ready.wait(3), "Batch did not acquire a test connection"
    deadline = monotonic() + 3
    with pg_db.pool.connection() as monitor:
        while monotonic() < deadline:
            if future.done():
                future.result()  # Surface worker failures before classifying completion.
                return False
            blocked = monitor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                "AND locktype = 'advisory' AND NOT granted)",
                (process_ids[0],),
            ).fetchone()[0]
            if blocked:
                return True
            sleep(0.01)
    pytest.fail("Batch neither completed nor waited on its event lock")


@pytest.mark.parametrize("column", ["member", "startgg", "payment_valid"])
def test_toggle_uses_returned_status_and_hidden_server_fields(dashboard, column):
    row = {
        "id": "record-0",
        "record_id": "record-0",
        "name": "Test Player",
        column: "✗",
    }
    returned = {
        "status": "DB-derived sentinel",
        "member": True,
        "startgg": False,
        "is_guest": True,
        "payment_valid": True,
        "payment_amount": 91,
        "payment_expected": 92,
    }
    dashboard.storage.update_checkin.return_value = {
        "record_id": "record-0",
        "fields": returned,
    }
    _, rows, active = dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": "record-0", "column_id": column}, [row], "test-event", {}
    )
    assert rows[0]["status"] == "DB-derived sentinel"
    assert rows[0]["payment_amount"] == 91 and rows[0]["payment_expected"] == 92
    assert rows[0]["startgg"] == "✗" and rows[0]["is_guest"] == "✓"
    assert active is None
    assert "status" not in dashboard.storage.update_checkin.call_args.args[1]


def test_payment_toggle_counts_stored_games_when_games_column_is_hidden(
    dashboard, monkeypatch
):
    stored = {
        "payment_valid": False,
        "tournament_games_registered": ["Game A", "Game B", "Game C"],
    }
    lookup = Mock(return_value={"record_id": "record-0", "fields": stored})
    monkeypatch.setattr(
        dashboard.storage, "get_checkin_by_record_id", lookup, raising=False
    )
    dashboard.storage.get_active_settings.return_value = {"swish_expected_per_game": 25}
    dashboard.storage.update_checkin.return_value = {
        "record_id": "record-0",
        "fields": {
            **stored,
            "payment_valid": True,
            "payment_amount": 75,
            "payment_expected": 75,
            "status": "Ready",
        },
    }
    _, rows, _ = dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": "record-0", "column_id": "payment_valid"},
        [{"id": "record-0", "record_id": "record-0", "payment_valid": "✗"}],
        "test-event",
        {},
    )
    payload = dashboard.storage.update_checkin.call_args.args[1]
    assert payload == {
        "payment_valid": True,
        "payment_expected": 75,
        "payment_amount": 75,
    }
    assert rows[0]["payment_amount"] == 75
    lookup.assert_called_once_with("record-0")


@pytest.mark.parametrize("column", ["name", "tag", "telephone"])
def test_inline_edit_refreshes_status_payments_and_flags_from_returned_row(
    dashboard, column
):
    previous = {**make_row(0), "telephone": "0000"}
    current = {**previous, column: "updated"}
    dashboard.storage.update_checkin.return_value = {
        "record_id": "record-0",
        "fields": {
            column: "stored-value",
            "status": "DB-edit sentinel",
            "payment_amount": 12,
            "payment_expected": 15,
            "member": True,
            "payment_valid": True,
        },
    }
    _, rows = dashboard.callbacks["save_tag_edits"](
        1,
        [current],
        [previous],
        {"row_id": "record-0", "column_id": column},
        "test-event",
        {},
    )
    assert rows[0][column] == "stored-value"
    assert rows[0]["status"] == "DB-edit sentinel"
    assert rows[0]["payment_amount"] == 12 and rows[0]["payment_expected"] == 15
    assert rows[0]["member"] == rows[0]["payment_valid"] == "✓"


@pytest.mark.db
def test_requirement_change_recomputes_all_event_rows_and_broadcasts_once(
    dashboard, pg_db, monkeypatch
):
    pg_db.seed_settings(
        require_payment=False, require_membership=False, require_startgg=False
    )
    first = pg_db.seed_checkin(payment_valid=True, status="Ready")
    second = pg_db.seed_checkin(payment_valid=False, status="Ready")
    third = pg_db.seed_checkin(payment_valid=False, status="Ready")
    outside = pg_db.seed_checkin(
        event_slug="other-event", payment_valid=False, status="Ready"
    )
    callback = dashboard.callbacks["save_requirements"]
    monkeypatch.setitem(
        callback.__globals__,
        "get_active_settings_with_id",
        postgres.get_active_settings_with_id,
    )
    monkeypatch.setitem(
        callback.__globals__, "update_settings", postgres.update_settings
    )
    monkeypatch.setitem(
        callback.__globals__, "get_active_slug", postgres.get_active_slug
    )
    monkeypatch.setattr(
        dashboard.storage,
        "recompute_active_statuses",
        postgres.recompute_active_statuses,
        raising=False,
    )
    _, state = callback(1, [True], [], [], [], [], {}, {})
    statuses = {row["record_id"]: row["status"] for row in pg_db.checkins()}
    assert statuses == {
        first["record_id"]: "Ready",
        second["record_id"]: "Pending",
        third["record_id"]: "Pending",
        outside["record_id"]: "Ready",
    }
    assert state["require_payment"] is True
    dashboard.post.assert_called_once_with(
        "http://backend:8000/api/notify/update",
        json={
            "type": "requirements_updated",
            "event_slug": "test-event",
            "updated_rows": 3,
        },
        timeout=2,
    )


@pytest.mark.db
def test_active_status_batch_rolls_back_if_a_row_update_fails(pg_db, monkeypatch):
    pg_db.seed_settings(
        require_payment=True, require_membership=False, require_startgg=False
    )
    pg_db.seed_checkin(payment_valid=False, status="Ready")
    pg_db.seed_checkin(payment_valid=False, status="Ready")
    original = postgres._update_checkin_with_status
    calls = []

    def fail_second(connection, record_id, fields):
        calls.append(record_id)
        if len(calls) == 2:
            raise RuntimeError("Synthetic second-row failure")
        return original(connection, record_id, fields)

    monkeypatch.setattr(postgres, "_update_checkin_with_status", fail_second)
    with pytest.raises(RuntimeError, match="Synthetic second-row failure"):
        postgres.recompute_active_statuses("test-event")
    assert [row["status"] for row in pg_db.checkins()] == ["Ready", "Ready"]


@pytest.mark.db
def test_status_batch_waits_for_shared_event_lock_before_reading_requirements(
    pg_db, monkeypatch
):
    slug = "test-event-" + pg_db.schema
    settings = pg_db.seed_settings(
        active_event_slug=slug,
        require_payment=False,
        require_membership=False,
        require_startgg=False,
    )
    pg_db.seed_checkin(event_slug=slug, payment_valid=False, status="Ready")
    ready, process_ids = observe_batch_connection(pg_db, monkeypatch)
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="t027-recompute"
    ) as executor:
        with pg_db.pool.connection() as holder, holder.transaction():
            holder.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))",
                (slug,),
            )
            future = executor.submit(postgres.recompute_active_statuses, slug)
            assert batch_waits_on_advisory_lock(pg_db, ready, process_ids, future)
            # This would time out if the worker locked settings before its event lock.
            with pg_db.pool.connection() as writer, writer.transaction():
                writer.execute("SET LOCAL lock_timeout = '2s'")
                writer.execute(
                    "UPDATE settings SET require_payment = true WHERE id = %s",
                    (settings["id"],),
                )
        assert future.result(timeout=5) == 1
    assert pg_db.checkins()[0]["status"] == "Pending"


@pytest.mark.db
def test_requirement_batch_includes_checkin_paused_after_old_status_calculation(
    pg_db, monkeypatch
):
    slug = "test-event-" + pg_db.schema
    settings = pg_db.seed_settings(
        active_event_slug=slug,
        require_payment=False,
        require_membership=False,
        require_startgg=False,
    )
    pg_db.seed_checkin(event_slug=slug, payment_valid=False, status="Ready")
    calculated, allow_insert = Event(), Event()
    original = postgres.compute_checkin_status

    paused_once = []

    def pause_new_checkin(fields, requirements):
        result = original(fields, requirements)
        # Pause only begin_checkin's own first calculation; the batch later recomputes the
        # same row with the new requirements (Pending), which is the behaviour under test.
        if fields.get("tag") == "Concurrent synthetic player" and not paused_once:
            paused_once.append(True)
            assert result["status"] == "Ready"
            calculated.set()
            assert allow_insert.wait(10), "Test did not release the paused check-in"
        return result

    monkeypatch.setattr(postgres, "compute_checkin_status", pause_new_checkin)
    ready, process_ids = observe_batch_connection(pg_db, monkeypatch)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="t027-create") as creator:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="t027-recompute"
        ) as batcher:
            checkin = creator.submit(
                postgres.begin_checkin,
                slug,
                {"tag": "Concurrent synthetic player", "name": "Synthetic player"},
            )
            try:
                assert calculated.wait(
                    3
                ), "New check-in never reached its old-rule status"
                postgres.update_settings(str(settings["id"]), {"require_payment": True})
                batch = batcher.submit(postgres.recompute_active_statuses, slug)
                # Without T-026 this completes first; with the shared lock it waits.
                batch_waits_on_advisory_lock(pg_db, ready, process_ids, batch)
            finally:
                allow_insert.set()
            created = checkin.result(timeout=5)
            updated_rows = batch.result(timeout=5)
    rows = {row["record_id"]: row for row in pg_db.checkins()}
    stored = rows[created["record_id"]]
    canonical = original(stored, postgres.get_active_settings())["status"]
    assert stored["status"] == canonical == "Pending"
    assert updated_rows == 2


def test_failed_requirement_refresh_is_audited_and_does_not_broadcast_success(
    dashboard, monkeypatch
):
    dashboard.storage.get_active_settings_with_id.return_value = {
        "record_id": "settings-1"
    }
    dashboard.storage.get_active_slug.return_value = "test-event"
    monkeypatch.setattr(
        dashboard.storage,
        "recompute_active_statuses",
        Mock(side_effect=RuntimeError("synthetic")),
        raising=False,
    )
    feedback, _ = dashboard.callbacks["save_requirements"](
        1, [True], [], [], [], [], {}, {}
    )
    assert "could not be refreshed" in feedback.children
    dashboard.storage.log_action.assert_called_once()
    dashboard.post.assert_not_called()


def test_sse_bursts_coalesce_and_editing_defers_without_losing_refresh():
    path = ROOT / "fgt_dashboard" / "assets" / "sse-client.js"
    source = path.read_text(encoding="utf-8")
    assert re.search(r"refreshDebounceMs\s*=\s*1000", source)
    assert "document.activeElement" in source and "isTableEditing()" in source
    node = shutil.which("node")
    if not node:
        return  # The brief explicitly permits a node-free structural check.
    program = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace(/\}\)\(\);\s*$/, 'globalThis.testRefresh = triggerRefresh;\n})();');
let now = 0, serial = 0, clicks = 0, active = null, editingClass = false;
const timers = new Map();
const input = { matches: () => true };
const checkbox = { matches: () => false };
const table = { contains: item => item === input || item === checkbox,
                querySelector: () => editingClass ? {} : null };
const context = {
  console: { log() {}, error() {} }, window: {},
  document: { readyState: 'loading', addEventListener() {},
    get activeElement() { return active; },
    getElementById(id) { return id === 'checkins-table' ? table : id === 'btn-refresh' ? { click() { clicks++; } } : null; } },
  setTimeout(fn, delay) { const id = ++serial; timers.set(id, { fn, due: now + delay }); return id; },
  clearTimeout(id) { timers.delete(id); }
};
vm.runInNewContext(source, context);
function advance(ms) {
  const until = now + ms;
  while (true) {
    const entries = [...timers].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due);
    if (!entries.length) break;
    const [id, timer] = entries[0]; timers.delete(id); now = timer.due; timer.fn();
  }
  now = until;
}
context.testRefresh(); advance(500); context.testRefresh(); advance(999);
assert.equal(clicks, 0); advance(1); assert.equal(clicks, 1);
active = input; context.testRefresh(); advance(3000); assert.equal(clicks, 1);
active = null; advance(1000); assert.equal(clicks, 2);
editingClass = true; context.testRefresh(); advance(1000); assert.equal(clicks, 2);
editingClass = false; advance(1000); assert.equal(clicks, 3);
active = checkbox; context.testRefresh(); advance(1000); assert.equal(clicks, 4);
"""
    result = subprocess.run(
        [node, "-e", program, str(path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr

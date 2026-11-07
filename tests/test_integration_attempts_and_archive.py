"""Real private-PG callback ordering and membership/archive regressions."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from shared import membership_store as store, postgres_api as postgres
from tests.fixtures import make_checkin
from tests.test_membership_endpoints import (
    api,
    isolated_membership,
    member_client,
    membership_db,
    provider,
    row,
    sender,
    start_attempt,
    wait_until,
)

pytestmark = pytest.mark.db


def submitted_attempt(database, checkin):
    attempt_id = store.create_attempt(
        pnr="000000000000",
        contact="synthetic@example.invalid",
        channel="email",
        event_slug=checkin["event_slug"],
        checkin_id=checkin["record_id"],
        code="123456",
        bylaws_version="test",
        privacy_version="test",
        email="synthetic@example.invalid",
        phone="+46000000000",
        year=2026,
    )
    assert store.begin_submit(attempt_id, "123456").status == "submitting"
    assert store.finish_submit(attempt_id, "submitted")
    return attempt_id


def archive(database, slug):
    return postgres.archive_event(
        event_slug=slug,
        event_date="2026-11-07",
        clear_active=True,
        swish_expected_per_game=0,
        startgg_snapshot={},
    )


@pytest.mark.parametrize("source", ["ebas", "startgg"])
def test_stale_attempt_is_rejected_without_any_checkin_field_change(pg_db, source):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=True, startgg=True, tournament_games_registered=["Current"])
    old = postgres.start_integration_attempt(checkin["record_id"])
    current = postgres.start_integration_attempt(checkin["record_id"])
    assert old != current
    before = pg_db.checkins()
    result = postgres.apply_integration_result(
        checkin["record_id"],
        source,
        True,
        {"member": False, "registered": False, "events": ["Old"]},
        attempt_id=old,
    )
    assert result["state"] == "stale_rejected" and result["updated"] is False
    assert pg_db.checkins() == before
    with pg_db.pool.connection() as connection:
        action, details = connection.execute("SELECT action, details FROM audit_log").fetchone()
    assert action == "stale_rejected" and json.loads(details)["source"] == source


def test_missing_id_is_legacy_only_until_the_first_tracked_run(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    assert postgres.apply_integration_result(checkin["record_id"], "ebas", True, {"member": True})[
        "updated"
    ]
    postgres.start_integration_attempt(checkin["record_id"])
    before = pg_db.checkins()
    result = postgres.apply_integration_result(
        checkin["record_id"], "ebas", True, {"member": False}
    )
    assert result["state"] == "stale_rejected" and pg_db.checkins() == before


def test_starting_a_new_run_serializes_with_the_locked_callback(pg_db, monkeypatch):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    old = postgres.start_integration_attempt(checkin["record_id"])
    locked, entering, release = Event(), Event(), Event()
    original, pids = postgres._lock_integration_checkin, []

    def observed_lock(connection, checkin_id):
        pids.append(connection.info.backend_pid)
        first = len(pids) == 1
        if not first:
            entering.set()
        current = original(connection, checkin_id)
        if first:
            locked.set()
            assert release.wait(timeout=10)
        return current

    monkeypatch.setattr(postgres, "_lock_integration_checkin", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        callback = executor.submit(
            postgres.apply_integration_result,
            checkin["record_id"],
            "startgg",
            True,
            {"registered": True},
            attempt_id=old,
        )
        try:
            assert locked.wait(timeout=5)
            restart = executor.submit(postgres.start_integration_attempt, checkin["record_id"])
            assert entering.wait(timeout=5)
            deadline = time.monotonic() + 5
            with pg_db.pool.connection() as probe:
                while not probe.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                    (pids[1],),
                ).fetchone()[0]:
                    assert (
                        time.monotonic() < deadline
                    ), "New attempt did not wait for the callback row lock"
                    time.sleep(0.01)
            assert not restart.done()
        finally:
            release.set()
        assert callback.result(timeout=10)["updated"] is True
        current = restart.result(timeout=10)
    before = pg_db.checkins()
    assert current != old
    assert (
        postgres.apply_integration_result(
            checkin["record_id"], "startgg", True, {"registered": False}, attempt_id=old
        )["state"]
        == "stale_rejected"
    )
    assert pg_db.checkins() == before


@pytest.mark.parametrize("timestamp_kind", ["older", "same", "invalid", "missing"])
def test_same_attempt_rejects_replayed_or_out_of_order_results(pg_db, timestamp_kind):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    attempt_id = postgres.start_integration_attempt(checkin["record_id"])
    with pg_db.pool.connection() as connection:
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    accepted = now + timedelta(seconds=2)
    postgres.apply_integration_result(
        checkin["record_id"],
        "startgg",
        True,
        {"registered": True, "events": ["Current"]},
        attempt_id=attempt_id,
        fetched_at=accepted.isoformat(),
    )
    before = pg_db.checkins()
    fetched_at = {
        "older": now.isoformat(),
        "same": accepted.isoformat(),
        "invalid": "000000000000",
        "missing": None,
    }[timestamp_kind]
    result = postgres.apply_integration_result(
        checkin["record_id"],
        "startgg",
        True,
        {"registered": False, "events": ["Old"]},
        attempt_id=attempt_id,
        fetched_at=fetched_at,
    )
    assert result["state"] == "stale_rejected" and pg_db.checkins() == before


def test_endpoint_passes_the_current_attempt_id_to_storage(app_client, pg_db):
    checkin = pg_db.seed_checkin()
    attempt_id = postgres.start_integration_attempt(checkin["record_id"])
    response = app_client.post(
        "/api/integration/result",
        json={
            "checkin_id": checkin["record_id"],
            "source": "startgg",
            "attempt_id": attempt_id,
            "ok": True,
            "data": {"registered": True, "events": ["Verified"]},
        },
    )
    assert response.status_code == 200 and response.json()["updated"] is True
    assert pg_db.checkins()[0]["tournament_games_registered"] == ["Verified"]


@pytest.mark.parametrize("engine", ["none", "n8n"])
def test_orchestrate_records_and_sends_ids_only_for_an_enabled_run(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, engine
):
    monkeypatch.setattr(backend_app.checkin, "INTEGRATION_ENGINE", engine)
    response = app_client.post("/api/checkin/orchestrate", json=make_checkin())
    assert response.status_code == 200
    stored = pg_db.checkins()[0]
    if engine == "none":
        assert stored["last_ebas_attempt_id"] is stored["last_startgg_attempt_id"] is None
    else:
        attempt_id = n8n_stub.calls[-1]["json"]["attempt_id"]
        assert stored["last_ebas_attempt_id"] == stored["last_startgg_attempt_id"] == attempt_id


def test_late_verified_false_cannot_clear_v2_membership(pg_db):
    pg_db.seed_settings(require_startgg=False, require_payment=False)
    checkin = pg_db.seed_checkin()
    integration = postgres.start_integration_attempt(checkin["record_id"])
    attempt = submitted_attempt(pg_db, checkin)
    assert postgres.mark_membership_checkin(attempt) == "submitted"
    result = postgres.apply_integration_result(
        checkin["record_id"], "ebas", True, {"member": False}, attempt_id=integration
    )
    stored = pg_db.checkins()[0]
    assert result["updated"] is False
    assert (stored["member"], stored["member_source"], stored["status"]) == (True, "v2", "Ready")


@pytest.mark.parametrize("provenance", [None, "ebas_check"])
def test_submitted_receipt_protects_membership_before_its_provenance_write(pg_db, provenance):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=True, member_source=provenance)
    submitted_attempt(pg_db, checkin)
    # Models the finish_submit/member-update gap and pre-004 submitted receipts.
    result = postgres.apply_integration_result(
        checkin["record_id"], "ebas", True, {"member": False}
    )
    assert result["updated"] is False and pg_db.checkins()[0]["member"] is True


def test_to_override_survives_automated_true_and_submitted_receipt_poll(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    attempt = submitted_attempt(pg_db, checkin)
    postgres.mark_membership_checkin(attempt)
    postgres.update_checkin(checkin["record_id"], {"member": False})
    postgres.apply_integration_result(checkin["record_id"], "ebas", True, {"member": True})
    postgres.mark_membership_checkin(attempt)
    stored = pg_db.checkins()[0]
    assert stored["member"] is False and stored["member_source"] == "to"


@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
@pytest.mark.parametrize("engine", ["n8n", "none"])
def test_admin_recheck_tracks_only_startgg_and_keeps_disabled_engine_inert(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, route, engine
):
    checkin = pg_db.seed_checkin()
    original = postgres.start_integration_attempt(checkin["record_id"])
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", engine)
    n8n_stub.response = {"ok": True, "isRegistered": True, "events": ["Current recheck"]}
    before = pg_db.checkins()
    response = app_client.post(f"/api/admin/{route}", json={"record_id": checkin["record_id"]})
    if engine == "none":
        assert response.status_code == 503 and n8n_stub.calls == []
        assert pg_db.checkins() == before
        return
    assert response.status_code == 200
    stored = pg_db.checkins()[0]
    current = n8n_stub.calls[-1]["json"]["attempt_id"]
    assert current != original
    assert stored["last_startgg_attempt_id"] == current
    assert stored["last_ebas_attempt_id"] == original
    assert stored["startgg"] is True and stored["tournament_games_registered"] == [
        "Current recheck"
    ]
    stale = postgres.apply_integration_result(
        checkin["record_id"], "startgg", True, {"registered": False}, attempt_id=original
    )
    assert stale["state"] == "stale_rejected"
    assert (
        postgres.apply_integration_result(
            checkin["record_id"], "ebas", True, {"member": True}, attempt_id=original
        )["updated"]
        is True
    )


@pytest.mark.parametrize("provenance", ["v2", "to"])
def test_legacy_member_callback_preserves_authoritative_membership(app_client, pg_db, provenance):
    checkin = pg_db.seed_checkin()
    postgres.update_checkin(checkin["record_id"], {"member": True}, member_source=provenance)
    response = app_client.post(
        f"/api/checkin/{checkin['record_id']}/member-status", json={"member": False}
    )
    assert response.status_code == 200 and response.json()["member"] is True
    assert pg_db.checkins()[0]["member_source"] == provenance


@pytest.mark.parametrize("provenance, member", [("v2", True), ("to", False)])
def test_archive_reopen_roundtrip_preserves_member_provenance(pg_db, provenance, member):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    postgres.update_checkin(checkin["record_id"], {"member": member}, member_source=provenance)
    archive(pg_db, checkin["event_slug"])
    with pg_db.pool.connection() as connection:
        assert (
            connection.execute("SELECT member_source FROM event_archive").fetchone()[0]
            == provenance
        )
    postgres.reopen_event(checkin["event_slug"])
    restored = pg_db.checkins()[0]
    assert (restored["member"], restored["member_source"]) == (member, provenance)


def test_submit_finishing_after_archive_updates_only_its_linked_history(
    member_client, membership_db, provider, sender, monkeypatch
):
    monkeypatch.setattr(api, "SUBMIT_WAIT_SECONDS", 0.01)
    attempt_id, details = start_attempt(member_client, membership_db)
    checkin = membership_db.checkins()[0]
    provider.release.clear()
    try:
        response = member_client.post(
            "/api/membership/verify",
            json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]},
        )
        assert response.status_code == 202 and provider.started.wait(timeout=5)
        archive(membership_db, checkin["event_slug"])
        with membership_db.pool.connection() as connection:
            connection.execute(
                "INSERT INTO event_archive (event_slug, tag, member) VALUES (%s, %s, false)",
                (checkin["event_slug"], checkin["tag"]),
            )
    finally:
        provider.release.set()

    def archived_member():
        with membership_db.pool.connection() as connection:
            return connection.execute(
                "SELECT member FROM event_archive WHERE source_record_id = %s",
                (details["checkin_id"],),
            ).fetchone()[0]

    wait_until(archived_member)
    assert member_client.get(f"/api/membership/{attempt_id}/status").json()["state"] == "submitted"
    assert membership_db.checkins() == []
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT member FROM event_archive WHERE source_record_id IS NULL"
            ).fetchone()[0]
            is False
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_log WHERE action='membership_member_updated' AND target_table='event_archive'"
            ).fetchone()[0]
            == 1
        )


def test_submit_with_deleted_associated_checkin_needs_manual_review(
    member_client, membership_db, provider, sender, monkeypatch
):
    monkeypatch.setattr(api, "SUBMIT_WAIT_SECONDS", 0.01)
    attempt_id, details = start_attempt(member_client, membership_db)
    provider.release.clear()
    try:
        response = member_client.post(
            "/api/membership/verify",
            json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]},
        )
        assert response.status_code == 202 and provider.started.wait(timeout=5)
        assert postgres.delete_checkin(details["checkin_id"])
    finally:
        provider.release.set()
    wait_until(lambda: row(membership_db, attempt_id)["state"] == "manual_review")
    state = member_client.get(f"/api/membership/{attempt_id}/status").json()
    assert state["state"] == "manual_review" and state["error_code"] == "checkin_missing"
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
            ).fetchone()[0]
            == "manual_review"
        )


def test_standalone_membership_still_submits_without_a_checkin(
    member_client, membership_db, sender
):
    attempt_id, _ = start_attempt(member_client, membership_db, checkin_id=None)
    result = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert result.json()["state"] == "submitted"


def test_archive_member_change_rolls_back_if_its_audit_fails(pg_db, monkeypatch):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    attempt = submitted_attempt(pg_db, checkin)
    archive(pg_db, checkin["event_slug"])

    def fail(*args):
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(postgres, "_integration_audit", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        postgres.mark_membership_checkin(attempt)
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT member, member_source FROM event_archive").fetchone() == (
            False,
            None,
        )

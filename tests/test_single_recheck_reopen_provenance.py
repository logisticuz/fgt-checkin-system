"""Real-PG regressions for single recheck, restored IDs, and conservative provenance."""

import uuid
from unittest.mock import AsyncMock

import pytest

from shared import postgres_api as postgres
from tests.test_integration_attempts_and_archive import archive
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
from tests.test_safe_archiving import table_rows

pytestmark = pytest.mark.db


def test_single_recheck_stale_has_no_side_effects(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch
):
    pg_db.seed_settings(swish_expected_per_game=25)
    checkin = pg_db.seed_checkin(member=True)
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    broadcast = AsyncMock()
    monkeypatch.setattr(backend_app.admin.sse_manager, "broadcast", broadcast)
    followup_writes, newest = [], []
    original_update = backend_app.admin.update_checkin

    def record_followup(*args, **kwargs):
        followup_writes.append((args, kwargs))
        return original_update(*args, **kwargs)

    monkeypatch.setattr(backend_app.admin, "update_checkin", record_followup)

    async def newer_result(request):
        attempt = postgres.start_integration_attempt(checkin["record_id"], sources=("startgg",))
        postgres.apply_integration_result(
            checkin["record_id"],
            "startgg",
            True,
            {"registered": True, "events": ["Current A", "Current B"]},
            attempt_id=attempt,
            recompute_payment_expected=True,
            state="verified_true",
        )
        newest.extend(pg_db.checkins())

    n8n_stub.on_call = newer_result
    n8n_stub.response = {"ok": True, "state": "verified_false", "isRegistered": False, "events": []}
    response = app_client.post(
        "/api/admin/recheck-startgg", json={"record_id": checkin["record_id"]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["state"] == "stale_rejected"
    assert pg_db.checkins() == newest
    assert pg_db.checkins()[0]["payment_expected"] == 50
    assert followup_writes == []
    broadcast.assert_not_awaited()


def test_late_membership_after_archive_and_reopen_updates_active_copy(
    member_client, membership_db, provider, sender, monkeypatch
):
    monkeypatch.setattr(api, "SUBMIT_WAIT_SECONDS", 0.01)
    attempt, details = start_attempt(member_client, membership_db)
    checkin = membership_db.checkins()[0]
    provider.release.clear()
    try:
        response = member_client.post(
            "/api/membership/verify", json={"attempt_id": attempt, "code": sender.calls[-1]["code"]}
        )
        assert response.status_code == 202 and provider.started.wait(timeout=5)
        archive(membership_db, checkin["event_slug"])
        assert postgres.reopen_event(checkin["event_slug"])["restored_rows"] == 1
    finally:
        provider.release.set()
    wait_until(lambda: row(membership_db, attempt)["member_link_reconciled_at"] is not None)
    restored = membership_db.checkins()
    assert len(restored) == 1
    assert restored[0]["record_id"] == details["checkin_id"]
    assert (restored[0]["member"], restored[0]["member_source"], restored[0]["status"]) == (
        True,
        "v2",
        "Ready",
    )


@pytest.mark.parametrize("callback", ["integration", "legacy"])
def test_pre004_to_override_survives_provider_negative(app_client, pg_db, callback):
    pg_db.seed_settings(require_payment=False, require_startgg=False)
    checkin = pg_db.seed_checkin(member=True, status="Ready")
    assert checkin.get("member_source") is None
    if callback == "integration":
        attempt = postgres.start_integration_attempt(checkin["record_id"])
        response = app_client.post(
            "/api/integration/result",
            json={
                "checkin_id": checkin["record_id"],
                "source": "ebas",
                "ok": True,
                "state": "verified_false",
                "attempt_id": attempt,
                "data": {"member": False},
            },
        )
    else:
        response = app_client.post(
            f"/api/checkin/{checkin['record_id']}/member-status",
            json={"member": False, "ok": True, "state": "verified_false"},
        )
    assert response.status_code == 200
    stored = pg_db.checkins()[0]
    assert stored["member"] is True and stored["member_source"] is None
    assert stored["status"] == "Ready"
    postgres.update_checkin(checkin["record_id"], {"member": False})
    assert pg_db.checkins()[0]["member"] is False
    assert pg_db.checkins()[0]["member_source"] == "to"


def test_reopen_id_collision_rolls_back_all_restores_and_settings(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(tag="test-collision")
    pg_db.seed_checkin(event_slug=checkin["event_slug"], tag="test-noncollision")
    archive(pg_db, checkin["event_slug"])
    with pg_db.pool.connection() as connection:
        connection.execute(
            "INSERT INTO active_event_data (record_id, event_slug, tag) VALUES (%s, %s, %s)",
            (checkin["record_id"], "test-other-event", "test-other-row"),
        )
        connection.execute("UPDATE settings SET active_event_slug='test-other-event'")
    before = {
        table: table_rows(pg_db, table)
        for table in ("active_event_data", "event_archive", "settings", "audit_log")
    }
    with pytest.raises(ValueError, match="check-in IDs"):
        postgres.reopen_event(checkin["event_slug"])
    assert {table: table_rows(pg_db, table) for table in before} == before


def test_reopen_unlinked_legacy_history_gets_a_new_uuid_without_backfill(pg_db):
    pg_db.seed_settings()
    with pg_db.pool.connection() as connection:
        connection.execute(
            "INSERT INTO event_archive (event_slug, tag, member) VALUES (%s, %s, false)",
            ("test-legacy-history", "test-legacy-tag"),
        )
    assert postgres.reopen_event("test-legacy-history")["restored_rows"] == 1
    restored = pg_db.checkins()[0]
    assert str(uuid.UUID(restored["record_id"])) == restored["record_id"]
    with pg_db.pool.connection() as connection:
        assert (
            connection.execute("SELECT source_record_id FROM event_archive").fetchone()[0] is None
        )

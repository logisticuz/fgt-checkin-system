"""Review regressions: stale bulk follow-ups and durable/fair member-link recovery."""

import json

import pytest

from shared import membership_store as store, postgres_api as postgres
from tests.test_integration_attempts_and_archive import archive
from tests.test_membership_endpoints import (
    PNR,
    api,
    isolated_membership,
    member_client,
    membership_db,
    provider,
    row,
    sender,
)

pytestmark = pytest.mark.db


def completed_attempt(database, checkin, ordinal=1, state="submitted", pnr=None):
    attempt = store.create_attempt(
        pnr=pnr or f"{ordinal:012}",
        contact=f"synthetic-{ordinal}@example.invalid",
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
    assert store.begin_submit(attempt, "123456").status == "submitting"
    store.finish_submit(attempt, state)
    return attempt


def test_bulk_recheck_does_not_price_from_a_rejected_stale_provider_result(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch
):
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    pg_db.seed_settings(swish_expected_per_game=25)
    checkin = pg_db.seed_checkin()

    async def newer_result_during_provider_call(request):
        newer = postgres.start_integration_attempt(checkin["record_id"], sources=("startgg",))
        assert postgres.apply_integration_result(
            checkin["record_id"],
            "startgg",
            True,
            {"registered": True, "events": ["New game A", "New game B"]},
            attempt_id=newer,
            recompute_payment_expected=True,
            state="verified_true",
        )["updated"]

    n8n_stub.on_call = newer_result_during_provider_call
    n8n_stub.response = {"ok": True, "state": "verified_false", "isRegistered": False, "events": []}
    response = app_client.post("/api/admin/bulk-recheck-startgg")
    assert response.status_code == 200 and response.json()["checked"] == 0
    stored = pg_db.checkins()[0]
    assert stored["tournament_games_registered"] == ["New game A", "New game B"]
    assert stored["payment_expected"] == 50
    with pg_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_log WHERE action='stale_rejected'"
            ).fetchone()[0]
            == 1
        )


def test_archived_submitted_link_is_retried_after_an_audit_failure(pg_db, monkeypatch):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    attempt = completed_attempt(pg_db, checkin)
    archive(pg_db, checkin["event_slug"])

    def fail_audit(*args):
        raise RuntimeError("Synthetic member-link audit failure")

    with monkeypatch.context() as failure:
        failure.setattr(postgres, "_integration_audit", fail_audit)
        with pytest.raises(RuntimeError, match="Synthetic member-link audit failure"):
            postgres.mark_membership_checkin(attempt)
    assert row(pg_db, attempt)["member_link_reconciled_at"] is None
    assert attempt in api._pending_reconciles()
    response = api._status(attempt)
    assert json.loads(response.body)["state"] == "submitted"
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT member, member_source FROM event_archive").fetchone() == (
            True,
            "v2",
        )
    assert row(pg_db, attempt)["member_link_reconciled_at"] is not None
    assert attempt not in api._pending_reconciles()


def test_missing_submitted_link_is_recovered_to_manual_review(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin()
    attempt = completed_attempt(pg_db, checkin)
    postgres.delete_checkin(checkin["record_id"])
    assert attempt in api._pending_reconciles()
    assert json.loads(api._status(attempt).body)["error_code"] == "checkin_missing"
    assert attempt not in api._pending_reconciles()


def test_ten_to_overrides_are_acknowledged_and_do_not_starve_uncertain_attempt(pg_db, provider):
    pg_db.seed_settings()
    provider.confirm_found = False
    submitted = []
    for index in range(1, 11):
        checkin = pg_db.seed_checkin(tag=f"test-to-{index}")
        submitted.append(completed_attempt(pg_db, checkin, index))
        postgres.update_checkin(checkin["record_id"], {"member": False})
    uncertain_checkin = pg_db.seed_checkin(tag="test-uncertain")
    uncertain = completed_attempt(pg_db, uncertain_checkin, 11, "outcome_unknown", pnr=PNR)
    for _ in range(2):
        pending = api._pending_reconciles()
        assert uncertain in pending
        for attempt in pending:
            api._status(attempt)
    assert provider.count("confirm_membership") == 1
    assert api._pending_reconciles() == [uncertain]
    assert all(
        row(pg_db, attempt)["member_link_reconciled_at"] is not None for attempt in submitted
    )
    assert all(record["member"] is False for record in pg_db.checkins())


def test_unresolved_submitted_batch_rotates_even_before_a_successful_helper(pg_db):
    pg_db.seed_settings()
    attempts = []
    for index in range(1, 13):
        checkin = pg_db.seed_checkin(tag=f"test-unresolved-{index}")
        attempts.append(completed_attempt(pg_db, checkin, index))
    first = api._pending_reconciles()
    second = api._pending_reconciles()
    assert len(first) == len(second) == 10
    assert set(first) | set(second) == set(attempts)


def test_repeated_verify_reports_manual_review_after_link_disappears(member_client, membership_db):
    checkin = membership_db.seed_checkin()
    attempt = completed_attempt(membership_db, checkin)
    postgres.delete_checkin(checkin["record_id"])
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt, "code": "123456"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["state"] == "manual_review"
    assert response.json()["error_code"] == "checkin_missing"
    assert row(membership_db, attempt)["state"] == "manual_review"

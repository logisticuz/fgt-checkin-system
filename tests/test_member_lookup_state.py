"""Lookup outcomes remain independent of protected membership truth, in private PG only."""

import base64
import importlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import status_api as status

from scripts import migration_preflight as preflight
from shared import membership_store, migrate
from shared import postgres_api as postgres

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "shared" / "migrations" / "005_member_lookup_state.sql"
STATES = ["confirmed_member", "confirmed_nonmember", "unknown", "skipped", None]


@pytest.fixture(autouse=True)
def synthetic_membership_key(monkeypatch):
    monkeypatch.setenv(
        "MEMBERSHIP_ENC_KEY", base64.b64encode(bytes(range(32))).decode()
    )


def apply_check(checkin, *, member=None, ok=True, state=None, error=None, **kwargs):
    return postgres.apply_integration_result(
        checkin["record_id"],
        "ebas",
        ok,
        {"member": member},
        state=state,
        error=error,
        **kwargs,
    )


def v2_attempt(checkin, state):
    attempt_id = membership_store.create_attempt(
        pnr="000000000000",
        contact="synthetic@example.invalid",
        channel="email",
        event_slug=checkin["event_slug"],
        checkin_id=checkin["record_id"],
        code="123456",
        bylaws_version="test-bylaws",
        privacy_version="test-privacy",
        email="synthetic@example.invalid",
        phone="0000000000",
    )
    assert membership_store.begin_submit(attempt_id, "123456").status == "submitting"
    if state != "submitting":
        assert membership_store.finish_submit(attempt_id, state)
    return attempt_id


def attempt_evidence(database, attempt_id):
    with database.pool.connection() as connection:
        return (
            connection.execute(
                "SELECT * FROM membership_attempts WHERE id = %s", (attempt_id,)
            ).fetchone(),
            connection.execute(
                "SELECT * FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
            ).fetchone(),
        )


@pytest.mark.parametrize("previous", [True, False])
@pytest.mark.parametrize("found", [True, False])
def test_confirmed_boolean_changes_only_automated_membership(pg_db, previous, found):
    pg_db.seed_settings(require_startgg=False)
    checkin = pg_db.seed_checkin(member=previous, member_source="ebas_check")
    attempt = postgres.start_integration_attempt(checkin["record_id"], sources=("ebas",))
    result = apply_check(
        checkin,
        member=found,
        attempt_id=attempt,
        state="verified_true" if found else "verified_false",
    )
    stored = pg_db.checkins()[0]
    expected = "confirmed_member" if found else "confirmed_nonmember"
    assert result["updated"] is True
    assert (
        stored["member"],
        stored["member_source"],
        stored["member_lookup_state"],
    ) == (found, "ebas_check", expected)
    assert stored["status"] == ("Ready" if found else "Pending")
    postgres.recompute_active_statuses(checkin["event_slug"])
    recomputed = pg_db.checkins()[0]
    assert (
        recomputed["member"],
        recomputed["member_lookup_state"],
        recomputed["status"],
    ) == (stored["member"], expected, stored["status"])


@pytest.mark.parametrize("previous", [True, False])
@pytest.mark.parametrize(
    "ok,member,state,error",
    [
        (False, None, "unavailable", {"code": "provider_timeout"}),
        (False, False, None, {"code": "provider_unavailable"}),
        (True, False, "unavailable", None),
        (True, False, "unknown", None),
        (True, False, "timeout", None),
        (True, True, "error", None),
        (True, False, "verified_true", None),
        (True, True, "verified_false", None),
        (True, False, "verified_false", {"code": "provider_error"}),
        (True, "false", "verified_false", None),
    ],
)
def test_errors_are_unknown_and_never_change_membership(
    pg_db, previous, ok, member, state, error
):
    pg_db.seed_settings(require_startgg=False)
    checkin = pg_db.seed_checkin(
        member=previous,
        member_source="ebas_check",
        member_lookup_state="confirmed_member" if previous else "confirmed_nonmember",
        status="Ready" if previous else "Pending",
    )
    result = apply_check(checkin, ok=ok, member=member, state=state, error=error)
    stored = pg_db.checkins()[0]
    assert result["updated"] is False
    assert stored["member_lookup_state"] == "unknown"
    assert (stored["member"], stored["member_source"], stored["status"]) == (
        previous,
        checkin["member_source"],
        checkin["status"],
    )
    assert stored["last_ebas_result_at"] is not None


@pytest.mark.parametrize("previous", [True, False])
def test_skipped_lookup_does_not_use_a_boolean_in_the_payload(pg_db, previous):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=previous, member_source="ebas_check")
    result = apply_check(checkin, member=not previous, ok=False, state="skipped")
    stored = pg_db.checkins()[0]
    assert result["updated"] is False
    assert (
        stored["member"],
        stored["member_source"],
        stored["member_lookup_state"],
    ) == (previous, "ebas_check", "skipped")


@pytest.mark.parametrize(
    "member,provenance,found",
    [
        (True, "to", False),
        (False, "to", True),
        (True, None, False),
        (True, "v2", False),
    ],
)
def test_lookup_outcome_cannot_override_protected_truth(pg_db, member, provenance, found):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=member, member_source=provenance)
    result = apply_check(
        checkin, member=found, state="verified_true" if found else "verified_false"
    )
    stored = pg_db.checkins()[0]
    assert result["updated"] is False
    assert (stored["member"], stored["member_source"]) == (member, provenance)
    assert stored["member_lookup_state"] == ("confirmed_member" if found else "confirmed_nonmember")


def test_submitted_receipt_protects_truth_before_provenance_is_written(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=True, member_source="ebas_check")
    attempt_id = v2_attempt(checkin, "submitted")
    before = attempt_evidence(pg_db, attempt_id)
    assert apply_check(checkin, member=False, state="verified_false")["updated"] is False
    stored = pg_db.checkins()[0]
    assert (
        stored["member"],
        stored["member_source"],
        stored["member_lookup_state"],
    ) == (True, "ebas_check", "confirmed_nonmember")
    assert attempt_evidence(pg_db, attempt_id) == before


@pytest.mark.parametrize("state", ["submitting", "outcome_unknown"])
@pytest.mark.parametrize("member", [True, False])
def test_negative_never_closes_an_inflight_v2_attempt(pg_db, state, member):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member=member, member_source="ebas_check")
    attempt_id = v2_attempt(checkin, state)
    before = attempt_evidence(pg_db, attempt_id)
    apply_check(checkin, member=False, state="verified_false")
    assert attempt_evidence(pg_db, attempt_id) == before
    assert membership_store.get_attempt(attempt_id).state == state
    stored = pg_db.checkins()[0]
    assert stored["member"] is False
    assert stored["member_lookup_state"] == "confirmed_nonmember"


@pytest.mark.parametrize(
    "stale", ["older_attempt", "missing_attempt", "older_time", "duplicate"]
)
def test_stale_result_cannot_change_lookup_or_truth(pg_db, stale):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(
        member=True, member_source="ebas_check", member_lookup_state="confirmed_member"
    )
    old = postgres.start_integration_attempt(checkin["record_id"], sources=("ebas",))
    current = postgres.start_integration_attempt(checkin["record_id"], sources=("ebas",))
    arguments = {"attempt_id": current}
    if stale == "older_attempt":
        arguments["attempt_id"] = old
    elif stale == "missing_attempt":
        arguments["attempt_id"] = None
    elif stale == "older_time":
        arguments["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    else:
        apply_check(checkin, member=True, attempt_id=current, state="verified_true")
    before = pg_db.checkins()
    result = apply_check(checkin, member=False, state="verified_false", **arguments)
    assert result["updated"] is False and result["state"] == "stale_rejected"
    assert pg_db.checkins() == before


def test_lookup_metadata_and_membership_roll_back_with_failed_audit(pg_db, monkeypatch):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(
        member=True, member_source="ebas_check", member_lookup_state="confirmed_member"
    )
    before = pg_db.checkins()

    def unavailable_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit unavailable")

    monkeypatch.setattr(postgres, "_integration_audit", unavailable_audit)
    with pytest.raises(RuntimeError, match="synthetic audit unavailable"):
        apply_check(checkin, member=False, state="verified_false")
    assert pg_db.checkins() == before


def test_other_integrations_and_payload_fields_cannot_set_lookup_state(pg_db):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(member_lookup_state="confirmed_nonmember")
    postgres.apply_integration_result(
        checkin["record_id"],
        "startgg",
        True,
        {"registered": True, "member_lookup_state": "confirmed_member", "member": True},
        state="verified_true",
    )
    stored = pg_db.checkins()[0]
    assert stored["member"] is False and stored["member_lookup_state"] == "confirmed_nonmember"
    postgres.apply_integration_result(
        checkin["record_id"],
        "ebas",
        True,
        {"member": False, "member_lookup_state": "confirmed_member"},
        state="verified_false",
    )
    assert pg_db.checkins()[0]["member_lookup_state"] == "confirmed_nonmember"


@pytest.mark.parametrize("lookup", STATES)
def test_read_projections_expose_state_with_null_as_unknown(
    app_client, pg_db, monkeypatch, lookup
):
    checkin = pg_db.seed_checkin(member=True, member_lookup_state=lookup)
    expected = lookup or "unknown"
    listed = postgres.get_checkins(checkin["event_slug"])
    assert listed[0]["member_lookup_state"] == expected and listed[0]["member"] is True
    assert postgres.get_checkins(include_all=True)[0]["member_lookup_state"] == expected
    record = postgres.get_checkin_by_record_id(checkin["record_id"])
    assert record["fields"]["member_lookup_state"] == expected
    monkeypatch.setattr(status, "_TOKEN_KEY", b"synthetic-lookup-status-key")
    token = status.capability_token(checkin["record_id"])
    response = app_client.get(
        f"/api/checkin/{checkin['record_id']}/status", params={"t": token}
    )
    assert response.status_code == 200
    assert response.json()["member_lookup_state"] == expected
    assert response.json()["member"] is True
    denied = app_client.get(f"/api/checkin/{checkin['record_id']}/status")
    assert denied.status_code == 403 and denied.json() == {
        "found": False,
        "ready": False,
    }
    assert (
        pg_db.checkins()[0]["member_lookup_state"] == lookup
    )  # Readers never backfill.


@pytest.mark.parametrize("lookup", STATES)
def test_airtable_stub_passes_the_same_read_only_state(monkeypatch, lookup):
    monkeypatch.setenv("AIRTABLE_API_KEY", "synthetic-airtable-key")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "synthetic-airtable-base")
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    airtable_api = importlib.import_module("shared.airtable_api")

    def records(table, *, filter_formula, fields):
        assert "member_lookup_state" in fields
        return [
            {
                "id": "synthetic",
                "fields": {"member": True, "member_lookup_state": lookup},
            }
        ]

    monkeypatch.setattr(airtable_api, "_list_records", records)
    row = airtable_api.get_checkins("synthetic-event")[0]
    assert row["member_lookup_state"] == (lookup or "unknown") and row["member"] is True


@pytest.mark.parametrize("lookup", STATES)
def test_archive_upsert_and_reopen_preserve_lookup_and_stable_id(pg_db, lookup):
    pg_db.seed_settings()
    checkin = pg_db.seed_checkin(
        member=True, member_source="ebas_check", member_lookup_state=lookup
    )
    arguments = {
        "event_slug": checkin["event_slug"],
        "event_date": "2026-11-07",
        "swish_expected_per_game": 0,
        "startgg_snapshot": {},
    }
    postgres.archive_event(**arguments, clear_active=False)
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE event_archive SET member_lookup_state = %s",
            ("unknown" if lookup == "skipped" else "skipped",),
        )
    postgres.archive_event(**arguments, clear_active=True)
    with pg_db.pool.connection() as connection:
        assert connection.execute(
            "SELECT source_record_id, member, member_source, member_lookup_state FROM event_archive"
        ).fetchall() == [(checkin["record_id"], True, "ebas_check", lookup)]
    assert pg_db.checkins() == []
    assert postgres.reopen_event(checkin["event_slug"])["restored_rows"] == 1
    stored = pg_db.checkins()[0]
    assert (
        stored["record_id"],
        stored["member"],
        stored["member_source"],
        stored["member_lookup_state"],
    ) == (checkin["record_id"], True, "ebas_check", lookup)


def test_migration_is_additive_idempotent_and_does_not_backfill(pg_db):
    source = MIGRATION.read_text(encoding="utf-8")
    migrate.lint(source, MIGRATION.name)
    with pg_db.pool.connection() as connection, connection.transaction():
        # Session-local shadow tables model pre-005 rows without altering fixture/application tables.
        for table in ("active_event_data", "event_archive"):
            connection.execute(
                psycopg.sql.SQL(
                    "CREATE TEMP TABLE {} (member boolean) ON COMMIT DROP"
                ).format(psycopg.sql.Identifier(table))
            )
            connection.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {} (member) VALUES (true), (false)"
                ).format(psycopg.sql.Identifier(table))
            )
        connection.execute(source, prepare=False)
        connection.execute(source, prepare=False)
        for table in ("active_event_data", "event_archive"):
            assert connection.execute(
                psycopg.sql.SQL(
                    "SELECT member, member_lookup_state FROM {} ORDER BY member"
                ).format(psycopg.sql.Identifier(table))
            ).fetchall() == [(False, None), (True, None)]
            with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
                connection.execute(
                    psycopg.sql.SQL(
                        "UPDATE {} SET member_lookup_state = 'verified_false'"
                    ).format(psycopg.sql.Identifier(table))
                )


def test_migration_runner_twice_and_actual_preflight_cli_pass(
    pg_db, monkeypatch, capsys
):
    applied = migrate.apply(pg_db.pool)
    assert MIGRATION.name in applied
    before = migrate.status(pg_db.pool)
    assert migrate.apply(pg_db.pool) == []
    assert migrate.status(pg_db.pool) == before
    with pg_db.pool.connection() as connection:
        monkeypatch.setattr(
            preflight.psycopg,
            "connect",
            lambda *args, **kwargs: nullcontext(connection),
        )
        assert preflight.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True and report["ready_to_deploy"] is True
    assert report["pending_migrations"] == report["required_unapplied"] == []


def test_pending_lookup_migration_blocks_preflight(pg_db):
    migrate.apply(pg_db.pool)
    with pg_db.pool.connection() as connection:
        connection.execute("DELETE FROM fgt_schema_migrations WHERE version = '005'")
        report = preflight.inspect_database(connection)
    assert report["ready_to_deploy"] is False
    assert report["pending_migrations"] == [MIGRATION.name]

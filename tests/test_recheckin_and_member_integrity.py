"""Membership and status integrity against disposable Postgres and real handlers."""

import json
import re

import httpx
import pytest

from shared import postgres_api as postgres
from tests.fixtures import make_checkin

pytestmark = pytest.mark.db


def test_recheckin_updates_only_nonempty_safe_fields(pg_db, monkeypatch):
    pg_db.seed_settings(require_payment=True)
    original = pg_db.seed_checkin(
        member=True,
        startgg=True,
        payment_valid=True,
        payment_amount=50,
        payment_expected=50,
        tournament_games_registered=["Test Game A", "Test Game B"],
        player_uuid="synthetic-player-id",
        checkin_uuid="synthetic-checkin-id",
        external_id="synthetic-external-id",
        is_guest=True,
        added_via="manual_dashboard",
        acquisition_source="friend",
        status="Ready",
    )

    def unexpected_player_lookup(*args):
        pytest.fail("Existing check-ins must keep their canonical player identity")

    monkeypatch.setattr(postgres, "_find_player_uuid", unexpected_player_lookup)
    result = postgres.begin_checkin(
        original["event_slug"],
        {
            "tag": original["tag"].upper(),
            "name": "Updated Test Player",
            "email": "updated@example.invalid",
            "telephone": "0000000001",
            "acquisition_source": "social media",
            "member": False,
            "startgg": False,
            "payment_valid": False,
            "payment_amount": 0,
            "payment_expected": 0,
            "tournament_games_registered": [],
            "is_guest": False,
            "player_uuid": "replacement-player-id",
            "added_via": "api",
            "UUID": "replacement-checkin-id",
            "external_id": "replacement-external-id",
            "status": "Pending",
        },
    )

    rows = pg_db.checkins()
    assert len(rows) == 1
    stored = rows[0]
    assert result["checkin_id"] == original["record_id"]
    assert result["created"] is False
    assert result["player_uuid"] == original["player_uuid"]
    for key in (
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "player_uuid",
        "checkin_uuid",
        "external_id",
        "is_guest",
        "added_via",
        "tag",
        "status",
    ):
        assert stored[key] == original[key], key
    assert stored["name"] == "Updated Test Player"
    assert stored["email"] == "updated@example.invalid"
    assert stored["telephone"] == "0000000001"
    assert stored["acquisition_source"] == "social"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_contact_fields_do_not_erase_existing_values(pg_db, empty):
    pg_db.seed_settings()
    original = pg_db.seed_checkin(acquisition_source="friend")
    result = postgres.begin_checkin(
        original["event_slug"],
        {
            "tag": original["tag"],
            **{key: empty for key in ("name", "email", "telephone", "acquisition_source")},
        },
    )
    assert result["created"] is False
    stored = pg_db.checkins()[0]
    for key in ("name", "email", "telephone", "acquisition_source"):
        assert stored[key] == original[key]


def test_name_only_recheckin_keeps_verified_state(pg_db):
    pg_db.seed_settings()
    original = pg_db.seed_checkin(tag=None, member=True, startgg=True, status="Ready")
    result = postgres.begin_checkin(original["event_slug"], {"name": original["name"]})
    assert result["checkin_id"] == original["record_id"]
    assert pg_db.checkins()[0]["member"] is True
    assert pg_db.checkins()[0]["status"] == "Ready"


@pytest.mark.parametrize(
    "initial, ok, data, expected, updated",
    [
        (True, False, {"member": False}, True, False),
        (False, False, {"member": True}, False, False),
        (True, True, {"member": False}, False, True),
        (False, True, {"member": True}, True, True),
        (False, True, {}, False, False),
        (True, True, {}, True, False),
        (True, True, {"member": None}, True, False),
        (True, True, {"member": "false"}, True, False),
        (False, True, {"member": "true"}, False, False),
        (True, True, {"member": 0}, True, False),
        (False, True, {"member": 1}, False, False),
    ],
)
def test_ebas_requires_success_and_explicit_boolean(pg_db, initial, ok, data, expected, updated):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(
        member_source="ebas_check",
        member=initial,
        startgg=True,
        status="Ready" if initial else "Pending",
    )
    result = postgres.apply_integration_result(
        row["record_id"],
        "ebas",
        ok,
        data,
        state=(
            ("verified_true" if data.get("member") is True else "verified_false")
            if ok is True
            else "unavailable"
        ),
    )
    stored = pg_db.checkins()[0]
    assert stored["member"] is expected
    assert stored["status"] == ("Ready" if expected else "Pending")
    assert result["updated"] is updated


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"member": False, "status": "Ready"}, "Pending"),
        ({"startgg": False, "status": "Ready"}, "Pending"),
        ({"payment_valid": False, "status": "Ready"}, "Pending"),
        ({"payment_amount": 50, "status": "Pending"}, "Ready"),
        ({"payment_expected": 50, "status": "Pending"}, "Ready"),
        ({"tournament_games_registered": ["Test Game"], "status": "Pending"}, "Ready"),
        ({"status": "Pending"}, "Ready"),
    ],
)
def test_updates_always_derive_status_from_stored_fields(pg_db, fields, expected):
    pg_db.seed_settings(require_payment=True)
    row = pg_db.seed_checkin(member=True, startgg=True, payment_valid=True, status="Ready")
    result = postgres.update_checkin(row["record_id"], fields)
    assert result["fields"]["status"] == expected
    assert pg_db.checkins()[0]["status"] == expected


@pytest.mark.parametrize("writer", ["update", "ebas", "startgg", "swish", "stripe", "endpoint"])
def test_field_and_status_are_written_atomically(app_client, pg_db, writer):
    pg_db.seed_settings(require_payment=True)
    missing_field = (
        "startgg"
        if writer == "startgg"
        else ("payment_valid" if writer in {"swish", "stripe"} else "member")
    )
    fields = {"member": True, "startgg": True, "payment_valid": True}
    fields[missing_field] = False
    row = pg_db.seed_checkin(**fields, status="Pending")

    # The DB rejects either half of a split field/status update. This is test-schema-only DDL.
    with pg_db.pool.connection() as connection:
        connection.execute(
            "ALTER TABLE active_event_data ADD CONSTRAINT test_atomic_status "
            "CHECK (status = CASE WHEN member AND startgg AND payment_valid "
            "THEN 'Ready' ELSE 'Pending' END)"
        )
    try:
        if writer == "endpoint":
            response = app_client.post(
                f"/api/checkin/{row['record_id']}/member-status", json={"member": True}
            )
            assert response.status_code == 200
            assert response.json() == {
                "success": True,
                "checkin_id": row["record_id"],
                "member": True,
                "status": "Ready",
            }
        elif writer == "update":
            postgres.update_checkin(row["record_id"], {"member": True, "status": "Pending"})
        else:
            key = "registered" if writer == "startgg" else missing_field
            postgres.apply_integration_result(
                row["record_id"], writer, True, {key: True}, state="verified_true"
            )
        stored = pg_db.checkins()[0]
        assert stored[missing_field] is True
        assert stored["status"] == "Ready"
    finally:
        with pg_db.pool.connection() as connection:
            connection.execute("ALTER TABLE active_event_data DROP CONSTRAINT test_atomic_status")


def test_recheckin_recomputes_status_without_safe_field_changes(pg_db):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(member=True, startgg=True, status="Pending")
    postgres.begin_checkin(row["event_slug"], {"tag": row["tag"], "status": "Pending"})
    assert pg_db.checkins()[0]["status"] == "Ready"


@pytest.mark.parametrize("member", [True, False])
def test_integration_result_handler_recomputes_status(app_client, pg_db, member):
    row = pg_db.seed_checkin(
        member_source="ebas_check",
        member=not member,
        startgg=True,
        status="Pending" if member else "Ready",
    )
    response = app_client.post(
        "/api/integration/result",
        json={
            "checkin_id": row["record_id"],
            "source": "ebas",
            "ok": True,
            "state": "verified_true" if member else "verified_false",
            "data": {"member": member},
        },
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    stored = pg_db.checkins()[0]
    assert stored["member"] is member
    assert stored["status"] == ("Ready" if member else "Pending")


@pytest.mark.parametrize("requirements_on", [False, True])
def test_new_checkin_defaults_and_derived_status(pg_db, requirements_on):
    pg_db.seed_settings(
        require_payment=requirements_on,
        require_membership=requirements_on,
        require_startgg=requirements_on,
    )
    payload = make_checkin(status="incorrect-caller-status")
    result = postgres.begin_checkin(payload["event_slug"], payload)
    stored = pg_db.checkins()[0]
    assert result["created"] is True
    assert stored["status"] == ("Pending" if requirements_on else "Ready")
    assert stored["member"] is False
    assert stored["payment_valid"] is False
    assert stored["payment_amount"] == stored["payment_expected"] == 0
    assert stored["tournament_games_registered"] == []


def test_failed_status_computation_leaves_fields_unchanged(pg_db, monkeypatch):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(startgg=True)

    def fail(*args):
        raise RuntimeError("Synthetic status computation failure")

    monkeypatch.setattr(postgres, "compute_checkin_status", fail)
    with pytest.raises(RuntimeError, match="Synthetic status computation failure"):
        postgres.update_checkin(row["record_id"], {"member": True})
    stored = pg_db.checkins()[0]
    assert stored["member"] is False
    assert stored["status"] == "Pending"


@pytest.mark.parametrize("source", ["ebas", "unrecognized-" + "0" * 12])
def test_integration_audit_never_copies_sensitive_payloads(pg_db, caplog, source):
    pg_db.seed_settings()
    row = pg_db.seed_checkin()
    synthetic_identifier = "0" * 12  # Deliberately invalid synthetic identifier, never a real PNR.
    if source != "ebas":
        before = pg_db.checkins()
        with pytest.raises(ValueError, match="^invalid_source$") as error:
            postgres.apply_integration_result(
                row["record_id"],
                source,
                {"private": synthetic_identifier},
                data={"member": True, "personnummer": synthetic_identifier},
            )
        assert synthetic_identifier not in str(error.value)
        assert synthetic_identifier not in caplog.text
        assert pg_db.checkins() == before
        return
    postgres.apply_integration_result(
        row["record_id"],
        source,
        True,
        data={
            "member": True,
            "personnummer": synthetic_identifier,
            "nested": [synthetic_identifier],
        },
        error={"code": synthetic_identifier, "message": f"personnummer={synthetic_identifier}"},
        fetched_at=synthetic_identifier,
        state="verified_true",
    )
    with pg_db.pool.connection() as connection:
        details = connection.execute("SELECT details FROM audit_log").fetchone()[0]
    assert json.loads(details)["ok"] is (source != "ebas")
    if source == "ebas":
        assert json.loads(details)["state"] == "unavailable"
        assert json.loads(details)["error_code"] == "provider_error"
    assert "personnummer" not in details.lower()
    assert not re.search(r"\d{10,12}", details)
    assert "personnummer" not in caplog.text.lower()
    assert synthetic_identifier not in caplog.text


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize(
    "ok, registered, state",
    [
        (False, False, "unavailable"),
        (False, True, "unavailable"),
        (True, None, None),
        (True, "false", None),
        (True, 0, None),
        (True, 1, None),
        (True, False, "unavailable"),
        (True, True, "skipped"),
    ],
)
def test_unverified_startgg_preserves_all_stored_fields_and_still_audits(
    pg_db, initial, ok, registered, state
):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(
        member=True,
        startgg=initial,
        is_guest=not initial,
        tournament_games_registered=["Original synthetic game"],
        startgg_event_id="original-event",
        email="original@example.invalid",
        status="Ready" if initial else "Pending",
    )
    data = {
        "events": [],
        "startgg_event_id": "replacement-event",
        "email": "replacement@example.invalid",
    }
    if registered is not None:
        data["registered"] = registered
    result = postgres.apply_integration_result(
        row["record_id"], "startgg", ok, data, state=state
    )
    stored = pg_db.checkins()[0]
    for field in (
        "startgg",
        "is_guest",
        "tournament_games_registered",
        "startgg_event_id",
        "email",
        "status",
    ):
        assert stored[field] == row[field], field
    assert result["updated"] is False
    with pg_db.pool.connection() as connection:
        audit = json.loads(
            connection.execute("SELECT details FROM audit_log").fetchone()[0]
        )
    assert audit["source"] == "startgg" and audit["updated_fields"] == []


@pytest.mark.parametrize("registered", [True, False])
def test_verified_startgg_boolean_updates_registration_and_derived_status(
    pg_db, registered
):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(member=True, startgg=not registered)
    result = postgres.apply_integration_result(
        row["record_id"],
        "startgg",
        True,
        {
            "registered": registered,
            "events": ["Updated synthetic game"],
            "startgg_event_id": "updated-event",
            "email": "updated@example.invalid",
        },
        state="verified_true" if registered else "verified_false",
    )
    stored = pg_db.checkins()[0]
    assert result["updated"] is True
    assert stored["startgg"] is registered and stored["is_guest"] is not registered
    assert stored["status"] == ("Ready" if registered else "Pending")
    assert stored["tournament_games_registered"] == ["Updated synthetic game"]
    assert (
        stored["startgg_event_id"] == "updated-event"
        and stored["email"] == "updated@example.invalid"
    )


@pytest.mark.parametrize("state", ["unavailable", "skipped"])
def test_integration_endpoint_forwards_degraded_state_and_preserves_startgg(
    app_client, pg_db, state
):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(member=True, startgg=True, is_guest=False, status="Ready")
    response = app_client.post(
        "/api/integration/result",
        json={
            "checkin_id": row["record_id"],
            "source": "startgg",
            "ok": True,
            "state": state,
            "data": {"registered": False, "events": []},
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "state": "unavailable",
        "error_code": "contradictory_verdict",
    }
    stored = pg_db.checkins()[0]
    assert stored["startgg"] is True and stored["is_guest"] is False
    assert stored["status"] == "Ready"


@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
@pytest.mark.parametrize("legacy", [False, True])
def test_dashboard_recheck_applies_successful_negative_lookup(
    app_client, pg_db, n8n_stub, backend_app, monkeypatch, route, legacy
):
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    row = pg_db.seed_checkin(
        member=True,
        startgg=True,
        is_guest=False,
        status="Ready",
        tournament_games_registered=["Original synthetic game"],
        payment_expected=25,
    )
    n8n_stub.response = {"isRegistered": False, "events": []}
    if not legacy:
        n8n_stub.response.update(ok=True, state="verified_false")
    response = app_client.post(f"/api/admin/{route}", json={"record_id": row["record_id"]})
    if legacy:
        if route == "recheck-startgg":
            assert response.status_code == 502
        else:
            assert response.status_code == 200
            assert response.json()["checked"] == 0 and len(response.json()["errors"]) == 1
        stored = pg_db.checkins()[0]
        assert stored["startgg"] is True and stored["is_guest"] is False
        assert stored["tournament_games_registered"] == ["Original synthetic game"]
        assert stored["status"] == "Ready" and stored["payment_expected"] == 25
        return
    assert response.status_code == 200
    stored = pg_db.checkins()[0]
    assert stored["startgg"] is False and stored["is_guest"] is True
    assert stored["tournament_games_registered"] == []
    assert stored["status"] == "Pending" and stored["payment_expected"] == 0
    if route == "recheck-startgg":
        assert response.json()["startgg"] is False and response.json()["status"] == "Pending"
        assert response.json()["events"] == []
    else:
        assert response.json()["checked"] == 1 and response.json()["errors"] == []
    with pg_db.pool.connection() as connection:
        audit = json.loads(connection.execute("SELECT details FROM audit_log").fetchone()[0])
    assert audit["ok"] is True and "startgg" in audit["updated_fields"]


@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
@pytest.mark.parametrize(
    "kind",
    [
        "timeout",
        "http-error",
        "unavailable",
        "skipped",
        "contradictory",
        "malformed",
        "string-boolean",
        "legacy-error",
    ],
)
def test_dashboard_recheck_preserves_stored_fields_on_degraded_lookup(
    app_client, pg_db, n8n_stub, backend_app, monkeypatch, route, kind
):
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    row = pg_db.seed_checkin(
        member=True,
        startgg=True,
        is_guest=False,
        status="Ready",
        tournament_games_registered=["Original synthetic game"],
        startgg_event_id="original-event",
        payment_expected=25,
        payment_amount=25,
    )
    if kind == "timeout":

        def timeout(request):
            raise httpx.ReadTimeout("Synthetic unavailable provider", request=request)

        n8n_stub.on_call = timeout
    elif kind == "http-error":
        n8n_stub.status_code = 503
    elif kind in {"unavailable", "skipped", "contradictory"}:
        n8n_stub.response = {
            "ok": kind == "contradictory",
            "state": "skipped" if kind == "skipped" else "unavailable",
            "isRegistered": False,
            "events": [],
        }
    elif kind == "string-boolean":
        n8n_stub.response = {"ok": True, "isRegistered": "false", "events": []}
    elif kind == "legacy-error":
        n8n_stub.response = {"error": True, "isRegistered": False, "events": []}
    else:
        n8n_stub.response = {"ok": True, "state": "verified_false"}
    response = app_client.post(
        f"/api/admin/{route}", json={"record_id": row["record_id"]}
    )
    if route == "recheck-startgg":
        assert response.status_code == 502
    else:
        assert response.status_code == 200
        assert response.json()["checked"] == 0 and len(response.json()["errors"]) == 1
    stored = pg_db.checkins()[0]
    for field in (
        "startgg",
        "is_guest",
        "tournament_games_registered",
        "startgg_event_id",
        "email",
        "status",
        "payment_expected",
        "payment_amount",
    ):
        assert stored[field] == row[field], field

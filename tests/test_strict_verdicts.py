"""Provider truth changes only after a typed, consistent verdict; all data is synthetic."""

import json
from copy import deepcopy

import pytest

from shared import postgres_api as postgres
from tests.test_n8n_flows import STARTGG, flow_version, run_node

PRIVATE = "synthetic-private-provider-body"
FLAGS = (
    "member",
    "member_source",
    "startgg",
    "is_guest",
    "tournament_games_registered",
    "status",
    "payment_expected",
    "payment_amount",
    "email",
)


def existing(database):
    database.seed_settings(swish_expected_per_game=25)
    return database.seed_checkin(
        member=True,
        member_source="ebas_check",
        startgg=True,
        is_guest=False,
        tournament_games_registered=["Existing synthetic game"],
        payment_expected=25,
        payment_amount=25,
        status="Ready",
    )


def unchanged(database, before):
    after = database.checkins()[0]
    assert {key: after[key] for key in FLAGS} == {key: before[key] for key in FLAGS}


def audit(database, action):
    with database.pool.connection() as connection:
        details = connection.execute(
            "SELECT details FROM audit_log WHERE action = %s ORDER BY id DESC LIMIT 1", (action,)
        ).fetchone()[0]
    assert PRIVATE not in details
    assert "personnummer" not in details
    return json.loads(details)


def callback(checkin, source, found=False):
    key = "member" if source == "ebas" else "registered"
    return {
        "checkin_id": checkin["record_id"],
        "source": source,
        "ok": True,
        "state": "verified_true" if found else "verified_false",
        "data": {key: found},
    }


@pytest.mark.db
@pytest.mark.parametrize("source", ["ebas", "startgg"])
@pytest.mark.parametrize(
    "kind,code",
    [
        ("string_ok", "invalid_ok"),
        ("string_boolean", "invalid_boolean"),
        ("missing_boolean", "missing_verdict"),
        ("unknown_state", "invalid_state"),
        ("missing_state", "invalid_state"),
        ("contradictory_state", "contradictory_verdict"),
        ("contradictory_boolean", "contradictory_verdict"),
        ("error", "provider_error"),
        ("errors", "provider_error"),
        ("request_result", "provider_error"),
        ("nested_error", "provider_error"),
    ],
)
def test_malformed_callbacks_are_400_preserve_flags_and_audit_safe_reason(
    app_client, pg_db, source, kind, code
):
    row = existing(pg_db)
    payload = callback(row, source)
    payload["attempt_id"] = postgres.start_integration_attempt(row["record_id"], sources=(source,))
    payload["private_marker"] = PRIVATE
    key = "member" if source == "ebas" else "registered"
    if kind == "string_ok":
        payload["ok"] = "false"
    elif kind == "string_boolean":
        payload["data"][key] = "false"
    elif kind == "missing_boolean":
        payload["data"] = {}
    elif kind == "unknown_state":
        payload["state"] = PRIVATE
    elif kind == "missing_state":
        payload.pop("state")
    elif kind == "contradictory_state":
        payload["state"] = "unavailable"
    elif kind == "contradictory_boolean":
        payload["state"] = "verified_true"
    elif kind == "nested_error":
        payload["data"]["nested"] = [{"errors": [PRIVATE]}]
    elif kind == "request_result":
        payload["request_result"] = PRIVATE
    else:
        payload[kind] = {"message": PRIVATE}
    response = app_client.post("/api/integration/result", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == {"state": "unavailable", "error_code": code}
    assert PRIVATE not in response.text
    unchanged(pg_db, row)
    evidence = audit(pg_db, "integration_result")
    assert evidence["state"] == "unavailable" and evidence["error_code"] == code
    assert evidence["ok"] is False and evidence["updated_fields"] == []
    assert evidence["attempt_id"] == payload["attempt_id"]


@pytest.mark.db
@pytest.mark.parametrize("kind", ["missing", "string", "error", "untyped"])
def test_legacy_member_status_uses_the_same_validator(app_client, pg_db, kind):
    row = existing(pg_db)
    payload = {"ok": True, "state": "verified_false", "member": False, "attempt_id": PRIVATE}
    if kind == "missing":
        payload.pop("member")
    elif kind == "string":
        payload["member"] = "false"
    elif kind == "error":
        payload["errors"] = [PRIVATE]
    else:
        payload = {"member": False, "private_marker": PRIVATE}
    response = app_client.post(f"/api/checkin/{row['record_id']}/member-status", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["state"] == "unavailable"
    assert PRIVATE not in response.text
    unchanged(pg_db, row)
    evidence = audit(pg_db, "legacy_member_result")
    assert evidence["state"] == "unavailable" and evidence["updated"] is False
    assert evidence["attempt_id"] is None


@pytest.mark.db
@pytest.mark.parametrize("source", ["ebas", "startgg"])
@pytest.mark.parametrize("state", ["unavailable", "skipped"])
def test_coherent_non_verdict_is_acknowledged_without_mutation(app_client, pg_db, source, state):
    row = existing(pg_db)
    payload = callback(row, source)
    payload.update(ok=False, state=state, error={"code": "provider_timeout", "message": PRIVATE})
    response = app_client.post("/api/integration/result", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is False and response.json()["ok"] is False
    assert response.json()["updated"] is False and response.json()["state"] == state
    unchanged(pg_db, row)
    assert audit(pg_db, "integration_result")["state"] == state


@pytest.mark.db
@pytest.mark.parametrize("source", ["ebas", "startgg"])
@pytest.mark.parametrize("found", [False, True])
def test_consistent_verified_boolean_still_changes_provider_truth(app_client, pg_db, source, found):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(member=not found, member_source="ebas_check", startgg=not found)
    response = app_client.post("/api/integration/result", json=callback(row, source, found))
    assert response.status_code == 200 and response.json()["updated"] is True
    assert pg_db.checkins()[0]["member" if source == "ebas" else "startgg"] is found
    assert audit(pg_db, "integration_result")["error_code"] is None


@pytest.mark.db
@pytest.mark.parametrize("source", ["ebas", "startgg"])
@pytest.mark.parametrize("attack", ["ok", "state", "error"])
def test_direct_storage_call_cannot_bypass_verdict_validation(pg_db, source, attack):
    row = existing(pg_db)
    payload = callback(row, source)
    if attack == "ok":
        payload["ok"] = "false"
    elif attack == "state":
        payload["state"] = "unavailable"
    else:
        payload["error"] = {"message": PRIVATE}
    payload.pop("checkin_id")
    result = postgres.apply_integration_result(row["record_id"], **payload)
    assert result["ok"] is False and result["updated"] is False
    assert result["verdict_valid"] is False and result["state"] == "unavailable"
    unchanged(pg_db, row)
    assert audit(pg_db, "integration_result")["updated_fields"] == []


@pytest.mark.db
def test_direct_legacy_storage_rejects_string_boolean(pg_db):
    row = existing(pg_db)
    result = postgres.apply_legacy_member_status(
        row["record_id"], "false", ok=True, state="verified_false"
    )
    assert result["verdict_valid"] is False and result["updated"] is False
    unchanged(pg_db, row)


@pytest.mark.db
@pytest.mark.parametrize(
    "payload,accepted",
    [
        ({"member": True}, True),
        ({"member": False}, False),
        ({"member": "true"}, False),
        ({"member": 1}, False),
        ({}, False),
        ({"member": True, "extra": PRIVATE}, False),
        ({"member": True, "error": False}, False),
        ({"member": True, "errors": []}, False),
        ({"member": True, "ok": True}, False),
        ({"member": True, "state": "verified_true"}, False),
        ({"member": True, "error": PRIVATE}, False),
    ],
)
def test_legacy_compatibility_shim_accepts_only_exact_boolean_true_body(
    app_client, pg_db, payload, accepted
):
    pg_db.seed_settings(require_startgg=False)
    row = pg_db.seed_checkin(member=False, member_source="ebas_check")
    response = app_client.post(f"/api/checkin/{row['record_id']}/member-status", json=payload)
    assert response.status_code == (200 if accepted else 400)
    assert pg_db.checkins()[0]["member"] is accepted
    if accepted:
        assert response.json() == {
            "success": True,
            "checkin_id": row["record_id"],
            "member": True,
            "status": "Ready",
        }
    assert PRIVATE not in response.text
    evidence = audit(pg_db, "legacy_member_result")
    assert evidence["compatibility"] == ("legacy_member_true" if accepted else None)


@pytest.mark.db
@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
@pytest.mark.parametrize(
    "kind", ["bare_boolean", "errors", "request_result", "contradictory", "redirect"]
)
def test_admin_recheck_rejects_untyped_or_error_shaped_boolean(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, route, kind
):
    row = existing(pg_db)
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    payload = {"isRegistered": False, "events": []}
    if kind != "bare_boolean":
        payload.update(ok=True, state="verified_false")
    if kind == "contradictory":
        payload["state"] = "unavailable"
    elif kind in {"errors", "request_result"}:
        payload[kind] = PRIVATE
    elif kind == "redirect":
        n8n_stub.status_code = 302
    n8n_stub.response = payload
    response = app_client.post(f"/api/admin/{route}", json={"record_id": row["record_id"]})
    if route == "recheck-startgg":
        assert response.status_code == 502
    else:
        assert response.status_code == 200
        assert response.json()["checked"] == 0 and len(response.json()["errors"]) == 1
    assert PRIVATE not in response.text
    unchanged(pg_db, row)


@pytest.mark.db
@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
def test_valid_empty_startgg_participant_list_remains_verified_false(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, route
):
    row = existing(pg_db)
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    parsed = run_node(
        flow_version(STARTGG),
        "Parse Response",
        {"data": {"tournament": {"participants": {"nodes": []}}}},
        {"Parse Input": {"tag": "Synthetic", "attempt_id": "synthetic-attempt"}},
    )
    assert parsed["state"] == "verified_false" and parsed["isRegistered"] is False
    n8n_stub.response = parsed
    response = app_client.post(f"/api/admin/{route}", json={"record_id": row["record_id"]})
    assert response.status_code == 200
    assert response.json().get("checked", 1) == 1
    stored = pg_db.checkins()[0]
    assert stored["startgg"] is False and stored["is_guest"] is True
    assert stored["tournament_games_registered"] == [] and stored["payment_expected"] == 0
    assert audit(pg_db, "integration_result")["state"] == "verified_false"


@pytest.mark.parametrize("source", ["ebas", "startgg"])
@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_verdict_booleans_are_never_coerced(source, value):
    payload = callback({"record_id": "synthetic"}, source)
    payload["data"]["member" if source == "ebas" else "registered"] = deepcopy(value)
    result = postgres.validate_provider_verdict(source, payload)
    assert result["valid"] is False and result["ok"] is False
    assert result["error_code"] == "invalid_boolean"


@pytest.mark.parametrize("events", [None, "game", {}, [None], [1], [""]])
def test_malformed_events_cannot_become_an_authoritative_empty_list(events):
    result = postgres.validate_provider_verdict(
        "startgg", {"ok": True, "state": "verified_true", "isRegistered": True, "events": events}
    )
    assert result["ok"] is False and result["error_code"] == "invalid_events"


@pytest.mark.db
@pytest.mark.parametrize("route", ["recheck-startgg", "bulk-recheck-startgg"])
def test_verified_recheck_without_games_metadata_preserves_games_and_price(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, route
):
    row = existing(pg_db)
    monkeypatch.setattr(backend_app.admin, "INTEGRATION_ENGINE", "n8n")
    n8n_stub.response = {"ok": True, "state": "verified_true", "isRegistered": True}
    response = app_client.post(f"/api/admin/{route}", json={"record_id": row["record_id"]})
    assert response.status_code == 200 and response.json().get("checked", 1) == 1
    unchanged(pg_db, row)


@pytest.mark.db
@pytest.mark.parametrize(
    "source,member,found", [("to", True, False), ("to", False, True), ("v2", True, False)]
)
def test_protected_legacy_callback_never_reports_a_successful_write(
    app_client, pg_db, source, member, found
):
    pg_db.seed_settings(require_startgg=False, require_payment=False)
    row = pg_db.seed_checkin(
        member=member, member_source=source, status="Ready" if member else "Pending"
    )
    response = app_client.post(
        f"/api/checkin/{row['record_id']}/member-status",
        json={"member": found, "ok": True, "state": "verified_true" if found else "verified_false"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "checkin_id": row["record_id"],
        "member": member,
        "status": "Ready" if member else "Pending",
    }
    assert pg_db.checkins()[0]["member"] is member
    assert audit(pg_db, "legacy_member_result")["updated"] is False


@pytest.mark.db
@pytest.mark.parametrize("source", ["ebas", "startgg"])
def test_stale_callback_never_reports_a_successful_write(app_client, pg_db, source):
    row = existing(pg_db)
    old = postgres.start_integration_attempt(row["record_id"], sources=(source,))
    postgres.start_integration_attempt(row["record_id"], sources=(source,))
    before = pg_db.checkins()
    payload = callback(row, source)
    payload["attempt_id"] = old
    response = app_client.post("/api/integration/result", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is False and response.json()["updated"] is False
    assert response.json()["state"] == "stale_rejected"
    assert pg_db.checkins() == before

"""Membership verification may update only the check-in named by its capability."""

import pytest
from status_api import capability_token

from tests.test_membership_endpoints import (
    isolated_membership,
    member_client,
    membership_db,
    payload,
    provider,
    sender,
)

pytestmark = pytest.mark.db


def _no_attempts(database, provider, sender):
    with database.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM membership_receipts").fetchone()[0] == 0
    assert provider.calls == []
    assert sender.calls == []


@pytest.mark.parametrize("token", [None, "", "invalid", 123, {}, []])
def test_missing_or_invalid_token_cannot_create_an_attempt(
    member_client, membership_db, provider, sender, token
):
    details = payload(membership_db)
    details.pop("status_token", None)
    if token is not None:
        details["status_token"] = token

    response = member_client.post("/api/membership/start", json=details)

    assert response.status_code == 403
    assert response.json()["error_code"] == "not_owner"
    _no_attempts(membership_db, provider, sender)


@pytest.mark.parametrize("kind", ["foreign", "expired", "tampered"])
def test_token_must_authorize_the_exact_checkin(
    member_client, membership_db, provider, sender, kind
):
    details = payload(membership_db)
    checkin_id = details["checkin_id"]
    if kind == "foreign":
        other = membership_db.seed_checkin()
        token = capability_token(other["record_id"])
    elif kind == "expired":
        token = capability_token(checkin_id, ttl=-1)
    else:
        original = capability_token(checkin_id)
        token = ("A" if original[0] != "A" else "B") + original[1:]
    details["status_token"] = token

    response = member_client.post("/api/membership/start", json=details)

    assert response.status_code == 403
    assert response.json()["error_code"] == "not_owner"
    _no_attempts(membership_db, provider, sender)
    assert all(row["member"] is False for row in membership_db.checkins())


def test_missing_checkin_cannot_bypass_owner_binding(
    member_client, membership_db, provider, sender
):
    details = payload(membership_db)
    details["status_token"] = capability_token(details.pop("checkin_id"))

    response = member_client.post("/api/membership/start", json=details)

    assert response.status_code == 403
    assert response.json()["error_code"] == "not_owner"
    _no_attempts(membership_db, provider, sender)


def test_valid_capability_keeps_start_and_verification_working(
    member_client, membership_db, sender
):
    details = payload(membership_db)
    checkin_id = details["checkin_id"]
    other = membership_db.seed_checkin()
    details["status_token"] = capability_token(checkin_id)

    started = member_client.post("/api/membership/start", json=details)

    assert started.status_code == 200
    assert started.json()["state"] == "code_sent"
    verified = member_client.post(
        "/api/membership/verify",
        json={"attempt_id": started.json()["attempt_id"], "code": sender.calls[-1]["code"]},
    )
    assert verified.status_code == 200
    assert verified.json()["state"] == "submitted"
    rows = {row["record_id"]: row for row in membership_db.checkins()}
    assert rows[checkin_id]["member"] is True
    assert rows[other["record_id"]]["member"] is False


def test_valid_token_does_not_bypass_active_event_check(
    member_client, membership_db, provider, sender
):
    details = payload(membership_db, checkin_id="unknown-synthetic-record")
    details["status_token"] = capability_token(details["checkin_id"])

    response = member_client.post("/api/membership/start", json=details)

    assert response.status_code == 404
    _no_attempts(membership_db, provider, sender)

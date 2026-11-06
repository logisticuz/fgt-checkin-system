"""Status capability checks use only synthetic keys, provider stubs and private Postgres."""

import base64
import uuid
from datetime import timedelta

import pytest
import status_api as status

from tests.fixtures import make_checkin

STATUS_FIELDS = {"found", "ready", "missing", "member", "startgg", "payment_ok", "games", "status"}
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def synthetic_signing_key(monkeypatch):
    monkeypatch.setenv("STATUS_TOKEN_KEY", "synthetic-status-token-key")
    monkeypatch.setattr(status, "_TOKEN_KEY", b"synthetic-status-signing-key-32-bytes")
    monkeypatch.setattr(status, "_now_seconds", lambda: NOW)


def decode_token(token):
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def encoded_token(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def status_url(checkin_id):
    return f"/api/checkin/{checkin_id}/status"


def test_orchestrate_issues_a_working_token_for_its_own_id(app_client, pg_db):
    checkin = app_client.post("/api/checkin/orchestrate", json=make_checkin()).json()
    checkin_id = checkin["checkin_id"]
    token = checkin["status_token"]
    assert isinstance(token, str) and token
    response = app_client.get(status_url(checkin_id), params={"t": token})
    assert response.status_code == 200
    assert response.json()["found"] is True
    assert set(response.json()) == STATUS_FIELDS
    other = pg_db.seed_checkin()
    mismatch = app_client.get(status_url(other["record_id"]), params={"t": token})
    assert mismatch.status_code == 403
    assert mismatch.json() == {"found": False, "ready": False}


def test_two_players_with_the_same_name_keep_their_own_status(app_client, pg_db):
    first = pg_db.seed_checkin(name="Same synthetic name", member=True, startgg=True)
    second = pg_db.seed_checkin(name="Same synthetic name", member=False, startgg=False)
    for row, expected in ((first, True), (second, False)):
        response = app_client.get(
            status_url(row["record_id"]), params={"t": status.capability_token(row["record_id"])}
        )
        assert response.json()["ready"] is expected
    response = app_client.get(
        status_url(second["record_id"]), params={"t": status.capability_token(first["record_id"])}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("token", [None, "", "not-a-token", "!not-base64!", "a" * 2048])
def test_invalid_tokens_are_rejected_before_storage(handler_client, monkeypatch, token):
    def forbidden():
        raise AssertionError("Invalid token reached status storage")

    monkeypatch.setattr(status.postgres_api, "get_active_settings", forbidden)
    response = handler_client.get(
        status_url(str(uuid.uuid4())), params={} if token is None else {"t": token}
    )
    assert response.status_code == 403
    assert response.json() == {"found": False, "ready": False}


def test_expired_token_is_forbidden(app_client, pg_db):
    row = pg_db.seed_checkin()
    token = status.capability_token(row["record_id"], ttl=-1)
    response = app_client.get(status_url(row["record_id"]), params={"t": token})
    assert response.status_code == 403
    assert response.json() == {"found": False, "ready": False}


def test_default_lifetime_is_six_hours_and_expiry_is_enforced(app_client, pg_db, monkeypatch):
    row = pg_db.seed_checkin()
    token = status.capability_token(row["record_id"])
    signed_payload = decode_token(token)[:-33]
    assert signed_payload.rsplit(b"|", 1)[1] == str(NOW + 6 * 3600).encode("ascii")
    monkeypatch.setattr(status, "_now_seconds", lambda: NOW + 6 * 3600)
    assert app_client.get(status_url(row["record_id"]), params={"t": token}).status_code == 403


def test_tampering_with_any_payload_or_mac_byte_is_forbidden(handler_client, monkeypatch):
    checkin_id = str(uuid.uuid4())
    raw = decode_token(status.capability_token(checkin_id))

    def forbidden():
        raise AssertionError("Tampered token reached status storage")

    monkeypatch.setattr(status.postgres_api, "get_active_settings", forbidden)
    for index in range(len(raw)):
        changed = bytearray(raw)
        changed[index] ^= 1
        response = handler_client.get(status_url(checkin_id), params={"t": encoded_token(changed)})
        assert response.status_code == 403, f"Tampered byte {index} was accepted"
        assert response.json() == {"found": False, "ready": False}


def test_noncanonical_base64_alias_and_padding_are_forbidden(handler_client):
    checkin_id = str(uuid.uuid4())
    token = status.capability_token(checkin_id)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alias = next(
        token[:-1] + character
        for character in alphabet
        if character != token[-1] and decode_token(token[:-1] + character) == decode_token(token)
    )
    for candidate in (alias, token + "="):
        assert (
            handler_client.get(status_url(checkin_id), params={"t": candidate}).status_code == 403
        )


def test_unknown_id_with_a_valid_signature_is_not_found(app_client):
    checkin_id = str(uuid.uuid4())
    response = app_client.get(
        status_url(checkin_id), params={"t": status.capability_token(checkin_id)}
    )
    assert response.status_code == 200
    assert response.json() == {"found": False, "ready": False}


def test_nonactive_event_has_no_name_or_tag_fallback(app_client, pg_db):
    old = pg_db.seed_checkin(
        event_slug="previous-event", tag="shared-tag", name="Shared name", member=True, startgg=True
    )
    pg_db.seed_checkin(tag="shared-tag", name="Shared name", member=False, startgg=False)
    response = app_client.get(
        status_url(old["record_id"]), params={"t": status.capability_token(old["record_id"])}
    )
    assert response.status_code == 200
    assert response.json() == {"found": False, "ready": False}


def test_no_active_event_is_not_found(app_client, pg_db):
    row = pg_db.seed_checkin(member=True, startgg=True)
    with pg_db.pool.connection() as connection:
        connection.execute("UPDATE settings SET is_active = false")
    response = app_client.get(
        status_url(row["record_id"]), params={"t": status.capability_token(row["record_id"])}
    )
    assert response.json() == {"found": False, "ready": False}


def test_response_whitelist_excludes_all_contact_and_personal_id_data(app_client, pg_db):
    row = pg_db.seed_checkin(
        name="200001010008",
        email="private@example.invalid",
        telephone="+12025550123",
        tournament_games_registered=["Synthetic Fighter"],
        member=True,
        startgg=True,
    )
    response = app_client.get(
        status_url(row["record_id"]), params={"t": status.capability_token(row["record_id"])}
    )
    assert set(response.json()) == STATUS_FIELDS
    assert response.json()["games"] == ["Synthetic Fighter"]
    for private in (
        "200001010008",
        "private@example.invalid",
        "+12025550123",
        "email",
        "telephone",
        "personnummer",
    ):
        assert private not in response.text


@pytest.mark.parametrize(
    "requirements,fields,ready,missing,payment_ok",
    [
        (
            {"require_membership": False, "require_startgg": False, "require_payment": False},
            {},
            True,
            [],
            True,
        ),
        (
            {"require_membership": True, "require_startgg": True, "require_payment": True},
            {},
            False,
            ["Membership", "Payment", "Start.gg"],
            False,
        ),
        (
            {"require_membership": True, "require_startgg": False, "require_payment": False},
            {"member": True},
            True,
            [],
            True,
        ),
        (
            {"require_membership": False, "require_startgg": False, "require_payment": True},
            {"payment_valid": False, "payment_amount": 100, "payment_expected": 25},
            False,
            ["Payment"],
            False,
        ),
        (
            {"require_membership": False, "require_startgg": False, "require_payment": True},
            {"payment_valid": True},
            True,
            [],
            True,
        ),
    ],
)
def test_status_uses_canonical_requirements_instead_of_stored_status(
    app_client, pg_db, requirements, fields, ready, missing, payment_ok
):
    pg_db.seed_settings(**requirements)
    row = pg_db.seed_checkin(status="Pending" if ready else "Ready", **fields)
    response = app_client.get(
        status_url(row["record_id"]), params={"t": status.capability_token(row["record_id"])}
    )
    body = response.json()
    assert body["ready"] is ready
    assert body["status"] == ("Ready" if ready else "Pending")
    assert body["missing"] == missing
    assert body["payment_ok"] is payment_ok


def test_status_page_context_requires_a_valid_matching_token(app_client, pg_db):
    row = pg_db.seed_checkin(name="Context player")
    checkin_id = row["record_id"]
    token = status.capability_token(checkin_id)
    response = app_client.get(
        "/status/Context%20player", params={"checkin_id": checkin_id, "t": token}
    )
    assert response.status_code == 200
    assert response.context["checkin_id"] == checkin_id
    assert response.context["status_token"] == token
    for params in (
        {},
        {"checkin_id": checkin_id},
        {"checkin_id": str(uuid.uuid4()), "t": token},
        {"checkin_id": checkin_id, "t": status.capability_token(checkin_id, ttl=-1)},
        {"checkin_id": checkin_id, "t": "invalid"},
    ):
        response = app_client.get("/status/Context%20player", params=params)
        assert "checkin_id" not in response.context
        assert "status_token" not in response.context


def test_explicit_status_key_and_private_secret_fallback(monkeypatch):
    monkeypatch.setenv("STATUS_TOKEN_KEY", "synthetic-explicit-key")
    explicit = status._load_signing_key()
    assert explicit == b"synthetic-explicit-key"
    monkeypatch.delenv("STATUS_TOKEN_KEY")
    for name in status._SERVER_SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMBERSHIP_ENC_KEY", "synthetic-private-server-secret")
    fallback = status._load_signing_key()
    assert fallback == status._load_signing_key()
    assert len(fallback) == 32 and fallback != explicit
    monkeypatch.setenv("MEMBERSHIP_ENC_KEY", "different-synthetic-server-secret")
    assert status._load_signing_key() != fallback


def test_ephemeral_key_warns_once_and_is_cached_per_process(monkeypatch, caplog):
    for name in ("STATUS_TOKEN_KEY", *status._SERVER_SECRETS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(status, "_TOKEN_KEY", None)
    with caplog.at_level("WARNING", logger="status_api"):
        first = status.capability_token("synthetic-checkin")
        second = status.capability_token("synthetic-checkin")
    assert first == second
    assert status.verify_capability("synthetic-checkin", first)
    assert len(caplog.records) == 1
    assert "will not survive restarts" in caplog.text


def test_key_change_invalidates_previous_capabilities(monkeypatch):
    token = status.capability_token("synthetic-checkin", ttl=timedelta(minutes=5))
    monkeypatch.setattr(status, "_TOKEN_KEY", b"different-synthetic-signing-key")
    assert not status.verify_capability("synthetic-checkin", token)

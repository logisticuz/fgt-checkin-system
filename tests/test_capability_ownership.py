"""Public repeat check-in cannot manufacture authority over another player's row."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from status_api import capability_token, verify_capability

from shared import membership_flow, postgres_api as postgres
from tests.fixtures import make_checkin
from tests.test_membership_endpoints import (
    isolated_membership,
    member_client,
    membership_db,
    payload as membership_payload,
    provider,
    sender,
)

pytestmark = pytest.mark.db
OWNER_EMAIL = "owner@example.invalid"
OWNER_PHONE = "+46701234567"
ATTACK_EMAIL = "other@example.invalid"
ATTACK_PHONE = "+46707654321"


def _owner(database, **overrides):
    return database.seed_checkin(
        **{"name": "Test Owner", "email": OWNER_EMAIL, "telephone": OWNER_PHONE, **overrides}
    )


def _repeat(row, **overrides):
    return make_checkin(
        **{
            "tag": row["tag"],
            "name": "Test Repeat",
            "email": ATTACK_EMAIL,
            "telephone": ATTACK_PHONE,
            **overrides,
        }
    )


def _legacy(row, token=None, **overrides):
    return {
        "checkin_id": row["record_id"],
        "status_token": token,
        "tag": row["tag"],
        "slug": row["event_slug"],
        "personnummer": "000000000000",
        **overrides,
    }


def _legacy_enabled(backend_app, monkeypatch):
    monkeypatch.setattr(membership_flow, "_membership_flow", "legacy")
    monkeypatch.setattr(backend_app.ebas_legacy, "INTEGRATION_ENGINE", "n8n")


def test_attack_cannot_mint_or_use_other_checkin_authority(
    member_client, membership_db, backend_app, monkeypatch, provider, sender, n8n_stub
):
    owner = _owner(membership_db, member=True, startgg=True, payment_valid=True)
    for _ in range(2):
        response = member_client.post("/api/checkin/orchestrate", json=_repeat(owner))
        assert response.status_code == 200
        body = response.json()
        assert body["checkin_id"] == owner["record_id"]
        assert body["ownership"] == "unverified"
        assert "status_token" not in body
        assert OWNER_EMAIL not in response.text and OWNER_PHONE not in response.text
    stored = membership_db.checkins()[0]
    assert (stored["name"], stored["email"], stored["telephone"]) == (
        owner["name"],
        OWNER_EMAIL,
        OWNER_PHONE,
    )
    assert stored["member"] and stored["startgg"] and stored["payment_valid"]

    games = member_client.patch(
        "/api/player/games", json={"checkin_id": owner["record_id"], "games": []}
    )
    assert games.status_code == 403
    membership = member_client.post(
        "/api/membership/start",
        json=membership_payload(checkin_id=owner["record_id"], status_token=None),
    )
    assert membership.status_code == 403
    assert membership.json()["error_code"] == "not_owner"
    _legacy_enabled(backend_app, monkeypatch)
    legacy = member_client.post("/api/ebas/register", json=_legacy(owner))
    assert legacy.status_code == 403  # Even an already-member short circuit needs authority.
    assert n8n_stub.calls == []
    assert provider.calls == [] and sender.calls == []
    with membership_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("proof", ["phone", "email"])
def test_matching_contact_issues_authority_that_works_for_all_flows(
    member_client, membership_db, backend_app, monkeypatch, sender, n8n_stub, proof
):
    owner = _owner(membership_db)
    details = (
        {"telephone": "070-123 45 67"} if proof == "phone" else {"email": " OWNER@EXAMPLE.INVALID "}
    )
    response = member_client.post("/api/checkin/orchestrate", json=_repeat(owner, **details))
    assert response.status_code == 200
    body = response.json()
    token = body["status_token"]
    assert verify_capability(owner["record_id"], token)
    assert "ownership" not in body
    with membership_db.pool.connection() as connection:
        connection.execute("UPDATE settings SET default_game = %s", (["Test Game"],))
    games = member_client.patch(
        "/api/player/games",
        json={"checkin_id": owner["record_id"], "status_token": token, "games": ["Test Game"]},
    )
    assert games.status_code == 200

    _legacy_enabled(backend_app, monkeypatch)
    n8n_stub.response = {"success": True}
    legacy = member_client.post("/api/ebas/register", json=_legacy(owner, token))
    assert legacy.status_code == 200
    assert legacy.json()["success"] is True
    assert len(n8n_stub.calls) == 1
    assert n8n_stub.calls[0]["json"]["checkin_id"] == owner["record_id"]
    assert "status_token" not in n8n_stub.calls[0]["json"]

    monkeypatch.setattr(membership_flow, "_membership_flow", "v2")
    started = member_client.post(
        "/api/membership/start",
        json=membership_payload(checkin_id=owner["record_id"], status_token=token),
    )
    assert started.status_code == 200
    verified = member_client.post(
        "/api/membership/verify",
        json={"attempt_id": started.json()["attempt_id"], "code": sender.calls[-1]["code"]},
    )
    assert verified.status_code == 200
    assert verified.json()["state"] == "submitted"


def test_new_checkin_gets_a_token(member_client, membership_db):
    response = member_client.post("/api/checkin/orchestrate", json=make_checkin())
    assert response.status_code == 200
    body = response.json()
    assert verify_capability(body["checkin_id"], body["status_token"])
    assert "ownership" not in body
    assert len(membership_db.checkins()) == 1


@pytest.mark.parametrize("phone", ["0701234567", "+46 70 123 45 67", "0046701234567"])
def test_phone_ownership_uses_normalized_swedish_form(member_client, membership_db, phone):
    owner = _owner(membership_db)
    body = member_client.post(
        "/api/checkin/orchestrate", json=_repeat(owner, telephone=phone)
    ).json()
    assert verify_capability(owner["record_id"], body["status_token"])


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_contacts_never_prove_ownership(member_client, membership_db, value):
    owner = _owner(membership_db, email=value, telephone=value)
    body = member_client.post(
        "/api/checkin/orchestrate", json=_repeat(owner, email="", telephone="")
    ).json()
    assert body["ownership"] == "unverified"
    assert "status_token" not in body


def test_parallel_first_checkins_do_not_both_claim_creation_or_replace_the_winner(
    membership_db, monkeypatch
):
    barrier = Barrier(2)
    original_lookup = postgres._find_player_uuid

    def simultaneous_lookup(*args, **kwargs):
        barrier.wait(timeout=5)
        return original_lookup(*args, **kwargs)

    monkeypatch.setattr(postgres, "CANONICAL_PLAYER_ID_ENABLED", True)
    monkeypatch.setattr(postgres, "_find_player_uuid", simultaneous_lookup)
    payloads = [
        {"tag": "test-race", "name": "Test First", "email": OWNER_EMAIL, "telephone": OWNER_PHONE},
        {
            "tag": "TEST-RACE",
            "name": "Test Second",
            "email": ATTACK_EMAIL,
            "telephone": ATTACK_PHONE,
        },
    ]
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(postgres.begin_checkin, "test-event", details, public=True)
            for details in payloads
        ]
        results = [future.result(timeout=10) for future in futures]
    assert sum(result["created"] for result in results) == 1
    assert sum(result["ownership_verified"] for result in results) == 1
    winner = next(index for index, result in enumerate(results) if result["created"])
    row = membership_db.checkins()[0]
    for field in ("email", "telephone", "name"):
        assert row[field] == payloads[winner][field]
    assert results[0]["checkin_id"] == results[1]["checkin_id"] == row["record_id"]


@pytest.mark.parametrize(
    "kind",
    ["missing", "missing_id", "foreign", "expired", "mismatched_tag", "bad_tag", "mismatched_slug"],
)
def test_legacy_registration_rejects_unowned_or_mismatched_targets(
    member_client, membership_db, backend_app, monkeypatch, n8n_stub, kind
):
    owner = _owner(membership_db)
    token = capability_token(owner["record_id"])
    details = _legacy(owner, token)
    if kind == "missing":
        details.pop("status_token")
    elif kind == "missing_id":
        details.pop("checkin_id")
    elif kind == "foreign":
        details["status_token"] = capability_token(membership_db.seed_checkin()["record_id"])
    elif kind == "expired":
        details["status_token"] = capability_token(owner["record_id"], ttl=-1)
    elif kind == "mismatched_tag":
        details["tag"] = "someone-else"
    elif kind == "bad_tag":
        details["tag"] = []
    else:
        details["slug"] = "another-event"
    _legacy_enabled(backend_app, monkeypatch)

    response = member_client.post("/api/ebas/register", json=details)

    assert response.status_code == 403
    assert n8n_stub.calls == []
    assert backend_app.ebas_legacy._ebas_registrations == {}


def test_legacy_requires_active_event_even_with_a_valid_token(
    member_client, membership_db, backend_app, monkeypatch, n8n_stub
):
    owner = _owner(membership_db, event_slug="inactive-event")
    _legacy_enabled(backend_app, monkeypatch)
    response = member_client.post(
        "/api/ebas/register", json=_legacy(owner, capability_token(owner["record_id"]))
    )
    assert response.status_code == 404
    assert n8n_stub.calls == []


@pytest.mark.parametrize("prior_ebas_attempt", [False, True])
def test_unverified_repeat_cannot_run_ebas_or_apply_its_callback(
    member_client, membership_db, backend_app, monkeypatch, n8n_stub, prior_ebas_attempt
):
    owner = _owner(membership_db)
    old_id = postgres.start_integration_attempt(owner["record_id"]) if prior_ebas_attempt else None
    monkeypatch.setattr(backend_app.checkin, "INTEGRATION_ENGINE", "n8n")
    n8n_stub.response = {
        "results": {"ebas": "verified_true", "startgg": "verified_true"},
        "integration_complete": True,
    }
    callback_results = {}

    async def integration_callbacks(request):
        outgoing = n8n_stub.calls[-1]["json"]
        assert "personnummer" not in outgoing
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app.app), base_url="http://testserver"
        ) as client:
            for source, data in (("ebas", {"member": True}), ("startgg", {"registered": True})):
                result = await client.post(
                    "/api/integration/result",
                    json={
                        "checkin_id": owner["record_id"],
                        "attempt_id": outgoing["attempt_id"],
                        "source": source,
                        "ok": True,
                        "state": "verified_true",
                        "data": data,
                    },
                )
                assert result.status_code == 200
                callback_results[source] = result.json()

    n8n_stub.on_call = integration_callbacks
    response = member_client.post(
        "/api/checkin/orchestrate", json=_repeat(owner, personnummer="200001010008")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ownership"] == "unverified" and body["reason"] == "ownership_unverified"
    assert body["integrations"] == {"ebas": "skipped", "startgg": "ok"}
    assert body["degraded"] is False
    assert "status_token" not in body
    assert callback_results["ebas"]["updated"] is False
    assert callback_results["ebas"]["state"] == "stale_rejected"
    assert callback_results["startgg"]["updated"] is True
    stored = membership_db.checkins()[0]
    assert stored["last_ebas_attempt_id"] == old_id
    assert stored["last_startgg_attempt_id"] == n8n_stub.calls[-1]["json"]["attempt_id"]
    assert stored["member"] is False and stored["startgg"] is True
    assert stored["email"] == OWNER_EMAIL and stored["telephone"] == OWNER_PHONE

"""Real membership handlers and private Postgres, with synthetic eBas/code delivery only."""

import base64
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
import membership_api as api
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from shared import (
    membership_crypto as crypto,
    membership_flow,
    membership_store as store,
    migrate,
    verification_service as verification,
)
from shared.ebas_client import EbasClient, EbasConfig
from shared.verification_sender import SendResult

PNR = "200001010008"
SECOND_PNR = "200101010007"
SPAR_NAME = "Exempelsson"
API_KEY = "synthetic-membership-ebas-key"
PUBLIC_KEYS = {
    "ok",
    "state",
    "error_code",
    "user_message",
    "tries_left",
    "attempt_id",
    "masked_destination",
    "resend_after_s",
    "retry_after_s",
    "field_errors",
}


@pytest.fixture(autouse=True)
def isolated_membership(monkeypatch):
    monkeypatch.setenv("MEMBERSHIP_ENC_KEY", base64.b64encode(bytes(range(32))).decode("ascii"))
    monkeypatch.setattr(membership_flow, "_membership_flow", "v2")
    monkeypatch.setattr(api, "CLEANUP_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(api, "RECONCILE_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 3600)


class Provider:
    def __init__(self):
        self.calls = []
        self.transports = []
        self.validation_errors = {}
        self.validation_unavailable = False
        self.submit_outcome = "submitted"
        self.confirm_found = True
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()

    def client(self):
        transport = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.transports.append(transport)
        return EbasClient(EbasConfig(API_KEY), transport)

    def handle(self, request):
        action = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        with self.lock:
            self.calls.append((action, json.loads(request.content)))
        echo = {"socialsecuritynumber": PNR, "api_key": API_KEY, "lastname": SPAR_NAME}
        if action == "validate_members":
            if self.validation_unavailable:
                raise httpx.ConnectError(
                    f"Provider echo {PNR} {API_KEY} {SPAR_NAME}", request=request
                )
            return httpx.Response(
                200,
                json={
                    "response": {
                        "request_result": "fail" if self.validation_errors else "success",
                        "spar_names": {"firstname": "Test", "lastname": SPAR_NAME},
                        "member_errors": [self.validation_errors],
                        "member_warnings": None,
                    },
                    "request_echo": echo,
                },
            )
        if action == "submit_member_with_lookup":
            self.started.set()
            if not self.release.wait(timeout=5):
                raise httpx.ReadTimeout("Synthetic delayed provider", request=request)
            if self.submit_outcome == "timeout":
                raise httpx.ReadTimeout(
                    f"Accepted request {PNR} {API_KEY} {SPAR_NAME}", request=request
                )
            if self.submit_outcome == "unavailable":
                return httpx.Response(503, json=echo)
            if self.submit_outcome == "in_progress":
                return httpx.Response(
                    200,
                    json={
                        "error": "Identical request is already being processed. Wait for the response before making a new request."
                    },
                )
            return httpx.Response(
                200,
                json={
                    "stored_member": self.submit_outcome == "submitted",
                    "request_result": "success" if self.submit_outcome == "submitted" else "fail",
                    "member_errors": (
                        {"email": [str(echo)]} if self.submit_outcome == "rejected" else {}
                    ),
                    "submit_request_echo": echo,
                    "spar_names": {"lastname": SPAR_NAME},
                },
            )
        if self.confirm_found is None:
            raise httpx.ConnectError(f"Provider echo {PNR} {API_KEY}", request=request)
        return httpx.Response(
            200,
            json={
                "response": {
                    "member_found": self.confirm_found,
                    "registration_source": "Inskriven manuellt",
                },
                "request_echo": echo,
            },
        )

    def count(self, action):
        return sum(name == action for name, _ in self.calls)


class Sender:
    def __init__(self):
        self.calls = []
        self.ok = True
        self.on_send = None

    def send_code(self, channel, to, code, lang):
        if self.on_send is not None:
            self.on_send()
        self.calls.append({"channel": channel, "to": to, "code": code, "lang": lang})
        return SendResult(self.ok, None if self.ok else "provider_unavailable")


@pytest.fixture
def provider(monkeypatch):
    provider = Provider()
    monkeypatch.setattr(api, "_get_ebas_client", provider.client)
    yield provider
    provider.release.set()
    for transport in provider.transports:
        transport.close()


@pytest.fixture
def sender(monkeypatch):
    sender = Sender()
    monkeypatch.setattr(verification, "get_sender", lambda: sender)
    return sender


@pytest.fixture
def membership_db(pg_db):
    migrate.apply(pg_db.pool)
    pg_db.seed_settings(require_startgg=False, require_payment=False)
    return pg_db


def payload(database=None, **overrides):
    record = database.seed_checkin() if database is not None else None
    details = {
        "personnummer": PNR,
        "email": "kiosk-one@example.invalid",
        "phone": "070-123 45 67",
        "channel": "sms",
        "accepted_bylaws": True,
        "accepted_privacy": True,
        "bylaws_version": api.BYLAWS_VERSION,
        "privacy_version": api.PRIVACY_VERSION,
        "checkin_id": record["record_id"] if record is not None else None,
    }
    details.update(overrides)
    if details.get("checkin_id"):
        from status_api import capability_token

        details.setdefault("status_token", capability_token(details["checkin_id"]))
    return details


def row(database, attempt_id):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM membership_attempts WHERE id = %s", (attempt_id,))
            return cursor.fetchone()


def assert_public(response):
    assert set(response.json()) <= PUBLIC_KEYS
    for value in (PNR, SECOND_PNR, SPAR_NAME, API_KEY, "request_echo", "spar_names"):
        assert value not in response.text


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("Synthetic membership operation did not finish")


@pytest.mark.parametrize("flow", ["legacy", "off"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/membership/start"),
        ("POST", "/api/membership/verify"),
        ("POST", "/api/membership/resend"),
        ("GET", "/api/membership/00000000-0000-0000-0000-000000000001/status"),
    ],
)
def test_other_modes_reject_before_body_storage_or_providers(
    handler_client, monkeypatch, flow, method, path
):
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)

    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled membership route touched a dependency")

    monkeypatch.setattr(api, "_get_ebas_client", forbidden)
    monkeypatch.setattr(store, "get_attempt", forbidden, raising=False)
    response = handler_client.request(method, path, content="{")
    assert response.status_code == 404
    assert_public(response)


@pytest.mark.parametrize("field", ["accepted_bylaws", "accepted_privacy"])
@pytest.mark.parametrize("value", [False, None, 0, 1, "true", []])
def test_consent_must_be_a_strict_json_boolean(handler_client, provider, sender, field, value):
    response = handler_client.post("/api/membership/start", json=payload(**{field: value}))
    assert response.status_code == 400
    assert response.json()["error_code"] == "consent_required"
    assert provider.calls == sender.calls == []
    assert_public(response)


@pytest.mark.parametrize("field", ["bylaws_version", "privacy_version"])
def test_stale_texts_are_rejected_before_validation(handler_client, provider, field):
    response = handler_client.post("/api/membership/start", json=payload(**{field: "obsolete"}))
    assert response.status_code == 409
    assert response.json()["error_code"] == "stale_texts"
    assert provider.calls == []
    assert_public(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("personnummer", "200001010009"),
        ("email", "not-an-email"),
        ("phone", "not-a-phone"),
        ("channel", "push"),
        ("checkin_id", []),
    ],
)
def test_local_details_validation_returns_only_field_names(handler_client, provider, field, value):
    response = handler_client.post("/api/membership/start", json=payload(**{field: value}))
    assert response.status_code == 422
    assert response.json()["field_errors"] == [field]
    assert provider.calls == []
    assert_public(response)


@pytest.fixture
def member_client(membership_db, provider, sender, backend_app):
    with TestClient(backend_app.app) as client:
        yield client


def start_attempt(client, database, **overrides):
    details = payload(database, **overrides)
    response = client.post("/api/membership/start", json=details)
    assert response.status_code == 200, response.text
    assert_public(response)
    return response.json()["attempt_id"], details


def test_start_persists_consent_and_encrypted_context_before_delivery(
    member_client, membership_db, sender
):
    def assert_stored():
        with membership_db.pool.connection() as connection:
            attempt = connection.execute(
                "SELECT state, pnr_enc, context_enc FROM membership_attempts"
            ).fetchone()
            assert attempt[0] == "code_sent"
            assert PNR.encode() not in bytes(attempt[1])
            assert b"kiosk-one@example.invalid" not in bytes(attempt[2])
            assert connection.execute("SELECT count(*) FROM membership_receipts").fetchone()[0] == 1

    sender.on_send = assert_stored
    attempt_id, _ = start_attempt(member_client, membership_db)
    assert sender.calls[0]["to"] == "+46701234567"
    assert sender.calls[0]["lang"] == "en"
    with membership_db.pool.connection() as connection:
        receipt = connection.execute(
            "SELECT bylaws_version, privacy_version, consented_at FROM membership_receipts WHERE attempt_id = %s",
            (attempt_id,),
        ).fetchone()
    assert receipt[:2] == (api.BYLAWS_VERSION, api.PRIVACY_VERSION)
    assert receipt[2].tzinfo is not None


def test_validation_errors_and_unavailability_do_not_send_codes(
    member_client, membership_db, provider, sender
):
    provider.validation_errors = {"email": [f"Echo {PNR} {SPAR_NAME}"], SPAR_NAME: [PNR]}
    response = member_client.post("/api/membership/start", json=payload(membership_db))
    assert response.status_code == 422
    assert response.json()["field_errors"] == ["email"]
    assert_public(response)
    provider.validation_unavailable = True
    response = member_client.post("/api/membership/start", json=payload(membership_db))
    assert response.status_code == 503
    assert response.json()["error_code"] == "ebas_unavailable"
    assert sender.calls == []
    assert_public(response)


def test_sender_failure_erases_secrets_and_preserves_receipt(member_client, membership_db, sender):
    sender.ok = False
    response = member_client.post("/api/membership/start", json=payload(membership_db))
    assert response.status_code == 502
    assert response.json()["error_code"] == "send_failed"
    with membership_db.pool.connection() as connection:
        assert connection.execute(
            "SELECT state, pnr_enc, context_enc FROM membership_attempts"
        ).fetchone() == ("failed", None, None)
        assert connection.execute("SELECT count(*) FROM membership_receipts").fetchone()[0] == 1
    assert_public(response)


def test_double_click_submits_exactly_once(member_client, membership_db, provider, sender):
    attempt_id, details = start_attempt(member_client, membership_db)
    code = sender.calls[-1]["code"]
    provider.release.clear()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                member_client.post,
                "/api/membership/verify",
                json={"attempt_id": attempt_id, "code": code},
            )
            assert provider.started.wait(timeout=3)
            second = member_client.post(
                "/api/membership/verify", json={"attempt_id": attempt_id, "code": code}
            )
            assert second.status_code == 200
            assert second.json()["state"] == "submitting"
            assert second.json()["ok"] is False
            provider.release.set()
            completed = first.result(timeout=5)
    finally:
        provider.release.set()
    assert completed.json()["state"] == "submitted"
    assert provider.count("submit_member_with_lookup") == 1
    attempt = row(membership_db, attempt_id)
    assert attempt["pnr_enc"] is None and attempt["context_enc"] is None
    with membership_db.pool.connection() as connection:
        assert connection.execute(
            "SELECT member, status FROM active_event_data WHERE record_id = %s",
            (details["checkin_id"],),
        ).fetchone() == (True, "Ready")
        assert connection.execute(
            "SELECT outcome, verified_at IS NOT NULL FROM membership_receipts WHERE attempt_id = %s",
            (attempt_id,),
        ).fetchone() == ("submitted", True)
    assert_public(second)
    assert_public(completed)


def test_five_wrong_codes_lock_without_submitting(member_client, membership_db, provider, sender):
    attempt_id, _ = start_attempt(member_client, membership_db)
    wrong = "000000" if sender.calls[-1]["code"] != "000000" else "111111"
    for index in range(5):
        response = member_client.post(
            "/api/membership/verify", json={"attempt_id": attempt_id, "code": wrong}
        )
        assert response.status_code == 200
        assert response.json()["tries_left"] == 4 - index
        assert response.json()["error_code"] == ("locked" if index == 4 else "wrong_code")
        assert_public(response)
    assert row(membership_db, attempt_id)["pnr_enc"] is None
    assert provider.count("submit_member_with_lookup") == 0


@pytest.mark.parametrize("outcome", ["timeout", "unavailable", "in_progress"])
def test_uncertain_submit_reconciles_without_resubmitting(
    member_client, membership_db, provider, sender, outcome
):
    provider.submit_outcome = outcome
    attempt_id, _ = start_attempt(member_client, membership_db)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert response.json()["state"] == "outcome_unknown"
    assert row(membership_db, attempt_id)["pnr_enc"] is not None
    reconciled = member_client.get(f"/api/membership/{attempt_id}/status")
    assert reconciled.json()["state"] == "submitted"
    assert provider.count("submit_member_with_lookup") == 1
    assert provider.count("confirm_membership") == 1
    assert row(membership_db, attempt_id)["pnr_enc"] is None
    assert_public(response)
    assert_public(reconciled)


def test_negative_reconcile_remains_uncertain_and_does_not_clear_membership(
    member_client, membership_db, provider, sender
):
    provider.submit_outcome = "timeout"
    provider.confirm_found = False
    details = payload(membership_db)
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE active_event_data SET member = true WHERE record_id = %s",
            (details["checkin_id"],),
        )
    started = member_client.post("/api/membership/start", json=details)
    attempt_id = started.json()["attempt_id"]
    member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET submit_started_at = clock_timestamp() - interval '11 minutes' WHERE id = %s",
            (attempt_id,),
        )
    for _ in range(2):
        response = member_client.get(f"/api/membership/{attempt_id}/status")
        assert response.json()["state"] == "outcome_unknown"
        assert_public(response)
    assert provider.count("confirm_membership") == 1
    assert provider.count("submit_member_with_lookup") == 1
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT member FROM active_event_data WHERE record_id = %s",
                (details["checkin_id"],),
            ).fetchone()[0]
            is True
        )


def test_long_submit_returns_202_and_background_work_finishes(
    member_client, membership_db, provider, sender, monkeypatch
):
    monkeypatch.setattr(api, "SUBMIT_WAIT_SECONDS", 0.01)
    attempt_id, _ = start_attempt(member_client, membership_db)
    provider.release.clear()
    try:
        response = member_client.post(
            "/api/membership/verify",
            json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]},
        )
        assert response.status_code == 202
        assert response.json()["state"] == "submitting"
        assert api._submission_tasks
    finally:
        provider.release.set()
    wait_until(lambda: row(membership_db, attempt_id)["state"] == "submitted")
    assert provider.count("submit_member_with_lookup") == 1
    assert_public(response)


def test_sms_spam_limit_returns_retry_after(member_client, membership_db, sender):
    for _ in range(3):
        start_attempt(member_client, membership_db)
    response = member_client.post("/api/membership/start", json=payload(membership_db))
    assert response.status_code == 429
    assert response.json()["error_code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) == response.json()["retry_after_s"] > 0
    assert len(sender.calls) == 3
    assert_public(response)


def test_resend_rotates_code_without_resetting_attempt_count(
    member_client, membership_db, provider, sender, monkeypatch
):
    codes = iter(["123456", "654321", "222222"])
    monkeypatch.setattr(crypto, "generate_code", lambda: next(codes))
    attempt_id, _ = start_attempt(member_client, membership_db)
    cooldown = member_client.post("/api/membership/resend", json={"attempt_id": attempt_id})
    assert cooldown.status_code == 429
    assert len(sender.calls) == 1
    member_client.post("/api/membership/verify", json={"attempt_id": attempt_id, "code": "000000"})
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET code_sent_at = clock_timestamp() - interval '61 seconds' WHERE id = %s",
            (attempt_id,),
        )
    response = member_client.post("/api/membership/resend", json={"attempt_id": attempt_id})
    assert response.status_code == 200
    assert response.json()["state"] == "code_sent"
    assert len(sender.calls) == 2
    assert row(membership_db, attempt_id)["verify_attempts"] == 1
    old = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": "123456"}
    )
    assert old.json()["error_code"] == "wrong_code"
    verified = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert verified.json()["state"] == "submitted"
    assert provider.count("submit_member_with_lookup") == 1


def test_two_kiosk_users_have_separate_capabilities(
    member_client, membership_db, sender, monkeypatch
):
    codes = iter(["123456", "654321"])
    monkeypatch.setattr(crypto, "generate_code", lambda: next(codes))
    first_id, _ = start_attempt(member_client, membership_db)
    second_id, _ = start_attempt(
        member_client,
        membership_db,
        personnummer=SECOND_PNR,
        email="kiosk-two@example.invalid",
        phone="0707654321",
    )
    assert first_id != second_id
    second_status = member_client.get(f"/api/membership/{second_id}/status")
    assert first_id not in second_status.text
    assert "kiosk-one@example.invalid" not in second_status.text
    wrong_user_code = member_client.post(
        "/api/membership/verify", json={"attempt_id": second_id, "code": "123456"}
    )
    assert wrong_user_code.json()["error_code"] == "wrong_code"
    unknown = str(uuid.uuid4())
    for response in (
        member_client.get(f"/api/membership/{unknown}/status"),
        member_client.post(
            "/api/membership/verify", json={"attempt_id": unknown, "code": "123456"}
        ),
    ):
        assert response.status_code == 404
        assert_public(response)
    assert_public(second_status)


def direct_attempt(database, *, submitting=False):
    details = payload(database)
    attempt_id = store.create_attempt(
        pnr=PNR,
        contact="+46701234567",
        channel="sms",
        event_slug="test-event",
        code="123456",
        bylaws_version=api.BYLAWS_VERSION,
        privacy_version=api.PRIVACY_VERSION,
        checkin_id=details["checkin_id"],
        email=details["email"],
        phone="+46701234567",
        lang="en",
    )
    if submitting:
        assert store.begin_submit(attempt_id, "123456").status == "submitting"
        store.finish_submit(attempt_id, "outcome_unknown")
    return attempt_id, details


def test_service_challenge_and_submission_claim_are_private(membership_db, sender):
    attempt_id, _ = direct_attempt(membership_db)
    challenge = verification.start_verification(attempt_id, "sms", "+46701234567", code="123456")
    assert set(challenge) == {"challenge_ref", "expires_at", "resend_policy"}
    assert challenge["resend_policy"]["resend_after_s"] == 60
    checked = verification.check_verification(attempt_id, challenge["challenge_ref"], "123456")
    assert checked.status == "verified"
    assert checked.submission_claim.pnr == PNR
    assert PNR not in repr(checked)
    follower = verification.check_verification(attempt_id, challenge["challenge_ref"], "123456")
    assert follower.status == "unavailable"
    assert follower.submission_claim.pnr is None


@pytest.mark.parametrize("flow", ["v2", "legacy", "off"])
def test_cleanup_runs_in_every_mode_and_preserves_receipt(
    membership_db, provider, sender, backend_app, monkeypatch, flow
):
    attempt_id, _ = direct_attempt(membership_db)
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = now() - interval '16 minutes', expires_at = now() - interval '1 minute' WHERE id = %s",
            (attempt_id,),
        )
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    monkeypatch.setattr(api, "CLEANUP_INTERVAL_SECONDS", 0.01)
    with TestClient(backend_app.app):
        wait_until(lambda: row(membership_db, attempt_id)["pnr_enc"] is None)
    attempt = row(membership_db, attempt_id)
    assert attempt["state"] == "expired"
    assert attempt["context_enc"] is None and attempt["code_hash"] is None
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("flow", ["legacy", "off"])
def test_background_reconcile_continues_when_public_routes_are_disabled(
    membership_db, provider, sender, backend_app, monkeypatch, flow
):
    attempt_id, details = direct_attempt(membership_db, submitting=True)
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 0.01)
    with TestClient(backend_app.app) as client:
        assert client.get(f"/api/membership/{attempt_id}/status").status_code == 404
        wait_until(lambda: row(membership_db, attempt_id)["state"] == "submitted")
    assert provider.count("confirm_membership") == 1
    assert provider.count("submit_member_with_lookup") == 0
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT member FROM active_event_data WHERE record_id = %s",
                (details["checkin_id"],),
            ).fetchone()[0]
            is True
        )


def test_uncertain_deadline_becomes_manual_review_without_resubmitting(
    member_client, membership_db, provider
):
    attempt_id, _ = direct_attempt(membership_db, submitting=True)
    provider.confirm_found = False
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = now() - interval '16 minutes', expires_at = now() - interval '1 minute' WHERE id = %s",
            (attempt_id,),
        )
    store.cleanup_expired()
    response = member_client.get(f"/api/membership/{attempt_id}/status")
    assert response.json()["state"] == "manual_review"
    assert response.json()["user_message"] == "Ask a TO to check your membership"
    attempt = row(membership_db, attempt_id)
    assert attempt["pnr_enc"] is None and attempt["context_enc"] is None
    assert provider.count("submit_member_with_lookup") == 0
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
            ).fetchone()[0]
            == "manual_review"
        )
    assert_public(response)


def test_background_repairs_a_submitted_checkin_without_resubmitting(
    membership_db, provider, sender, backend_app, monkeypatch
):
    attempt_id, details = direct_attempt(membership_db, submitting=True)
    store.finish_submit(attempt_id, "submitted")
    monkeypatch.setattr(membership_flow, "_membership_flow", "off")
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 0.01)

    def member_updated():
        with membership_db.pool.connection() as connection:
            return connection.execute(
                "SELECT member FROM active_event_data WHERE record_id = %s",
                (details["checkin_id"],),
            ).fetchone()[0]

    with TestClient(backend_app.app):
        wait_until(member_updated)
    assert row(membership_db, attempt_id)["state"] == "submitted"
    assert provider.calls == []


def test_responses_and_logs_never_contain_provider_or_contact_data(
    member_client, membership_db, provider, sender, caplog
):
    details = payload(membership_db)
    with caplog.at_level("DEBUG"):
        started = member_client.post("/api/membership/start", json=details)
        attempt_id = started.json()["attempt_id"]
        verified = member_client.post(
            "/api/membership/verify",
            json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]},
        )
        status = member_client.get(f"/api/membership/{attempt_id}/status")
    for response in (started, verified, status):
        assert_public(response)
    for private in (
        PNR,
        SPAR_NAME,
        API_KEY,
        details["email"],
        "+46701234567",
        sender.calls[-1]["code"],
    ):
        assert private not in caplog.text


@pytest.mark.parametrize("path", ["/register", "/register.html"])
def test_registration_pages_include_current_year_and_opaque_checkin_id(member_client, path):
    checkin_id = str(uuid.uuid4())
    response = member_client.get(path, params={"checkin_id": checkin_id})
    assert response.status_code == 200
    assert response.context["membership_year"] == api.date.today().year
    assert response.context["checkin_id"] == checkin_id
    for query in ({}, {"checkin_id": "invalid"}):
        response = member_client.get(path, params=query)
        assert "checkin_id" not in response.context


def old_submission(database, attempt_id):
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = now() - interval '5 minutes', "
            "expires_at = now() + interval '10 minutes', "
            "submit_started_at = now() - interval '261 seconds' WHERE id = %s",
            (attempt_id,),
        )


def test_abandoned_claim_is_recovered_atomically_without_a_new_submit(membership_db):
    attempt_id, _ = direct_attempt(membership_db)
    store.begin_submit(attempt_id, "123456")
    assert store.recover_abandoned_submitting(260) == 0
    assert row(membership_db, attempt_id)["state"] == "submitting"
    old_submission(membership_db, attempt_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.recover_abandoned_submitting, [260, 260]))
    assert sorted(results) == [0, 1]
    assert store.get_attempt(attempt_id).state == "outcome_unknown"
    assert store.begin_submit(attempt_id, "123456").status == "already_submitting"
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT outcome FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
            ).fetchone()[0]
            == "outcome_unknown"
        )


def test_restart_simulation_recovers_via_confirmation_only(
    membership_db, provider, sender, backend_app, monkeypatch
):
    attempt_id, details = direct_attempt(membership_db)
    store.begin_submit(attempt_id, "123456")  # The process discards this in-memory claim.
    old_submission(membership_db, attempt_id)
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 0.01)
    with TestClient(backend_app.app):
        wait_until(lambda: row(membership_db, attempt_id)["state"] == "submitted")
    assert provider.count("confirm_membership") == 1
    assert provider.count("submit_member_with_lookup") == 0
    with membership_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT member FROM active_event_data WHERE record_id = %s",
                (details["checkin_id"],),
            ).fetchone()[0]
            is True
        )


def test_recovered_claim_with_negative_confirmation_keeps_waiting(
    member_client, membership_db, provider
):
    attempt_id, _ = direct_attempt(membership_db)
    store.begin_submit(attempt_id, "123456")
    old_submission(membership_db, attempt_id)
    provider.confirm_found = False
    assert store.recover_abandoned_submitting(260) == 1
    response = member_client.get(f"/api/membership/{attempt_id}/status")
    assert response.json()["state"] == "outcome_unknown"
    assert "error_code" not in response.json()
    assert response.json()["user_message"] == api._STATE_MESSAGES["outcome_unknown"]
    assert provider.count("submit_member_with_lookup") == 0


def test_finalization_failure_recovers_without_submitting_twice(
    member_client, membership_db, provider, sender, monkeypatch
):
    attempt_id, _ = start_attempt(member_client, membership_db)
    original_finish = store.finish_submit
    failed_once = False

    def transient_finish(identifier, state, error_code=None):
        nonlocal failed_once
        if state == "submitted" and not failed_once:
            failed_once = True
            raise store.MembershipStoreError("Synthetic finalization outage")
        return original_finish(identifier, state, error_code)

    monkeypatch.setattr(store, "finish_submit", transient_finish)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert response.status_code == 503
    assert row(membership_db, attempt_id)["state"] == "submitting"
    old_submission(membership_db, attempt_id)
    assert store.recover_abandoned_submitting(260) == 1
    reconciled = member_client.get(f"/api/membership/{attempt_id}/status")
    assert reconciled.json()["state"] == "submitted"
    assert provider.count("submit_member_with_lookup") == 1
    assert provider.count("confirm_membership") == 1
    assert_public(response)
    assert_public(reconciled)


def test_worker_handoff_failure_becomes_uncertain_immediately(
    member_client, membership_db, provider, sender, monkeypatch
):
    attempt_id, _ = start_attempt(member_client, membership_db)

    def cannot_spawn(*args):
        raise RuntimeError("Synthetic worker handoff failure")

    monkeypatch.setattr(api, "_spawn_submission", cannot_spawn)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert response.json()["state"] == "outcome_unknown"
    assert row(membership_db, attempt_id)["state"] == "outcome_unknown"
    assert provider.count("submit_member_with_lookup") == 0
    reconciled = member_client.get(f"/api/membership/{attempt_id}/status")
    assert reconciled.json()["state"] == "submitted"
    assert provider.count("confirm_membership") == 1


@pytest.mark.parametrize("state", ["submitting", "outcome_unknown"])
def test_unresolved_claim_deadline_always_becomes_manual_review(
    member_client, membership_db, provider, state
):
    attempt_id, _ = direct_attempt(membership_db)
    store.begin_submit(attempt_id, "123456")
    if state == "outcome_unknown":
        store.finish_submit(attempt_id, "outcome_unknown")
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = now() - interval '16 minutes', "
            "expires_at = now() - interval '1 minute' WHERE id = %s",
            (attempt_id,),
        )
    store.cleanup_expired()
    attempt = store.get_attempt(attempt_id)
    assert attempt.state == "manual_review"
    assert attempt.pnr_enc is None and attempt.context_enc is None and attempt.code_hash is None
    for response in (
        member_client.get(f"/api/membership/{attempt_id}/status"),
        member_client.post(
            "/api/membership/verify", json={"attempt_id": attempt_id, "code": "123456"}
        ),
    ):
        assert response.json()["state"] == "manual_review"
        assert response.json()["user_message"] == "Ask a TO to check your membership"
    assert provider.calls == []


def test_expired_retained_worker_claim_never_reaches_the_provider(membership_db, provider, sender):
    attempt_id, _ = direct_attempt(membership_db)
    claim = store.begin_submit(attempt_id, "123456")
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = now() - interval '16 minutes', "
            "expires_at = now() - interval '1 minute' WHERE id = %s",
            (attempt_id,),
        )
    response = api._submit(attempt_id, claim, store.get_attempt(attempt_id))
    assert json.loads(response.body)["state"] == "manual_review"
    assert row(membership_db, attempt_id)["pnr_enc"] is None
    assert provider.calls == []


def test_new_year_requires_fresh_consent_before_any_submit(
    member_client, membership_db, provider, sender, monkeypatch
):
    today = [date(2026, 12, 31)]
    monkeypatch.setattr(api, "_today", lambda: today[0])
    old_id, _ = start_attempt(member_client, membership_db)
    assert store.get_attempt(old_id).membership_year == 2026
    today[0] = date(2027, 1, 1)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": old_id, "code": sender.calls[-1]["code"]}
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "year_changed"
    assert response.json()["user_message"] == "Please start again"
    assert row(membership_db, old_id)["state"] == "failed"
    assert row(membership_db, old_id)["pnr_enc"] is None
    assert provider.count("submit_member_with_lookup") == 0
    fresh_id, _ = start_attempt(member_client, membership_db)
    assert store.get_attempt(fresh_id).membership_year == 2027
    fresh = member_client.post(
        "/api/membership/verify", json={"attempt_id": fresh_id, "code": sender.calls[-1]["code"]}
    )
    assert fresh.json()["state"] == "submitted"
    submits = [
        payload for action, payload in provider.calls if action == "submit_member_with_lookup"
    ]
    assert len(submits) == 1 and submits[0]["member"]["renewed"] == "2027-01-01"
    assert_public(response)


def test_year_is_checked_again_at_worker_handoff(
    member_client, membership_db, provider, sender, monkeypatch
):
    today = [date(2026, 12, 31)]
    monkeypatch.setattr(api, "_today", lambda: today[0])
    attempt_id, _ = start_attempt(member_client, membership_db)
    original_spawn = api._spawn_submission

    def midnight_handoff(*args):
        today[0] = date(2027, 1, 1)
        return original_spawn(*args)

    monkeypatch.setattr(api, "_spawn_submission", midnight_handoff)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "year_changed"
    assert row(membership_db, attempt_id)["outcome_error_code"] == "year_changed"
    assert provider.count("submit_member_with_lookup") == 0


def test_reconciliation_keeps_the_consented_year_after_midnight(
    member_client, membership_db, provider, sender, monkeypatch
):
    today = [date(2026, 12, 31)]
    monkeypatch.setattr(api, "_today", lambda: today[0])
    provider.submit_outcome = "timeout"
    attempt_id, _ = start_attempt(member_client, membership_db)
    response = member_client.post(
        "/api/membership/verify", json={"attempt_id": attempt_id, "code": sender.calls[-1]["code"]}
    )
    assert response.json()["state"] == "outcome_unknown"
    today[0] = date(2027, 1, 1)
    reconciled = member_client.get(f"/api/membership/{attempt_id}/status")
    assert reconciled.json()["state"] == "submitted"
    submitted = next(
        payload for action, payload in provider.calls if action == "submit_member_with_lookup"
    )
    confirmed = next(
        payload for action, payload in provider.calls if action == "confirm_membership"
    )
    assert submitted["member"]["renewed"] == "2026-12-31"
    assert confirmed["request"]["year_id"] == store.get_attempt(attempt_id).membership_year == 2026
    assert provider.count("submit_member_with_lookup") == 1

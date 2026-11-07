"""Check-in eBas confirmation stays in Python and uses the canonical attempt-safe writer."""

import asyncio
import json
import logging
import threading
import time
from datetime import date
from unittest.mock import Mock

import httpx
import pytest
from starlette.concurrency import run_in_threadpool

from shared import membership_check
from shared import postgres_api as postgres
from shared.ebas_client import EbasClient, EbasConfig
from tests.fixtures import make_checkin

pytestmark = pytest.mark.db
PNR = "200001010016"
OWNER_EMAIL = "python-owner@example.invalid"
OWNER_PHONE = "+46701234567"


class PythonProvider:
    def __init__(self):
        self.outcome = True
        self.calls = []
        self.clients = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def client(self):
        transport = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.clients.append(transport)
        return EbasClient(EbasConfig("synthetic-python-ebas-key"), transport)

    def handle(self, request):
        data = json.loads(request.content)["request"]
        self.calls.append(
            {
                "data": data,
                "thread": threading.get_ident(),
                "timeout": request.extensions["timeout"],
            }
        )
        self.entered.set()
        assert self.release.wait(5), "Synthetic provider was not released"
        echo = PNR + " private-provider@example.invalid Provider Private Synthetic Name"
        logging.getLogger("httpcore.python_check_diagnostic").warning(
            "Provider metadata: %s", echo
        )
        if self.outcome == "exception":
            raise httpx.ConnectError(echo, request=request)
        if self.outcome == "http_error":
            return httpx.Response(
                502,
                json={"error": echo},
                extensions={"reason_phrase": echo.encode("ascii")},
            )
        value = "malformed" if self.outcome == "malformed" else self.outcome
        return httpx.Response(
            200,
            json={"response": {"member_found": value}},
            extensions={"reason_phrase": echo.encode("ascii")},
        )


@pytest.fixture
def python_provider(monkeypatch):
    provider = PythonProvider()
    monkeypatch.setattr(membership_check, "create_ebas_client", provider.client)
    yield provider
    provider.release.set()
    for client in provider.clients:
        client.close()


@pytest.fixture
def integrations(backend_app, n8n_stub, monkeypatch):
    monkeypatch.setattr(backend_app.checkin, "INTEGRATION_ENGINE", "n8n")
    result = {"callbacks": {}, "loop_thread": None}
    # Deliberately contradict native results: n8n eBas telemetry must be ignored.
    n8n_stub.response = {
        "results": {"ebas": "verified_true", "startgg": "verified_true"}
    }

    async def callbacks(request):
        outgoing = n8n_stub.calls[-1]["json"]
        result["loop_thread"] = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app.app),
            base_url="http://testserver",
        ) as client:
            for source, data in (
                ("ebas", {"member": True}),
                ("startgg", {"registered": True}),
            ):
                response = await client.post(
                    "/api/integration/result",
                    json={
                        "checkin_id": outgoing["checkin_id"],
                        "attempt_id": outgoing["attempt_id"],
                        "source": source,
                        "ok": True,
                        "state": "verified_true",
                        "data": data,
                    },
                )
                assert response.status_code == 200
                result["callbacks"][source] = response.json()

    result["run_callbacks"] = callbacks
    n8n_stub.on_call = callbacks
    return result


def owner(database, *, member=False, source="ebas_check"):
    return database.seed_checkin(
        email=OWNER_EMAIL,
        telephone=OWNER_PHONE,
        member=member,
        member_source=source,
        startgg=True,
    )


def repeat(row, **values):
    return make_checkin(
        tag=row["tag"],
        email=OWNER_EMAIL,
        telephone=OWNER_PHONE,
        personnummer=PNR,
        **values
    )


@pytest.mark.parametrize(
    "outcome, initial, expected, availability",
    [
        (True, False, True, "ok"),
        (False, True, False, "ok"),
        ("exception", False, False, "unavailable"),
        ("http_error", False, False, "unavailable"),
        ("malformed", True, True, "unavailable"),
    ],
)
def test_owned_pnr_runs_one_python_check_off_loop_and_never_reaches_n8n(
    app_client,
    pg_db,
    n8n_stub,
    integrations,
    python_provider,
    caplog,
    outcome,
    initial,
    expected,
    availability,
):
    row = owner(pg_db, member=initial)
    python_provider.outcome = outcome
    caplog.set_level(logging.INFO)
    response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
    assert response.status_code == 200
    body = response.json()
    assert body["integrations"] == {"ebas": availability, "startgg": "ok"}
    assert body["degraded"] is (availability == "unavailable")
    stored = pg_db.checkins()[0]
    assert stored["member"] is expected and stored["member_source"] == "ebas_check"
    assert len(python_provider.calls) == len(n8n_stub.calls) == 1
    call = python_provider.calls[0]
    assert call["data"]["socialsecuritynumber"] == PNR
    assert call["data"]["year_id"] == date.today().year
    assert call["thread"] != integrations["loop_thread"]
    assert "personnummer" not in n8n_stub.calls[0]["json"]
    assert PNR not in json.dumps(n8n_stub.calls) + response.text + caplog.text
    assert "private-provider@example.invalid" not in response.text + caplog.text
    assert "Provider Private Synthetic Name" not in response.text + caplog.text
    assert stored["last_startgg_attempt_id"] == n8n_stub.calls[0]["json"]["attempt_id"]
    assert stored["last_ebas_attempt_id"] != stored["last_startgg_attempt_id"]
    assert integrations["callbacks"]["ebas"]["state"] == "stale_rejected"
    if availability == "ok":
        assert body["member"] is expected
        assert ("Membership" in body["missing"]) is (not expected)
    else:
        assert body["member"] is (True if expected else None)
        assert "Membership" not in body["missing"]


def test_unverified_owner_cannot_start_python_check_or_reuse_n8n_authority(
    app_client, pg_db, n8n_stub, integrations, python_provider
):
    row = owner(pg_db)
    prior = postgres.start_integration_attempt(row["record_id"], sources=("ebas",))
    details = repeat(row)
    details.update(email="other@example.invalid", telephone="+46707654321")
    response = app_client.post("/api/checkin/orchestrate", json=details)
    assert response.status_code == 200
    assert response.json()["ownership"] == "unverified"
    assert response.json()["integrations"]["ebas"] == "skipped"
    assert python_provider.calls == []
    assert "personnummer" not in n8n_stub.calls[0]["json"]
    assert pg_db.checkins()[0]["last_ebas_attempt_id"] == prior
    assert integrations["callbacks"]["ebas"]["state"] == "stale_rejected"


def test_without_pnr_ebas_is_skipped_even_if_n8n_claims_verified_membership(
    app_client, pg_db, integrations, python_provider
):
    row = owner(pg_db)
    details = repeat(row)
    details.pop("personnummer")
    response = app_client.post("/api/checkin/orchestrate", json=details)
    assert (
        response.status_code == 200
        and response.json()["integrations"]["ebas"] == "skipped"
    )
    assert response.json()["member"] is None and python_provider.calls == []
    assert pg_db.checkins()[0]["member"] is False
    assert pg_db.checkins()[0]["last_ebas_attempt_id"] is None


def test_engine_none_runs_neither_python_ebas_nor_n8n(
    app_client, pg_db, backend_app, n8n_stub, python_provider
):
    row = owner(pg_db)
    response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
    assert response.status_code == 200
    assert response.json()["integrations"] == {"ebas": "skipped", "startgg": "skipped"}
    assert response.json()["degraded"] is True
    assert python_provider.calls == n8n_stub.calls == []
    assert pg_db.checkins()[0]["last_ebas_attempt_id"] is None


@pytest.mark.parametrize(
    "source, initial, provider_value", [("to", False, True), ("v2", True, False)]
)
def test_python_check_preserves_to_and_v2_provenance(
    app_client, pg_db, integrations, python_provider, source, initial, provider_value
):
    row = owner(pg_db, member=initial, source=source)
    python_provider.outcome = provider_value
    response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
    assert (
        response.status_code == 200 and response.json()["integrations"]["ebas"] == "ok"
    )
    stored = pg_db.checkins()[0]
    assert stored["member"] is initial and stored["member_source"] == source


def test_stale_python_result_is_rejected_without_checkin_changes(
    app_client, pg_db, n8n_stub, integrations, python_provider
):
    row = owner(pg_db, member=True)
    python_provider.outcome = False
    python_provider.release.clear()
    snapshots = []

    async def newer_run(request):
        await integrations["run_callbacks"](request)
        assert await asyncio.to_thread(python_provider.entered.wait, 3)
        await run_in_threadpool(
            postgres.start_integration_attempt, row["record_id"], sources=("ebas",)
        )
        snapshots.extend(await run_in_threadpool(pg_db.checkins))
        python_provider.release.set()

    n8n_stub.on_call = newer_run
    try:
        response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
    finally:
        python_provider.release.set()
    assert (
        response.status_code == 200
        and response.json()["integrations"]["ebas"] == "unavailable"
    )
    assert pg_db.checkins() == snapshots
    with pg_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_log WHERE action = 'stale_rejected'"
            ).fetchone()[0]
            >= 1
        )


def test_slow_python_check_does_not_extend_current_orchestrate_budget(
    app_client, pg_db, backend_app, integrations, python_provider, monkeypatch
):
    row = owner(pg_db)
    python_provider.release.clear()
    monkeypatch.setattr(backend_app.checkin, "EBAS_REGISTER_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    try:
        response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
        assert time.monotonic() - started < 1.5
        assert (
            response.status_code == 200
            and response.json()["integrations"]["ebas"] == "unavailable"
        )
        assert python_provider.entered.is_set()
        assert python_provider.calls[0]["timeout"]["read"] <= 0.1
    finally:
        python_provider.release.set()
    deadline = time.monotonic() + 3
    while membership_check._checks_in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not membership_check._checks_in_flight
    assert pg_db.checkins()[0]["member"] is True


def test_default_stub_check_never_calls_external_transport(
    app_client, pg_db, integrations, monkeypatch, n8n_stub
):
    monkeypatch.delenv("EBAS_TRANSPORT", raising=False)
    monkeypatch.setenv("EBAS_API_KEY", "synthetic-configured-but-unused-key")
    transport = Mock(side_effect=AssertionError("External transport must stay blocked"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    row = owner(pg_db)
    response = app_client.post("/api/checkin/orchestrate", json=repeat(row))
    assert (
        response.status_code == 200 and response.json()["integrations"]["ebas"] == "ok"
    )
    assert pg_db.checkins()[0]["member"] is True
    assert len(n8n_stub.calls) == 1
    transport.assert_not_called()

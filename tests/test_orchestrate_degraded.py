"""Provider outages must never turn an unknown membership into a signup instruction."""

import httpx
import pytest

from shared import membership_check
from shared.ebas_client import ConfirmResult, EbasConfig
from tests.fixtures import make_checkin

pytestmark = pytest.mark.db
PERSONNUMMER = "200001010016"  # Invented test input; every upstream transport is stubbed.


@pytest.fixture
def n8n_engine(backend_app, monkeypatch):
    monkeypatch.setattr(backend_app.checkin, "INTEGRATION_ENGINE", "n8n")

    class PythonCheck:
        config = EbasConfig("synthetic-native-check-key")
        found = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def confirm_membership(self, pnr, year):
            return ConfirmResult(self.found)

    check = PythonCheck()
    monkeypatch.setattr(membership_check, "create_ebas_client", lambda: check)
    return check


def _assert_no_membership_hint(body):
    assert body["member"] is not False
    assert "Membership" not in body["missing"]


def _callback_results(n8n_stub, backend_app, results, python_check):
    python_check.found = results.get("ebas")

    async def callbacks(request):
        checkin_id = n8n_stub.calls[-1]["json"]["checkin_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app.app),
            base_url="http://testserver",
        ) as client:
            for source, value in results.items():
                if source == "ebas":
                    continue  # Native confirmation owns eBas; n8n can report Start.gg only.
                key = "registered"
                response = await client.post(
                    "/api/integration/result",
                    json={
                        "checkin_id": checkin_id,
                        "attempt_id": n8n_stub.calls[-1]["json"].get("attempt_id"),
                        "source": source,
                        "ok": True,
                        "data": {key: value},
                    },
                )
                response.raise_for_status()

    n8n_stub.on_call = callbacks


def test_disabled_engine_is_explicitly_degraded(app_client, pg_db, n8n_stub):
    body = app_client.post("/api/checkin/orchestrate", json=make_checkin()).json()

    assert body["integrations"] == {"ebas": "skipped", "startgg": "skipped"}
    assert body["degraded"] is True
    assert body["ready"] is False
    assert body["member"] is None
    _assert_no_membership_hint(body)
    assert pg_db.checkins()[0]["member"] is False
    assert pg_db.checkins()[0]["status"] == "Pending"
    assert n8n_stub.calls == []  # Disabled integrations must never reach a transport.


@pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_shape", "empty_summary"])
def test_failed_orchestrator_is_unavailable(
    app_client, pg_db, n8n_stub, n8n_engine, failure
):
    if failure == "timeout":

        def timeout(request):
            raise httpx.ReadTimeout("Synthetic timeout", request=request)

        n8n_stub.on_call = timeout
    elif failure == "http_error":
        n8n_stub.status_code = 503
    elif failure == "invalid_shape":
        n8n_stub.response = []

    response = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["integrations"] == {"ebas": "unavailable", "startgg": "unavailable"}
    assert body["degraded"] is True
    assert body["member"] is None
    _assert_no_membership_hint(body)
    assert pg_db.checkins()[0]["member"] is False
    assert pg_db.checkins()[0]["startgg"] is False
    assert body["status"] == pg_db.checkins()[0]["status"] == "Pending"
    assert len(n8n_stub.calls) == 1


def test_successful_callbacks_remain_ready(
    app_client, pg_db, n8n_stub, backend_app, n8n_engine
):
    n8n_stub.response = {"integrations": {"ebas": "ok", "startgg": "ok"}}
    _callback_results(
        n8n_stub, backend_app, {"ebas": True, "startgg": True}, n8n_engine
    )

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["integrations"] == {"ebas": "ok", "startgg": "ok"}
    assert body["degraded"] is False
    assert body["ready"] is True
    assert body["member"] is True
    assert body["missing"] == []
    assert pg_db.checkins()[0]["status"] == "Ready"


def test_confirmed_negative_membership_still_offers_signup(
    app_client, n8n_stub, backend_app, n8n_engine
):
    n8n_stub.response = {"integrations": {"ebas": "ok", "startgg": "ok"}}
    _callback_results(
        n8n_stub, backend_app, {"ebas": False, "startgg": True}, n8n_engine
    )

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["degraded"] is False
    assert body["member"] is False
    assert body["missing"] == ["Membership"]
    assert body["ready"] is False


@pytest.mark.parametrize(
    ("state", "member", "availability", "degraded"),
    [
        ("verified_true", True, "ok", False),
        ("verified_false", False, "ok", False),
        ("unavailable", None, "unavailable", True),
        ("skipped", None, "skipped", False),
        ("unexpected", None, "unavailable", True),
    ],
)
def test_explicit_provider_state_is_distinct_from_its_boolean_result(
    app_client, n8n_stub, backend_app, n8n_engine, state, member, availability, degraded
):
    n8n_stub.response = {
        "ebas": {"state": state},
        "startgg": {"state": "verified_true"},
    }
    results = {"startgg": True}
    if member is not None:
        results["ebas"] = member
    _callback_results(n8n_stub, backend_app, results, n8n_engine)

    body = app_client.post(
        "/api/checkin/orchestrate",
        json=make_checkin(personnummer=PERSONNUMMER if state != "skipped" else ""),
    ).json()

    assert body["integrations"] == {"ebas": availability, "startgg": "ok"}
    assert body["degraded"] is degraded
    assert body["member"] is member
    if member is None:
        _assert_no_membership_hint(body)
    else:
        assert ("Membership" in body["missing"]) is (member is False)


@pytest.mark.parametrize(
    ("ebas_state", "member", "availability", "degraded"),
    [
        ("verified_true", True, "ok", False),
        ("verified_false", False, "ok", False),
        ("unavailable", None, "unavailable", True),
        ("skipped", None, "skipped", False),
        ("unexpected", None, "unavailable", True),
        (None, None, "unavailable", True),
        ({"state": "verified_false"}, False, "ok", False),
    ],
)
def test_python_ebas_and_t028_startgg_results_override_other_reports(
    app_client,
    n8n_stub,
    backend_app,
    n8n_engine,
    ebas_state,
    member,
    availability,
    degraded,
):
    n8n_stub.response = {
        "integration_complete": True,
        "member": False,
        "startgg": True,
        "results": {
            "ebas": ebas_state,
            "startgg": "verified_true",
            "attempt_id": "synthetic-workflow-attempt",
        },
        "integrations": {"ebas": "ok", "startgg": "unavailable"},
    }
    results = {"startgg": True}
    if member is not None:
        results["ebas"] = member
    _callback_results(n8n_stub, backend_app, results, n8n_engine)

    body = app_client.post(
        "/api/checkin/orchestrate",
        json=make_checkin(personnummer=PERSONNUMMER if ebas_state != "skipped" else ""),
    ).json()

    assert body["integrations"] == {"ebas": availability, "startgg": "ok"}
    assert body["degraded"] is degraded
    assert body["member"] is member
    assert ("Membership" in body["missing"]) is (member is False)
    assert "attempt_id" not in body["integrations"]


def test_reviewer_t028_negative_membership_probe(app_client, n8n_stub, n8n_engine):
    n8n_engine.found = False
    n8n_stub.response = {
        "integration_complete": True,
        "member": False,
        "startgg": True,
        "results": {"ebas": "verified_false", "startgg": "verified_true"},
    }

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["integrations"] == {"ebas": "ok", "startgg": "ok"}
    assert body["degraded"] is False
    assert body["member"] is False
    assert "Membership" in body["missing"]


def test_one_unavailable_provider_keeps_the_other_result(
    app_client, pg_db, n8n_stub, backend_app, n8n_engine
):
    n8n_stub.response = {"integrations": {"ebas": "unavailable", "startgg": "ok"}}
    _callback_results(n8n_stub, backend_app, {"startgg": True}, n8n_engine)

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["integrations"] == {"ebas": "unavailable", "startgg": "ok"}
    assert body["degraded"] is True
    assert body["startgg"] is True
    _assert_no_membership_hint(body)
    assert pg_db.checkins()[0]["startgg"] is True
    assert pg_db.checkins()[0]["member"] is False
    assert pg_db.checkins()[0]["status"] == "Pending"


def test_startgg_outage_does_not_hide_a_confirmed_membership_requirement(
    app_client, n8n_stub, n8n_engine
):
    n8n_engine.found = False
    n8n_stub.response = {"integrations": {"ebas": "ok", "startgg": "unavailable"}}

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["degraded"] is True
    assert body["member"] is False
    assert "Membership" in body["missing"]


def test_outage_preserves_previously_verified_flags(app_client, pg_db, n8n_stub, n8n_engine):
    payload = make_checkin(member=True, startgg=True)
    pg_db.seed_checkin(**payload)
    n8n_stub.status_code = 503

    body = app_client.post("/api/checkin/orchestrate", json=payload).json()

    assert body["degraded"] is True
    assert body["member"] is True
    assert body["startgg"] is True
    assert body["ready"] is True
    assert body["missing"] == []
    assert pg_db.checkins()[0]["member"] is True
    assert pg_db.checkins()[0]["startgg"] is True
    assert pg_db.checkins()[0]["status"] == "Ready"


def test_legacy_startgg_summary_cannot_substitute_for_python_ebas_check(
    app_client, n8n_stub, n8n_engine
):
    n8n_stub.response = {"integration_complete": True, "member": True, "startgg": True}

    body = app_client.post("/api/checkin/orchestrate", json=make_checkin()).json()

    assert body["integrations"] == {"ebas": "skipped", "startgg": "ok"}
    assert body["degraded"] is False
    # Provider summary never overwrites the authoritative stored flags.
    assert body["member"] is None
    _assert_no_membership_hint(body)
    assert body["ready"] is False


def test_legacy_false_is_ambiguous_until_source_telemetry_exists(app_client, n8n_stub, n8n_engine):
    n8n_stub.response = {"integration_complete": True, "member": False, "startgg": True}

    body = app_client.post(
        "/api/checkin/orchestrate", json=make_checkin(personnummer=PERSONNUMMER)
    ).json()

    assert body["integrations"] == {"ebas": "unavailable", "startgg": "ok"}
    assert body["degraded"] is True
    _assert_no_membership_hint(body)


def test_missing_personnummer_is_skipped(app_client, n8n_stub, n8n_engine):
    n8n_stub.response = {"integration_complete": True, "member": False, "startgg": True}

    body = app_client.post("/api/checkin/orchestrate", json=make_checkin()).json()

    assert body["integrations"] == {"ebas": "skipped", "startgg": "ok"}
    assert body["degraded"] is False
    _assert_no_membership_hint(body)

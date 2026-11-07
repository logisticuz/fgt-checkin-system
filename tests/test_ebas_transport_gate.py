"""The application defaults to a local eBas transport, even when a provider key exists."""

import ast
import base64
import logging
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import aiohttp
import httpx
import membership_api as api
import pytest
import requests
import status_api
from fastapi.testclient import TestClient

from shared import (
    ebas_client,
    membership_crypto as crypto,
    membership_flow,
    membership_store as store,
    migrate,
)
from tests.test_membership_endpoints import direct_attempt, payload, wait_until

API_KEY = "synthetic-live-ebas-key-must-not-appear"
PNR = "200001010008"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_defaults(monkeypatch):
    monkeypatch.delenv("EBAS_TRANSPORT", raising=False)
    monkeypatch.delenv("EBAS_STUB_CONFIRM", raising=False)
    monkeypatch.delenv("IS_PROD", raising=False)
    monkeypatch.delenv("EBAS_ALLOW_LIVE_WITH_ENGINE_NONE", raising=False)
    monkeypatch.setenv("INTEGRATION_ENGINE", "none")
    monkeypatch.setenv("EBAS_API_KEY", API_KEY)
    monkeypatch.setenv("VERIFICATION_SENDER", "console")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv(
        "MEMBERSHIP_ENC_KEY", base64.b64encode(bytes(range(32))).decode("ascii")
    )
    monkeypatch.setattr(membership_flow, "_membership_flow", "v2")
    monkeypatch.setattr(api, "CLEANUP_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 3600)
    monkeypatch.setattr(status_api, "_TOKEN_KEY", b"synthetic-transport-gate-key")


@pytest.fixture
def no_outbound(monkeypatch):
    probes = []
    for owner, method in (
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
        (requests.sessions.Session, "send"),
        (aiohttp.ClientSession, "_request"),
    ):
        probe = Mock(side_effect=AssertionError("Unexpected external transport"))
        monkeypatch.setattr(owner, method, probe)
        probes.append(probe)
    yield
    for probe in probes:
        probe.assert_not_called()


@pytest.fixture
def gate_db(pg_db):
    migrate.apply(pg_db.pool)
    pg_db.seed_settings(require_startgg=False, require_payment=False)
    return pg_db


def test_default_factory_uses_local_stub_and_ignores_provider_key(
    no_outbound, monkeypatch
):
    constructor = Mock(
        side_effect=AssertionError("Stub must not construct HTTPTransport")
    )
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", constructor)
    with ebas_client.create_ebas_client() as client:
        assert isinstance(client._client._transport, ebas_client.StubEbasTransport)
        assert client.config.api_key == "stub"
        member = ebas_client.MemberInput(PNR, "player@example.invalid", "+46701234567")
        assert client.validate_member(member).ok is True
        assert (
            client.submit_member_with_lookup(member, date(2026, 11, 7)).state
            == "submitted"
        )
        assert client.confirm_membership(PNR, 2026) == ebas_client.ConfirmResult(
            None, error_code="stub_transport"
        )
    assert client._client.is_closed
    constructor.assert_not_called()


@pytest.mark.db
def test_default_stub_start_verify_and_status_make_zero_outbound_calls(
    backend_app, gate_db, no_outbound, monkeypatch
):
    monkeypatch.setattr(crypto, "generate_code", lambda: "123456")
    actions = []
    original = ebas_client.StubEbasTransport.handle_request

    def observe(transport, request):
        actions.append(request.url.path.rsplit("/", 1)[-1])
        return original(transport, request)

    monkeypatch.setattr(ebas_client.StubEbasTransport, "handle_request", observe)
    details = payload(gate_db)
    with TestClient(backend_app.app) as client:
        started = client.post("/api/membership/start", json=details)
        assert started.status_code == 200 and started.json()["state"] == "code_sent"
        attempt_id = started.json()["attempt_id"]
        verified = client.post(
            "/api/membership/verify", json={"attempt_id": attempt_id, "code": "123456"}
        )
        assert verified.status_code == 200 and verified.json()["state"] == "submitted"
        assert (
            client.get(f"/api/membership/{attempt_id}/status").json()["state"]
            == "submitted"
        )
    assert actions == ["validate_members.json", "submit_member_with_lookup.json"]
    rows = {row["record_id"]: row for row in gate_db.checkins()}
    assert rows[details["checkin_id"]]["member"] is True


@pytest.mark.db
def test_explicit_stub_found_status_reconcile_makes_zero_outbound_calls(
    backend_app, gate_db, no_outbound, monkeypatch
):
    monkeypatch.setenv("EBAS_STUB_CONFIRM", "found")
    attempt_id, details = direct_attempt(gate_db, submitting=True)
    with TestClient(backend_app.app) as client:
        response = client.get(f"/api/membership/{attempt_id}/status")
        assert response.status_code == 200 and response.json()["state"] == "submitted"
    rows = {row["record_id"]: row for row in gate_db.checkins()}
    assert rows[details["checkin_id"]]["member"] is True


@pytest.mark.db
@pytest.mark.parametrize("flow", ["v2", "legacy", "off"])
def test_explicit_stub_found_background_reconcile_is_also_offline(
    backend_app, gate_db, no_outbound, monkeypatch, flow
):
    monkeypatch.setenv("EBAS_STUB_CONFIRM", "found")
    attempt_id, details = direct_attempt(gate_db, submitting=True)
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    monkeypatch.setattr(api, "BACKGROUND_RECONCILE_TICK_SECONDS", 0.01)
    with TestClient(backend_app.app):
        wait_until(lambda: store.get_attempt(attempt_id).state == "submitted")
    assert gate_db.checkins()[0]["record_id"] == details["checkin_id"]
    assert gate_db.checkins()[0]["member"] is True


def test_live_with_engine_none_refuses_actual_application_startup(
    backend_app, no_outbound, monkeypatch
):
    monkeypatch.setenv("EBAS_TRANSPORT", "live")
    with pytest.raises(
        ValueError, match="EBAS_TRANSPORT=live.*INTEGRATION_ENGINE=none"
    ):
        with TestClient(backend_app.app):
            pytest.fail("Contradictory configuration reached a running application")


@pytest.mark.parametrize("override", [False, True])
def test_explicit_live_mode_uses_real_transport_class_mocked_at_boundary(
    monkeypatch, override
):
    monkeypatch.setenv("EBAS_TRANSPORT", "live")
    if override:
        monkeypatch.setenv("EBAS_ALLOW_LIVE_WITH_ENGINE_NONE", "1")
    else:
        monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    calls = []

    def mocked_provider(transport, request):
        calls.append(request)
        return httpx.Response(
            200, json={"response": {"member_found": True}}, request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", mocked_provider)
    with ebas_client.create_ebas_client() as client:
        assert isinstance(client._client._transport, httpx.HTTPTransport)
        assert client.config.api_key == API_KEY
        assert client.confirm_membership(PNR, 2026).found is True
    assert len(calls) == 1 and calls[0].url.host == "ebas.sverok.se"


@pytest.mark.parametrize("mode", ["stub", "live"])
def test_startup_logs_selected_transport_once_without_key(
    backend_app, no_outbound, monkeypatch, caplog, mode
):
    if mode == "live":
        monkeypatch.setenv("EBAS_TRANSPORT", "live")
        monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    with caplog.at_level(logging.INFO):
        with TestClient(backend_app.app):
            pass
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("eBas transport:")
    ]
    assert messages == [f"eBas transport: {mode}"]
    assert API_KEY not in caplog.text


def test_invalid_transport_fails_closed_without_echoing_its_value(
    backend_app, monkeypatch, caplog
):
    monkeypatch.setenv("EBAS_TRANSPORT", "invalid-" + API_KEY)
    with pytest.raises(
        ValueError, match="EBAS_TRANSPORT must be stub or live"
    ) as error:
        with TestClient(backend_app.app):
            pass
    assert API_KEY not in str(error.value) + caplog.text


@pytest.mark.parametrize("outcome,found", [("found", True), ("not_found", False)])
def test_stub_confirmation_requires_explicit_nonproduction_opt_in(
    no_outbound, monkeypatch, outcome, found
):
    monkeypatch.setenv("EBAS_STUB_CONFIRM", outcome)
    with ebas_client.create_ebas_client() as client:
        assert client.confirm_membership(PNR, 2026) == ebas_client.ConfirmResult(found)


@pytest.mark.parametrize("variable,value", [("IS_PROD", "true"), ("ENV", "prod"), ("APP_ENV", "production")])
def test_production_stub_logs_one_startup_error_and_ignores_positive_opt_in(
    backend_app, no_outbound, monkeypatch, caplog, variable, value
):
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("EBAS_STUB_CONFIRM", "found")
    with caplog.at_level(logging.ERROR):
        with TestClient(backend_app.app):
            for _ in range(2):
                with ebas_client.create_ebas_client() as client:
                    assert client.confirm_membership(
                        PNR, 2026
                    ) == ebas_client.ConfirmResult(None, error_code="stub_transport")
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and "EBAS STUB IN PRODUCTION" in errors[0].getMessage()
    assert PNR not in caplog.text and API_KEY not in caplog.text


def test_only_the_factory_constructs_production_ebas_clients():
    factory_path = ROOT / "shared" / "ebas_client.py"
    factory = next(
        node
        for node in ast.parse(factory_path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "create_ebas_client"
    )
    allowed_lines = {
        node.lineno for node in ast.walk(factory) if isinstance(node, ast.Call)
    }
    calls = []
    for directory in ("backend", "shared", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "EbasClient"
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "EbasClient"
                ):
                    calls.append((path, node.lineno))
    assert calls and all(
        path == factory_path and line in allowed_lines for path, line in calls
    )

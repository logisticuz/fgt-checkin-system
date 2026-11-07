"""Integration boundaries use only synthetic data, private Postgres and in-memory providers."""

import asyncio
import json
import logging
import uuid
from unittest.mock import Mock

import aiohttp
import httpx
import pytest
import requests
import status_api
from psycopg.types.json import Jsonb

from tests.fixtures import SYNTHETIC_PERSONNUMMER, make_checkin


@pytest.fixture(autouse=True)
def synthetic_capability_key(monkeypatch):
    monkeypatch.setattr(
        status_api, "_TOKEN_KEY", b"synthetic-integration-fixes-signing-key"
    )


@pytest.mark.db
def test_disabled_engine_makes_no_provider_calls(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch
):
    monkeypatch.setattr(
        backend_app.ebas_legacy, "get_membership_flow", lambda: "legacy"
    )
    transports = []
    for owner, method in (
        (requests.sessions.Session, "send"),
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
        (aiohttp.ClientSession, "_request"),
    ):
        transport = Mock(
            side_effect=AssertionError("Disabled engine attempted provider transport")
        )
        monkeypatch.setattr(owner, method, transport)
        transports.append(transport)

    def forbidden(request):
        raise AssertionError("Disabled engine reached the n8n stub")

    n8n_stub.on_call = forbidden
    # This invented, check-digit-valid input is used by the existing validation tests.
    payload = make_checkin(personnummer="200001010016")
    response = app_client.post("/api/checkin/orchestrate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["integrations"] == {"ebas": "skipped", "startgg": "skipped"}
    assert body["degraded"] is True and body["member"] is None
    assert body["ready"] is False and "Membership" not in body["missing"]
    record_id = body["checkin_id"]
    for route in ("recheck-startgg", "bulk-recheck-startgg"):
        response = app_client.post(f"/api/admin/{route}", json={"record_id": record_id})
        assert response.status_code == 503
        assert response.json()["detail"]["state"] == "skipped"
    response = app_client.post(
        "/api/ebas/register", json={**payload, "slug": payload["event_slug"]}
    )
    assert response.status_code == 503 and response.json()["state"] == "skipped"
    assert n8n_stub.calls == []
    for transport in transports:
        transport.assert_not_called()
    stored = pg_db.checkins()[0]
    assert stored["member"] is False and stored["startgg"] is False


def test_queued_legacy_worker_rechecks_engine_before_transport(
    backend_app, n8n_stub, monkeypatch
):
    legacy = backend_app.ebas_legacy
    monkeypatch.setattr(legacy, "_ebas_registrations", {"synthetic-checkin": 0})
    monkeypatch.setattr(legacy, "_ebas_registration_lock", asyncio.Lock())
    result = asyncio.run(
        legacy._submit_legacy_ebas("synthetic-checkin", 0, {"personnummer": "0" * 12})
    )
    assert result.status_code == 503 and json.loads(result.body)["state"] == "skipped"
    assert n8n_stub.calls == [] and legacy._ebas_registrations == {}


@pytest.mark.db
@pytest.mark.parametrize("path", ["orchestrate", "legacy"])
@pytest.mark.parametrize("failure", ["http", "transport"])
def test_provider_error_logs_never_include_body_or_contact_sentinels(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, caplog, path, failure
):
    monkeypatch.setattr(backend_app.checkin, "INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(backend_app.ebas_legacy, "INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(
        backend_app.ebas_legacy, "get_membership_flow", lambda: "legacy"
    )
    sentinels = [
        "0" * 12,
        "+46 (70) 135-79-24",
        "provider-private@example.invalid",
        "Provider Private Synthetic Name",
    ]
    private_body = " | ".join(sentinels)
    if failure == "http":
        n8n_stub.status_code = 502
        n8n_stub.response = {"message": private_body}
    else:

        def unavailable(request):
            raise httpx.ConnectError(private_body, request=request)

        n8n_stub.on_call = unavailable
    transport = backend_app.checkin.httpx_client._transport
    original_handler = transport.handler
    transport_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    original_levels = [item.level for item in transport_loggers]
    original_filters = [list(item.filters) for item in transport_loggers]

    async def provider_diagnostics(request):
        logging.getLogger(f"httpcore.n8n_{path}_diagnostic").warning(
            "Provider metadata: %s", private_body
        )
        logging.getLogger("unrelated.application").info(
            "Unrelated application log preserved"
        )
        response = await original_handler(request)
        response.extensions["reason_phrase"] = private_body.encode("ascii")
        return response

    monkeypatch.setattr(transport, "handler", provider_diagnostics)
    caplog.set_level(logging.INFO)
    if path == "orchestrate":
        response = app_client.post("/api/checkin/orchestrate", json=make_checkin())
        assert response.status_code == 200 and response.json()["degraded"] is True
    else:
        row = pg_db.seed_checkin()
        from status_api import capability_token

        response = app_client.post(
            "/api/ebas/register",
            json={
                "checkin_id": row["record_id"],
                "status_token": capability_token(row["record_id"]),
                "tag": row["tag"],
                "slug": row["event_slug"],
                "personnummer": SYNTHETIC_PERSONNUMMER,
            },
        )
        assert response.status_code == 202
        assert response.json()["state"] == "outcome_unknown"
        assert response.json()["retry_allowed"] is False
    assert len(n8n_stub.calls) == 1
    for sentinel in sentinels:
        assert sentinel not in caplog.text and sentinel not in response.text
    errors = [
        record.getMessage()
        for record in caplog.records
        if "integration_error code=" in record.getMessage()
    ]
    assert len(errors) == 1
    assert errors[0].endswith("status=502" if failure == "http" else "status=none")
    assert "Unrelated application log preserved" in caplog.messages
    assert [item.level for item in transport_loggers] == original_levels
    assert [item.filters for item in transport_loggers] == original_filters
    logging.getLogger("httpx").info("Post-provider transport log preserved")
    assert "Post-provider transport log preserved" in caplog.messages


@pytest.mark.db
@pytest.mark.parametrize(
    "route",
    [
        "/",
        "/kiosk",
        "/register",
        "/register.html",
        "/status/Public%20synthetic%20player",
    ],
)
@pytest.mark.parametrize("ready", [False, True])
def test_public_page_contexts_and_html_never_receive_server_tokens(
    app_client, pg_db, backend_app, monkeypatch, route, ready
):
    tokens = {
        "N8N_WEBHOOK_TOKEN": "private-webhook-sentinel",
        "SSE_TOKEN": "private-sse-sentinel",
    }
    for module in (backend_app, backend_app.app_state, backend_app.pages):
        for key, value in tokens.items():
            monkeypatch.setattr(module, key, value, raising=False)
    monkeypatch.setattr(backend_app.pages, "get_tournament_events", lambda slug: [])
    pg_db.seed_checkin(name="Public synthetic player", member=ready, startgg=ready)
    response = app_client.get(route)
    assert response.status_code == 200
    assert "n8n_token" not in response.context and "sse_token" not in response.context
    for token in tokens.values():
        assert token not in response.text and token not in str(response.context)


@pytest.mark.parametrize(
    "params",
    [
        {"t": "invalid"},
        {"t": "", "checkin_id": ""},
        {"checkin_id": "synthetic-checkin"},
        {"checkin_id": "synthetic-checkin", "t": "invalid"},
    ],
)
def test_invalid_capability_page_is_static_forbidden_without_any_storage_lookup(
    handler_client, backend_app, monkeypatch, params
):
    forbidden = Mock(
        side_effect=AssertionError("Invalid capability reached participant storage")
    )
    for helper in (
        "check_participant_status",
        "get_participant_details",
        "lookup_checkin_status",
        "get_active_settings",
    ):
        monkeypatch.setattr(backend_app.pages, helper, forbidden)
    response = handler_client.get("/status/Private%20URL%20label", params=params)
    assert response.status_code == 403
    assert "expired" in response.text and "ask a TO" in response.text
    assert "<script" not in response.text and "Private URL label" not in response.text
    forbidden.assert_not_called()


@pytest.mark.db
@pytest.mark.parametrize("kind", ["missing", "foreign", "expired", "tampered", "valid"])
def test_games_mutation_requires_capability_for_its_own_row(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, kind
):
    row = pg_db.seed_checkin()
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE settings SET default_game = %s", (["Synthetic game"],)
        )
    payload = {"checkin_id": row["record_id"], "games": ["Synthetic game"]}
    if kind == "foreign":
        payload["status_token"] = status_api.capability_token(str(uuid.uuid4()))
    elif kind == "expired":
        payload["status_token"] = status_api.capability_token(row["record_id"], ttl=-1)
    elif kind == "tampered":
        payload["status_token"] = "invalid-token"
    elif kind == "valid":
        payload["status_token"] = status_api.capability_token(row["record_id"])
    lookup = Mock(wraps=backend_app.checkin.get_checkin_by_record_id)
    monkeypatch.setattr(backend_app.checkin, "get_checkin_by_record_id", lookup)
    response = app_client.patch("/api/player/games", json=payload)
    stored = pg_db.checkins()[0]
    if kind == "valid":
        assert response.status_code == 200
        assert stored["tournament_games_registered"] == ["Synthetic game"]
        assert stored["payment_expected"] == 25
        lookup.assert_called_once_with(row["record_id"])
    else:
        assert response.status_code == 403
        assert (
            stored["tournament_games_registered"] == []
            and stored["payment_expected"] == 0
        )
        lookup.assert_not_called()
    assert n8n_stub.calls == []


@pytest.mark.db
@pytest.mark.parametrize("route", ["/register", "/register.html", "/api/player/games"])
@pytest.mark.parametrize("source", ["settings", "fresh-cache", "expired-cache"])
def test_disabled_engine_game_entry_points_use_only_cache_or_settings(
    app_client, pg_db, backend_app, n8n_stub, monkeypatch, route, source
):
    state = backend_app.app_state
    row = pg_db.seed_checkin()
    local = {"id": "local-event", "name": "Local synthetic game"}
    cached = {"id": "cached-event", "name": "Cached synthetic game"}
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE settings SET default_game = NULL, events_json = %s",
            (Jsonb([local]),),
        )
    monkeypatch.setattr(state, "STARTGG_API_KEY", "synthetic-configured-startgg-key")
    cache = {}
    expected = local
    if source != "settings":
        timestamp = state.time.time()
        if source == "expired-cache":
            timestamp -= state.STARTGG_CACHE_TTL + 1
        else:
            expected = cached
        cache[row["event_slug"]] = ([cached], timestamp)
    monkeypatch.setattr(state, "STARTGG_CACHE", cache)
    provider = Mock(
        side_effect=AssertionError("Disabled engine attempted Start.gg transport")
    )
    monkeypatch.setattr(state.SESSION, "post", provider)
    if route == "/api/player/games":
        response = app_client.patch(
            route,
            json={
                "checkin_id": row["record_id"],
                "status_token": status_api.capability_token(row["record_id"]),
                "games": [expected["name"]],
            },
        )
        assert response.status_code == 200
        assert pg_db.checkins()[0]["tournament_games_registered"] == [expected["name"]]
    else:
        response = app_client.get(route)
        assert response.status_code == 200 and response.context["games"] == [expected]
    provider.assert_not_called()
    assert n8n_stub.calls == []


def test_enabled_engine_still_fetches_and_caches_tournament_games(
    backend_app, monkeypatch
):
    state = backend_app.app_state
    monkeypatch.setattr(state, "INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(state, "STARTGG_API_KEY", "synthetic-configured-startgg-key")
    monkeypatch.setattr(state, "STARTGG_CACHE", {})
    response = Mock()
    response.json.return_value = {
        "data": {
            "tournament": {
                "events": [{"id": "synthetic-game", "name": "Provider synthetic game"}]
            }
        }
    }
    provider = Mock(return_value=response)
    monkeypatch.setattr(state.SESSION, "post", provider)
    for _ in range(2):
        assert state.get_tournament_events("synthetic-event") == [
            {"id": "synthetic-game", "name": "Provider synthetic game"}
        ]
    provider.assert_called_once()

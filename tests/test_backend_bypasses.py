"""Removed backend bypasses and compatible, logging-only internal authentication."""

import logging

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import make_checkin

INTERNAL_KINDS = ["begin", "integration", "member", "notify-checkin", "notify-update"]
SYNTHETIC_TOKEN = "synthetic-internal-token"
PRIVATE_MARKER = "synthetic-private-body-marker"


def internal_call(client, pg_db, kind, headers=None):
    if kind == "begin":
        path = "/api/checkin/begin"
        payload = {"event_slug": "test-event", "payload": make_checkin()}
    elif kind == "integration":
        row = pg_db.seed_checkin()
        path = "/api/integration/result"
        payload = {
            "checkin_id": row["record_id"],
            "source": "ebas",
            "ok": True,
            "state": "verified_true",
            "data": {"member": True},
        }
    elif kind == "member":
        row = pg_db.seed_checkin()
        path = f"/api/checkin/{row['record_id']}/member-status"
        payload = {"member": True, "ok": True, "state": "verified_true"}
    else:
        path = "/api/notify/" + ("checkin" if kind == "notify-checkin" else "update")
        payload = {"name": "Synthetic Test Player"}
    payload["private_marker"] = PRIVATE_MARKER
    response = client.post(path, json=payload, headers=headers)
    return response, path


def backend_warnings(caplog, backend_app):
    return [
        record
        for record in caplog.records
        if record.name == backend_app.logger.name and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"]
)
def test_catch_all_proxy_is_removed(handler_client, n8n_stub, method):
    response = handler_client.request(method, "/n8n/anything")
    assert response.status_code in {404, 405}
    assert n8n_stub.calls == []


def test_public_member_patch_is_removed(handler_client):
    response = handler_client.patch(
        "/api/player/member", json={"tag": "synthetic-tag", "slug": "test-event", "member": True}
    )
    assert response.status_code in {404, 405}


@pytest.mark.parametrize("path", ["/login", "/auth/callback"])
def test_unused_backend_oauth_is_removed(handler_client, path):
    response = handler_client.get(path, params={"admin_key": "synthetic-key", "code": "test-code"})
    assert response.status_code == 404


@pytest.mark.db
@pytest.mark.parametrize("kind", INTERNAL_KINDS)
@pytest.mark.parametrize(
    "supplied", [None, "incorrect-token", b"\xff"], ids=["missing", "wrong", "non-ascii"]
)
def test_missing_or_invalid_internal_token_warns_but_still_succeeds(
    app_client, pg_db, backend_app, monkeypatch, caplog, kind, supplied
):
    monkeypatch.setattr(backend_app, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    headers = {"X-Internal-Token": supplied} if supplied is not None else None
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=backend_app.logger.name):
        response, path = internal_call(app_client, pg_db, kind, headers)
    assert response.status_code == 200
    assert response.json()["success"] is True
    warnings = backend_warnings(caplog, backend_app)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"path={path}" in message
    assert "client=testclient" in message
    assert PRIVATE_MARKER not in message
    assert SYNTHETIC_TOKEN not in message
    assert "incorrect-token" not in message


@pytest.mark.db
@pytest.mark.parametrize("kind", INTERNAL_KINDS)
def test_correct_internal_token_succeeds_without_warning(
    app_client, pg_db, backend_app, monkeypatch, caplog, kind
):
    monkeypatch.setattr(backend_app, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=backend_app.logger.name):
        response, _ = internal_call(app_client, pg_db, kind, {"X-Internal-Token": SYNTHETIC_TOKEN})
    assert response.status_code == 200
    assert backend_warnings(caplog, backend_app) == []


@pytest.mark.db
def test_unconfigured_token_warns_once_at_startup_and_skips_request_warnings(
    pg_db, backend_app, monkeypatch, caplog
):
    pg_db.seed_settings()
    monkeypatch.setattr(backend_app, "INTERNAL_API_TOKEN", None)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_API_TOKEN", None)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=backend_app.logger.name):
        with TestClient(backend_app.app) as client:
            for kind in INTERNAL_KINDS:
                response, _ = internal_call(client, pg_db, kind)
                assert response.status_code == 200
    warnings = backend_warnings(caplog, backend_app)
    assert len(warnings) == 1
    assert "INTERNAL_API_TOKEN is unset" in warnings[0].getMessage()


def test_configured_token_has_no_startup_warning(backend_app, monkeypatch, caplog):
    monkeypatch.setattr(backend_app, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_API_TOKEN", SYNTHETIC_TOKEN)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=backend_app.logger.name):
        with TestClient(backend_app.app):
            pass
    assert backend_warnings(caplog, backend_app) == []

"""Stage internal callback authentication without weakening public begin ownership."""

import asyncio
import importlib.util
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import make_checkin
from tests.test_route_inventory import route_contexts

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-private-internal-mode-token"
BODY_MARKER = "synthetic-private-internal-body"
WRONG_TOKEN = "synthetic-wrong-internal-mode-token"
INTERNAL_PATHS = {
    "/api/checkin/begin",
    "/api/integration/result",
    "/api/checkin/{checkin_id}/member-status",
    "/api/notify/checkin",
    "/api/notify/update",
}


def policy(monkeypatch, backend, mode, token=TOKEN):
    monkeypatch.setattr(backend.app_state, "INTERNAL_AUTH_MODE", mode)
    for module in (backend, backend.app_state, backend.checkin):
        monkeypatch.setattr(module, "INTERNAL_API_TOKEN", token)


def request_payload(database, path):
    if path == "/api/checkin/begin":
        return path, {"event_slug": "test-event", "payload": make_checkin()}
    if path in {"/api/integration/result", "/api/checkin/{checkin_id}/member-status"}:
        row = database.seed_checkin(member=False, member_source="ebas_check")
        if path == "/api/integration/result":
            return path, {
                "checkin_id": row["record_id"],
                "source": "ebas",
                "ok": True,
                "state": "verified_true",
                "data": {"member": True},
            }
        return path.format(checkin_id=row["record_id"]), {
            "member": True,
            "ok": True,
            "state": "verified_true",
        }
    return path, {"name": "Synthetic Test Player"}


def warnings(caplog, backend):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == backend.logger.name and record.levelno == logging.WARNING
    ]


def test_shared_dependency_covers_exactly_the_internal_route_inventory(backend_app):
    inventory = json.loads(
        (ROOT / "tests/fixtures/route_inventory.json").read_text(encoding="utf-8")
    )
    expected = {row[0] for row in inventory if "require_internal_soft" in row[3]}
    assert expected == INTERNAL_PATHS
    actual = {
        getattr(context, "path", None) or route.path
        for route, context in route_contexts(backend_app.app)
        if any(
            dependency.call is backend_app.app_state.require_internal_soft
            for dependency in getattr(getattr(context, "dependant", None), "dependencies", [])
        )
    }
    assert actual == expected


@pytest.mark.db
@pytest.mark.parametrize("path", sorted(INTERNAL_PATHS))
@pytest.mark.parametrize("mode", ["soft", "enforce"])
@pytest.mark.parametrize(
    "supplied", [None, WRONG_TOKEN, TOKEN], ids=["missing", "wrong", "correct"]
)
def test_every_internal_endpoint_obeys_the_shared_policy(
    pg_db, backend_app, n8n_stub, monkeypatch, caplog, path, mode, supplied
):
    pg_db.seed_settings()
    policy(monkeypatch, backend_app, mode)
    path, body = request_payload(pg_db, path)
    body["private_marker"] = BODY_MARKER
    before = pg_db.checkins()
    headers = {} if supplied is None else {"X-Internal-Token": supplied}
    with TestClient(backend_app.app) as client:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=backend_app.logger.name):
            response = client.post(path + "?private=" + BODY_MARKER, json=body, headers=headers)
    rejected = mode == "enforce" and supplied != TOKEN
    if rejected:
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}
        assert pg_db.checkins() == before
        reason = "missing_token" if supplied is None else "invalid_token"
        assert warnings(caplog, backend_app) == [
            f"Internal request rejected: path={path} reason={reason}"
        ]
    else:
        assert response.status_code == 200
        assert response.json()["success"] is True
        messages = warnings(caplog, backend_app)
        if supplied != TOKEN:
            assert len(messages) == 1
            assert messages[0] == (
                f"Internal request has a missing or invalid token: path={path} client=testclient"
            )
        else:
            assert messages == []
    assert n8n_stub.calls == []
    assert TOKEN not in caplog.text and WRONG_TOKEN not in caplog.text
    assert BODY_MARKER not in caplog.text
    assert TOKEN not in response.text and WRONG_TOKEN not in response.text


@pytest.mark.db
@pytest.mark.parametrize("path", sorted(INTERNAL_PATHS))
def test_non_ascii_token_is_rejected_without_echo(pg_db, backend_app, monkeypatch, caplog, path):
    pg_db.seed_settings()
    policy(monkeypatch, backend_app, "enforce")
    path, body = request_payload(pg_db, path)
    before = pg_db.checkins()
    with TestClient(backend_app.app) as client:
        caplog.clear()
        response = client.post(path, json=body, headers={"X-Internal-Token": b"\xff"})
    assert response.status_code == 401 and response.json() == {"detail": "Unauthorized"}
    assert pg_db.checkins() == before
    assert warnings(caplog, backend_app) == [
        f"Internal request rejected: path={path} reason=invalid_token"
    ]
    assert TOKEN not in caplog.text and "\xff" not in caplog.text


@pytest.mark.db
@pytest.mark.parametrize("supplied", [None, WRONG_TOKEN])
def test_soft_begin_still_uses_public_ownership_for_untrusted_callers(
    pg_db, backend_app, monkeypatch, supplied
):
    pg_db.seed_settings()
    row = pg_db.seed_checkin(
        name="Original Owner",
        email="owner@example.invalid",
        telephone="0700000000",
        member=True,
        startgg=True,
        payment_valid=True,
    )
    policy(monkeypatch, backend_app, "soft")
    attack = make_checkin(
        tag=row["tag"],
        name="Changed Contact",
        email="changed@example.invalid",
        telephone="0700000001",
    )
    headers = {} if supplied is None else {"X-Internal-Token": supplied}
    with TestClient(backend_app.app) as client:
        response = client.post(
            "/api/checkin/begin",
            json={"event_slug": row["event_slug"], "payload": attack},
            headers=headers,
        )
    assert response.status_code == 200 and response.json()["ownership_verified"] is False
    after = pg_db.checkins()[0]
    assert (after["name"], after["email"], after["telephone"]) == (
        row["name"],
        row["email"],
        row["telephone"],
    )
    assert after["member"] and after["startgg"] and after["payment_valid"]


@pytest.mark.parametrize("token", [None, "", "   "])
def test_enforce_without_a_nonempty_token_refuses_asgi_startup(
    backend_app, monkeypatch, caplog, token
):
    policy(monkeypatch, backend_app, "enforce", token)
    with pytest.raises(RuntimeError, match="enforce requires a non-empty INTERNAL_API_TOKEN"):
        with TestClient(backend_app.app):
            pytest.fail("Contradictory configuration reached a running application")
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("mode", ["soft", "enforce"])
def test_asgi_startup_logs_the_selected_mode_once_without_secrets(
    backend_app, monkeypatch, caplog, mode
):
    policy(monkeypatch, backend_app, mode)
    caplog.clear()
    with caplog.at_level(logging.INFO), TestClient(backend_app.app):
        pass
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("internal auth:")
    ]
    assert lines == [f"internal auth: {mode}"]
    assert TOKEN not in caplog.text


def test_invalid_mode_refuses_startup_without_echoing_configuration(
    backend_app, monkeypatch, caplog
):
    policy(monkeypatch, backend_app, BODY_MARKER)
    with pytest.raises(RuntimeError, match="INTERNAL_AUTH_MODE must be soft or enforce") as error:
        with TestClient(backend_app.app):
            pytest.fail("Invalid mode reached a running application")
    assert BODY_MARKER not in str(error.value) and BODY_MARKER not in caplog.text
    assert TOKEN not in caplog.text


def test_unset_mode_defaults_to_soft(backend_app, monkeypatch):
    monkeypatch.delenv("INTERNAL_AUTH_MODE", raising=False)
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    spec = importlib.util.spec_from_file_location(
        "synthetic_internal_mode_default", ROOT / "backend/app_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        assert module.INTERNAL_AUTH_MODE == "soft"
    finally:
        asyncio.run(module.httpx_client.aclose())
        module.SESSION.close()


@pytest.mark.parametrize("filename", [".env.example", "n8n/config/n8n.env.example"])
def test_env_examples_keep_only_required_workflow_access(filename):
    text = (ROOT / filename).read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.split("#", 1)[0].strip()
    assert not any(key.startswith(("AIRTABLE_", "N8N_BASIC_AUTH_")) for key in values)
    assert values["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert values["N8N_SECURE_COOKIE"] == "true"
    assert values["INTERNAL_API_TOKEN"] == ""
    references = set()
    for path in (ROOT / "n8n/flows").glob("*.json"):
        references.update(re.findall(r"\$env\.([A-Z][A-Z0-9_]*)", path.read_text(encoding="utf-8")))
    allowlist = set(values["N8N_ENV_ACCESS_ALLOWLIST"].split(","))
    assert allowlist == references
    assert "INTERNAL_API_TOKEN" in allowlist and "STARTGG_CLIENT_SECRET" not in allowlist

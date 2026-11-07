"""Public membership routes enforce the startup mode, including legacy rollback."""

import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from membership_api import BYLAWS_VERSION, PRIVACY_VERSION
from status_api import capability_token

from shared import membership_flow

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ("legacy", "v2", "off")
OFF_MESSAGE = "Membership registration is handled by a TO today. Please ask a TO."
V2_MESSAGE = (
    "Membership registration has moved. Please use the membership step on the check-in page."
)


@pytest.mark.db
@pytest.mark.parametrize("flow", FLOWS)
@pytest.mark.parametrize(
    "method, route",
    [
        ("POST", "/api/ebas/register"),
        ("POST", "/api/membership/start"),
        ("POST", "/api/membership/verify"),
        ("POST", "/api/membership/resend"),
        ("GET", "/member/confirm/test-attempt"),
    ],
)
def test_membership_mode_route_matrix(
    flow, method, route, app_client, pg_db, n8n_stub, monkeypatch, backend_app
):
    monkeypatch.setattr(backend_app.ebas_legacy, "INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    row = pg_db.seed_checkin()
    n8n_stub.response = {"success": True}
    payload = {
        "checkin_id": row["record_id"],
        "status_token": capability_token(row["record_id"]),
        "tag": row["tag"],
        "slug": row["event_slug"],
        "personnummer": "000000000000",
        "email": row["email"],
        "telephone": row["telephone"],
    }
    if route.startswith("/api/membership/"):
        payload = {
            "personnummer": "200001010008",
            "email": row["email"],
            "phone": "+46701234567",
            "channel": "sms",
            "accepted_bylaws": False,
            "accepted_privacy": True,
            "bylaws_version": BYLAWS_VERSION,
            "privacy_version": PRIVACY_VERSION,
            "checkin_id": row["record_id"],
        }
    response = app_client.request(method, route, json=payload if method == "POST" else None)
    is_legacy_endpoint = route == "/api/ebas/register"
    expected_status = {"legacy": 200, "v2": 410, "off": 503}[flow] if is_legacy_endpoint else 404
    if flow == "v2" and route.startswith("/api/membership/"):
        expected_status = 400
    assert response.status_code == expected_status
    assert len(n8n_stub.calls) == int(is_legacy_endpoint and flow == "legacy")
    if is_legacy_endpoint and flow != "legacy":
        assert response.json() == {
            "error_code": f"flow_{flow}",
            "user_message": V2_MESSAGE if flow == "v2" else OFF_MESSAGE,
        }


@pytest.mark.parametrize("flow, status", [("v2", 410), ("off", 503)])
def test_disabled_legacy_flow_rejects_before_body_or_storage(
    flow, status, handler_client, backend_app, n8n_stub, monkeypatch
):
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)

    def unexpected_storage(*args):
        raise AssertionError("A disabled legacy flow must not access storage")

    monkeypatch.setattr(backend_app.ebas_legacy, "get_checkin_by_tag", unexpected_storage)
    response = handler_client.post("/api/ebas/register", content="not JSON")
    assert response.status_code == status
    assert n8n_stub.calls == []


@pytest.mark.parametrize("flow", FLOWS)
def test_health_reports_membership_flow(flow, handler_client, monkeypatch):
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    response = handler_client.get("/health")
    assert response.status_code == 200
    assert response.json()["membership_flow"] == flow


class ElementsById(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.elements[attributes["id"]] = attributes


@pytest.mark.db
@pytest.mark.parametrize("flow", FLOWS)
@pytest.mark.parametrize("route", ["/", "/kiosk", "/register", "/register.html"])
def test_checkin_and_register_template_contexts(flow, route, app_client, monkeypatch):
    monkeypatch.setattr(membership_flow, "_membership_flow", flow)
    response = app_client.get(route)
    assert response.status_code == 200
    assert response.context["membership_flow"] == flow
    if route.startswith("/register"):
        elements = ElementsById(response.text).elements
        if flow == "off":
            assert "membership-flow-off-message" in elements
            assert elements["section-ebas"]["style"] == "display: none"
            assert "inert" in elements["section-ebas"]
            assert f'.textContent = "{OFF_MESSAGE}"' in response.text
        else:
            assert "membership-flow-off-message" not in elements
            assert "style" not in elements["section-ebas"]


def run_startup(code, flow=None, backend=False):
    environment = os.environ.copy()
    environment.pop("MEMBERSHIP_FLOW", None)
    if flow is not None:
        environment["MEMBERSHIP_FLOW"] = flow
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT / "backend" if backend else ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("flow", ["invalid", "", "LEGACY"])
def test_invalid_membership_flow_prevents_backend_startup(flow):
    result = run_startup("import backend.main", flow=flow, backend=True)
    assert result.returncode != 0
    assert "Invalid MEMBERSHIP_FLOW" in result.stderr


@pytest.mark.parametrize("flow", [None, *FLOWS])
def test_membership_flow_is_read_once_with_legacy_default(flow):
    code = (
        "import os; from shared.membership_flow import get_membership_flow; "
        f"assert get_membership_flow() == {flow or 'legacy'!r}; "
        "os.environ['MEMBERSHIP_FLOW'] = 'invalid'; "
        f"assert get_membership_flow() == {flow or 'legacy'!r}"
    )
    result = run_startup(code, flow=flow)
    assert result.returncode == 0, result.stderr


def test_v2_membership_start_is_available(handler_client, monkeypatch):
    monkeypatch.setattr(membership_flow, "_membership_flow", "v2")
    response = handler_client.post(
        "/api/membership/start",
        json={
            "personnummer": "200001010008",
            "email": "player@example.invalid",
            "phone": "+46701234567",
            "channel": "sms",
            "accepted_bylaws": False,
            "accepted_privacy": True,
            "bylaws_version": BYLAWS_VERSION,
            "privacy_version": PRIVACY_VERSION,
            "checkin_id": None,
        },
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "consent_required"

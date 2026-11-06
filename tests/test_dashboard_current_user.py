"""Dashboard audit and dev permissions follow the request session, not browser stores."""

import logging
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask
from starlette.middleware.wsgi import WSGIMiddleware

from fgt_dashboard.auth_helpers import current_user
from tests.test_dashboard_row_identity import dashboard, make_row

SESSION = {
    "user_id": "server-user",
    "user_name": "Server User",
    "user_email": "server@example.invalid",
    "access_token": "synthetic-secret-token",
    "is_dev": True,
}
FORGED = {
    "user_id": "dev-user",
    "user_name": "Forged User",
    "user_email": "forged@example.invalid",
    "is_dev": True,
}


@pytest.fixture
def request_session(dashboard, monkeypatch):
    lookup = Mock(return_value=SESSION.copy())
    monkeypatch.setattr(dashboard.storage, "get_session", lookup, raising=False)
    monkeypatch.setenv("DEV_TOOLS_ALLOWED_IDENTITIES", "dev-user")
    app = Flask(__name__)
    return app, lookup


def test_current_user_uses_valid_session_and_returns_minimal_safe_fields(request_session):
    app, lookup = request_session
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        assert current_user() == {
            "user_id": "server-user",
            "user_name": "Server User",
            "email": "server@example.invalid",
            "is_dev": False,
        }
        assert current_user()["user_id"] == "server-user"
    lookup.assert_called_once_with("synthetic-session")


@pytest.mark.parametrize("value", [None, {}, {"user_name": "Missing ID"}])
def test_invalid_session_is_unauthenticated(request_session, value):
    app, lookup = request_session
    lookup.return_value = value
    with app.test_request_context(headers={"Cookie": "fgc_session=invalid-session"}):
        assert current_user() is None


def test_missing_cookie_and_missing_request_context_are_unauthenticated(request_session):
    app, lookup = request_session
    assert current_user() is None
    with app.test_request_context():
        assert current_user() is None
    lookup.assert_not_called()


def test_lookup_failure_is_closed_and_does_not_log_cookie_or_exception_secrets(
    request_session, caplog
):
    app, lookup = request_session
    lookup.side_effect = RuntimeError("synthetic-secret-token")
    with (
        app.test_request_context(headers={"Cookie": "fgc_session=private-cookie"}),
        caplog.at_level(logging.WARNING),
    ):
        assert current_user() is None
    assert "private-cookie" not in caplog.text
    assert "synthetic-secret-token" not in caplog.text


@pytest.mark.parametrize(
    "user_id, name, email, expected",
    [
        ("server-user", "dev-user", "dev-user", False),
        ("dev-user", "Unrelated Name", "unrelated@example.invalid", True),
    ],
)
def test_dev_permission_matches_only_session_user_id(
    request_session, user_id, name, email, expected
):
    app, lookup = request_session
    lookup.return_value = {
        "user_id": user_id,
        "user_name": name,
        "user_email": email,
        "is_dev": True,
    }
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        assert current_user()["is_dev"] is expected


def test_default_named_owners_and_dev_environment_do_not_create_an_identity(
    request_session, monkeypatch
):
    app, lookup = request_session
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("DEV_TOOLS_ALLOWED_IDENTITIES", raising=False)
    with app.test_request_context():
        assert current_user() is None
    lookup.assert_not_called()


def test_cookie_is_available_through_the_actual_fastapi_wsgi_flask_bridge(request_session):
    flask_app, _ = request_session

    @flask_app.get("/who")
    def who():
        return current_user()

    api = FastAPI()
    api.mount("/", WSGIMiddleware(flask_app))
    with TestClient(api, cookies={"fgc_session": "synthetic-session"}) as client:
        response = client.get("/who")
    assert response.status_code == 200
    assert response.json()["user_id"] == "server-user"
    assert "access_token" not in response.json()


def assert_server_actor(actor):
    assert actor == {
        "user_id": "server-user",
        "user_name": "Server User",
        "user_email": "server@example.invalid",
    }


def test_payment_toggle_ignores_forged_audit_identity(dashboard, request_session):
    app, _ = request_session
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        dashboard.callbacks["toggle_field"](
            {"row": 0, "row_id": "record-0", "column_id": "payment_valid"},
            [make_row(0)],
            "test-event",
            FORGED,
        )
    dashboard.storage.update_checkin.assert_called_once()
    assert_server_actor(dashboard.storage.log_action.call_args.args[0])


def test_delete_ignores_forged_audit_identity(dashboard, request_session):
    app, _ = request_session
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        dashboard.callbacks["delete_selected_player"](
            1, ["record-0"], [make_row(0)], "test-event", FORGED
        )
    dashboard.storage.delete_checkin.assert_called_once_with("record-0")
    assert_server_actor(dashboard.storage.log_action.call_args.args[0])


def test_forged_store_cannot_enable_dev_tools_or_trigger_recompute(
    dashboard, request_session, monkeypatch
):
    app, _ = request_session
    recompute = Mock()
    monkeypatch.setattr(dashboard.storage, "recompute_event_stats", recompute, raising=False)
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        styles = dashboard.callbacks["toggle_dev_tools_visibility"](FORGED, ["show"])
        response = dashboard.callbacks["recompute_selected_event_stats"](1, "test-event", FORGED)
    assert all(style == {"display": "none"} for style in styles)
    assert "Unauthorized" in response.children
    recompute.assert_not_called()


def test_server_dev_identity_can_recompute_even_when_browser_store_disagrees(
    dashboard, request_session, monkeypatch
):
    app, lookup = request_session
    lookup.return_value = {**SESSION, "user_id": "dev-user"}
    recompute = Mock(return_value={"event_slug": "test-event"})
    monkeypatch.setattr(dashboard.storage, "recompute_event_stats", recompute, raising=False)
    with app.test_request_context(headers={"Cookie": "fgc_session=synthetic-session"}):
        dashboard.callbacks["recompute_selected_event_stats"](1, "test-event", {"is_dev": False})
    recompute.assert_called_once()
    actor = recompute.call_args.kwargs["user"]
    assert actor == {
        "user_id": "dev-user",
        "user_name": "Server User",
        "user_email": "server@example.invalid",
    }


def test_missing_session_cannot_gain_dev_access_from_store(dashboard, request_session):
    app, _ = request_session
    with app.test_request_context():
        assert all(
            style == {"display": "none"}
            for style in dashboard.callbacks["toggle_dev_tools_visibility"](FORGED, ["show"])
        )

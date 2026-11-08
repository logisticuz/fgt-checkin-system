"""Dashboard internal HTTP calls share authentication without changing soft mode."""

import ast
import logging
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from tests.test_dashboard_row_identity import dashboard, make_row  # noqa: F401
from tests.test_live_duplicate_ui import click, text_of, ui  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-dashboard-internal-token"


@pytest.fixture
def callback_globals(dashboard):  # noqa: F811
    return dashboard.callbacks["toggle_field"].__globals__


@pytest.fixture
def authenticated_dashboard(backend_app, request):
    # Import the real backend before the dashboard fixture replaces shared.storage.
    return backend_app, request.getfixturevalue("dashboard")


def test_every_dashboard_backend_request_uses_the_shared_helper():
    """Inventory every requests verb; only literal Start.gg calls may bypass the helper."""
    backend_paths = []
    direct_internal_calls = []
    for path in (ROOT / "fgt_dashboard").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "_backend_post":
                assert isinstance(node.args[0], ast.Constant)
                assert node.args[0].value.startswith("/api/")
                backend_paths.append(node.args[0].value)
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "requests"
                and node.func.attr
                in {"get", "post", "put", "patch", "delete", "head", "options", "request"}
            ):
                continue
            parent = parents[node]
            while not isinstance(parent, (ast.FunctionDef, ast.Module)):
                parent = parents[parent]
            if getattr(parent, "name", None) == "_backend_post":
                direct_internal_calls.append((path.name, node.func.attr))
                continue
            assert node.func.attr == "post", f"Unaudited HTTP call: {path}:{node.lineno}"
            assert isinstance(node.args[0], ast.Constant), f"Dynamic HTTP URL: {path}:{node.lineno}"
            assert (
                node.args[0].value == "https://api.start.gg/gql/alpha"
            ), f"Backend HTTP call bypasses helper: {path}:{node.lineno}"
    assert direct_internal_calls == [("callbacks.py", "post")]
    assert Counter(backend_paths) == Counter(
        {
            "/api/notify/update": 4,
            "/api/admin/recheck-startgg": 1,
            "/api/admin/bulk-recheck-startgg": 1,
            "/api/archive/event": 1,
            "/api/archive/reopen": 1,
            "/api/archive/delete": 1,
        }
    )


@pytest.mark.parametrize("mode", ["soft", "enforce"])
@pytest.mark.parametrize("configured_token", [None, "", "   ", f"  {TOKEN}  "])
def test_helper_headers_obey_the_real_internal_auth_policy(
    authenticated_dashboard, monkeypatch, mode, configured_token
):
    backend_app, dashboard = authenticated_dashboard
    callback_globals = dashboard.callbacks["toggle_field"].__globals__
    if configured_token is None:
        monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INTERNAL_API_TOKEN", configured_token)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_AUTH_MODE", mode)
    monkeypatch.setattr(backend_app.app_state, "INTERNAL_API_TOKEN", TOKEN)
    monkeypatch.setitem(
        callback_globals, "BACKEND_INTERNAL_URL", "http://dashboard-backend.invalid/"
    )
    app = FastAPI()

    @app.post(
        "/api/notify/update", dependencies=[Depends(backend_app.app_state.require_internal_soft)]
    )
    def notify():
        return {"success": True}

    with TestClient(app) as client:
        dashboard.post.side_effect = client.post
        response = callback_globals["_backend_post"](
            "/api/notify/update", json={"type": "field_updated"}, timeout=2
        )
    sent = dashboard.post.call_args
    assert sent.args == ("http://dashboard-backend.invalid/api/notify/update",)
    assert sent.kwargs["json"] == {"type": "field_updated"}
    assert sent.kwargs["timeout"] == 2
    expected_headers = (
        {"X-Internal-Token": TOKEN} if configured_token and configured_token.strip() else {}
    )
    assert sent.kwargs.get("headers", {}) == expected_headers
    assert response.status_code == (401 if mode == "enforce" and not expected_headers else 200)


def test_helper_preserves_other_headers_and_reads_the_current_token(
    dashboard, callback_globals, monkeypatch
):  # noqa: F811
    headers = {"Authorization": "Bearer synthetic-other-auth"}
    post = callback_globals["_backend_post"]
    for token in (TOKEN, "synthetic-rotated-token"):
        monkeypatch.setenv("INTERNAL_API_TOKEN", token)
        response = post(
            "/api/archive/event",
            json={"event_slug": "synthetic-event"},
            headers=headers,
            timeout=30,
        )
        assert response is dashboard.post.return_value
        assert dashboard.post.call_args.kwargs["headers"] == {**headers, "X-Internal-Token": token}
    assert headers == {"Authorization": "Bearer synthetic-other-auth"}


@pytest.mark.parametrize("status", [199, 300, 401, 500])
def test_non_2xx_is_logged_without_token_headers_or_response_body(
    dashboard, callback_globals, monkeypatch, caplog, status
):  # noqa: F811
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    dashboard.post.return_value = SimpleNamespace(status_code=status, text=TOKEN, reason=TOKEN)
    with caplog.at_level(logging.WARNING, logger=callback_globals["logger"].name):
        response = callback_globals["_backend_post"]("/api/notify/update", json={}, timeout=2)
    assert response is dashboard.post.return_value
    assert [record.getMessage() for record in caplog.records] == [
        f"Internal backend request refused: path=/api/notify/update HTTP {status}"
    ]
    assert TOKEN not in caplog.text and "X-Internal-Token" not in caplog.text


def prepared_response(url, **kwargs):
    """Run Requests' actual header validation without sending any network traffic."""
    requests.Request("POST", url, json=kwargs.get("json"), headers=kwargs.get("headers")).prepare()
    return SimpleNamespace(status_code=200)


@pytest.mark.parametrize("control", [chr(code) for code in range(32)] + [chr(127)])
def test_control_characters_are_omitted_and_warn_once(
    dashboard, callback_globals, monkeypatch, caplog, control
):  # noqa: F811
    token = f"synthetic-prefix{control}private-suffix"
    # NUL cannot be stored in an OS environment; exercise the config read directly.
    monkeypatch.setitem(callback_globals, "os", SimpleNamespace(getenv=lambda name: token))
    dashboard.post.side_effect = prepared_response
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            response = callback_globals["_backend_post"]("/api/notify/update", json={}, timeout=2)
            assert response.status_code == 200
    assert dashboard.post.call_count == 2
    assert "X-Internal-Token" not in dashboard.post.call_args.kwargs.get("headers", {})
    assert [record.getMessage() for record in caplog.records] == [
        "INTERNAL_API_TOKEN invalid (control characters); sending without token"
    ]
    assert token not in caplog.text and "private-suffix" not in caplog.text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_trailing_newline_is_stripped_and_valid_token_is_sent(
    dashboard, callback_globals, monkeypatch, caplog, newline
):  # noqa: F811
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN + newline)
    dashboard.post.side_effect = prepared_response
    with caplog.at_level(logging.WARNING):
        response = callback_globals["_backend_post"]("/api/notify/update", json={}, timeout=2)
    assert response.status_code == 200
    assert dashboard.post.call_args.kwargs["headers"] == {"X-Internal-Token": TOKEN}
    assert caplog.records == []


def exercise_internal_callback(ui, monkeypatch, action):  # noqa: F811
    """Drive all nine registered call sites with isolated storage and synthetic rows."""
    module = ui.callbacks["toggle_field"].__globals__
    ui.storage.get_active_settings.return_value = {"active_event_slug": "live-event"}
    ui.storage.get_active_settings_with_id.return_value = {"record_id": "settings-1", "fields": {}}
    ui.storage.get_active_slug.return_value = "live-event"
    ui.storage.recompute_active_statuses = Mock(return_value=0)
    ui.storage.begin_checkin = Mock(return_value={"record_id": "record-0", "created": True})
    monkeypatch.setitem(module, "_manual_game_choices", lambda *args, **kwargs: ([], ""))
    click("btn-live-dup-keep-b.n_clicks")
    target = {"slug": "live-event", "active_slug": "live-event"}
    calls = {
        "duplicates": lambda: ui.callbacks["resolve_live_duplicate_choice"](
            0, 1, 0, 0, {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"}
        ),
        "field": lambda: ui.callbacks["toggle_field"](
            {"row": 0, "row_id": "record-0", "column_id": "member"}, [make_row(0)], "live-event", {}
        ),
        "manual": lambda: ui.callbacks["add_manual_checkin"](
            1, "Synthetic Player", "synthetic-tag", [], "live-event", {}
        ),
        "requirements": lambda: ui.callbacks["save_requirements"](
            1, [True], [], [], [], [], {}, {}
        ),
        "single": lambda: ui.callbacks["recheck_startgg"](
            1, ["record-0"], [make_row(0)], "live-event", {}
        ),
        "bulk": lambda: ui.callbacks["bulk_recheck_startgg"](1, "live-event", {}),
        "archive": lambda: ui.callbacks["archive_current_event"](1, target, {}),
        "reopen": lambda: ui.callbacks["reopen_archived_event"](1, target, {}),
        "delete": lambda: ui.callbacks["delete_archived_event"](
            1, "live-event", "Synthetic reason", {}
        ),
    }
    return calls[action]()


@pytest.mark.parametrize(
    "action",
    [
        "duplicates",
        "field",
        "manual",
        "requirements",
        "single",
        "bulk",
        "archive",
        "reopen",
        "delete",
    ],
)
def test_every_request_exception_path_keeps_token_out_of_logs_and_ui(
    ui, monkeypatch, caplog, action
):  # noqa: F811
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)

    def invalid_header(url, **kwargs):
        # Model a later Requests validation error: the real exception embeds the value.
        kwargs["headers"] = {"X-Internal-Token": TOKEN + "\n" + TOKEN}
        return prepared_response(url, **kwargs)

    ui.post.side_effect = invalid_header
    with caplog.at_level(logging.WARNING):
        result = exercise_internal_callback(ui, monkeypatch, action)
    ui.post.assert_called_once()
    assert "InvalidHeader" in caplog.text
    assert urlsplit(ui.post.call_args.args[0]).path in caplog.text
    assert TOKEN not in caplog.text and TOKEN not in text_of(result)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("action", ["single", "bulk"])
def test_timeout_exception_paths_keep_token_out_of_logs_and_ui(
    ui, monkeypatch, caplog, action
):  # noqa: F811
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    ui.post.side_effect = requests.exceptions.Timeout(TOKEN)
    with caplog.at_level(logging.WARNING):
        result = exercise_internal_callback(ui, monkeypatch, action)
    ui.post.assert_called_once()
    assert "Timeout" in caplog.text and "timed out" in text_of(result)
    assert TOKEN not in caplog.text and TOKEN not in text_of(result)
    assert all(record.exc_info is None for record in caplog.records)

"""Dashboard internal HTTP calls share authentication without changing soft mode."""

import ast
import logging
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from tests.test_dashboard_row_identity import dashboard  # noqa: F401

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

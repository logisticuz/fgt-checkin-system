"""Real FastAPI auth handlers and docs configuration with synthetic OAuth/storage."""

import html
import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CODE = "synthetic-code"
TOKEN = "synthetic-private-token"


@pytest.fixture
def load_application(monkeypatch):
    def load(kind, docs=None):
        if docs is None:
            monkeypatch.delenv("FGT_API_DOCS", raising=False)
        else:
            monkeypatch.setenv("FGT_API_DOCS", docs)
        monkeypatch.setenv(
            "STARTGG_REDIRECT_URI", "https://dashboard.example.invalid/auth/callback"
        )
        monkeypatch.setenv("ENV", "dev")
        auth = None
        if kind == "dashboard":
            import shared

            storage = ModuleType("shared.storage")
            for name in (
                "create_session",
                "get_session",
                "delete_session",
                "update_session_activity",
                "cleanup_expired_sessions",
                "get_active_slug",
                "get_all_event_slugs",
                "get_active_settings_with_id",
                "update_settings",
                "log_action",
            ):
                setattr(storage, name, Mock())
            storage.get_session.return_value = {"user_id": "synthetic-user", "user_name": "Test TO"}
            storage.get_active_slug.return_value = "test-event"
            storage.cleanup_expired_sessions.return_value = 0
            storage.create_session.return_value = "synthetic-session"
            monkeypatch.setitem(sys.modules, "shared.storage", storage)
            monkeypatch.setattr(shared, "storage", storage, raising=False)

            auth = ModuleType("auth")
            for name in (
                "build_authorize_url",
                "exchange_code_for_token",
                "get_startgg_user",
                "is_event_admin",
                "check_event_admin",
            ):
                setattr(auth, name, Mock())
            auth.build_authorize_url.return_value = "https://provider.example.invalid/authorize"
            auth.exchange_code_for_token.return_value = {"access_token": TOKEN}
            auth.get_startgg_user.return_value = {"id": "synthetic-user", "name": "Test TO"}
            auth.is_event_admin.return_value = True
            monkeypatch.setitem(sys.modules, "auth", auth)

            def wsgi_fallback(environ, start_response):
                start_response("200 OK", [("Content-Type", "text/plain")])
                return [b"Synthetic Dash catch-all"]

            dash = ModuleType("app")
            dash.app = SimpleNamespace(server=wsgi_fallback)
            monkeypatch.setitem(sys.modules, "app", dash)
            path = ROOT / "fgt_dashboard" / "api.py"
        else:
            monkeypatch.chdir(ROOT / "backend")
            path = ROOT / "backend" / "main.py"
        name = f"auth_docs_{kind}_{docs}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module, auth

    return load


@pytest.mark.parametrize(
    "code", ["<script>alert(1)</script>", 'x"};alert(1);//', "x\n", "x/y", "x y", "x" * 513, ""]
)
def test_invalid_callback_code_is_rejected_without_reflection(load_application, code):
    module, auth = load_application("dashboard")
    with TestClient(module.app) as client:
        response = client.get("/auth/callback", params={"code": code})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid authorization code"}
    auth.exchange_code_for_token.assert_not_called()


@pytest.mark.parametrize("prod", [False, True])
@pytest.mark.parametrize("code", ["AZaz09._~-", "x" * 512])
def test_valid_code_gets_encoded_302_without_bridge_html(load_application, monkeypatch, prod, code):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    with TestClient(module.app) as client:
        response = client.get("/auth/callback", params={"code": code}, follow_redirects=False)
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.path == ("/auth/callback/finalize" if prod else "/admin/auth/callback/finalize")
    assert parse_qs(location.query) == {"code": [code]}
    assert response.content == b""
    auth.exchange_code_for_token.assert_not_called()


def test_remaining_waiting_page_uses_safe_html_and_json_string(load_application):
    module, _ = load_application("dashboard")
    title = '<img src=x onerror="alert(1)">'
    message = '<script>alert("message")</script>'
    target = "/next?value='\"</script><script>alert(1)</script>&x=1"
    rendered = module._render_waiting_redirect_page(title, message, target)
    assert html.escape(title, quote=True) in rendered
    assert html.escape(message, quote=True) in rendered
    assert rendered.count("</script>") == 1
    javascript = rendered.split("window.location.href = ", 1)[1]
    value, end = json.JSONDecoder().raw_decode(javascript)
    assert value == target
    assert javascript[end:].startswith(";")
    assert "<" not in javascript[:end]


@pytest.mark.parametrize("path", ["/auth/login", "/auth/callback/finalize"])
def test_auth_exceptions_return_log_id_without_provider_secrets(load_application, caplog, path):
    module, auth = load_application("dashboard")
    secret_message = f"Provider failure: code={CODE} token={TOKEN} private-detail"
    if path == "/auth/login":
        auth.build_authorize_url.side_effect = ValueError(secret_message)
    else:
        auth.exchange_code_for_token.side_effect = RuntimeError(secret_message)
    with caplog.at_level(logging.ERROR), TestClient(module.app) as client:
        response = client.get(path, params={"code": CODE})
    assert response.status_code == 500
    body = response.json()
    assert re.fullmatch(r"[0-9a-f]{8}", body["log_id"])
    assert body["error"] in {"Unable to start authentication.", "Authentication failed."}
    assert CODE not in response.text and TOKEN not in response.text
    records = [record for record in caplog.records if record.name == module.logger.name]
    assert any(body["log_id"] in record.getMessage() and record.exc_info for record in records)
    assert CODE not in caplog.text and TOKEN not in caplog.text
    assert secret_message not in caplog.text


def test_failed_exchange_logs_only_response_key_names(load_application, caplog):
    module, auth = load_application("dashboard")
    auth.exchange_code_for_token.return_value = {
        "refresh_token": TOKEN,
        "token_type": "synthetic-type",
    }
    with caplog.at_level(logging.ERROR), TestClient(module.app) as client:
        response = client.get("/auth/callback/finalize", params={"code": CODE})
    assert response.status_code == 500
    assert TOKEN not in response.text
    assert "refresh_token" in caplog.text and "token_type" in caplog.text
    assert TOKEN not in caplog.text and "synthetic-type" not in caplog.text
    assert CODE not in caplog.text


@pytest.mark.parametrize("kind", ["backend", "dashboard"])
@pytest.mark.parametrize("flag", [None, "0", "true", "1"])
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_require_explicit_opt_in(load_application, kind, flag, path):
    module, _ = load_application(kind, flag)
    with TestClient(module.app, cookies={"fgc_session": "synthetic-session"}) as client:
        response = client.get(path, follow_redirects=False)
    assert response.status_code == (200 if flag == "1" else 404)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_disabled_dashboard_docs_do_not_fall_through_to_login_or_dash(load_application, path):
    module, _ = load_application("dashboard")
    with TestClient(module.app) as client:
        response = client.get(path, follow_redirects=False)
    assert response.status_code == 404

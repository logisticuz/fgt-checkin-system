"""Real FastAPI auth handlers and docs configuration with synthetic OAuth/storage."""

import html
import importlib.util
import json
import logging
import re
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from shared import auth as oauth

ROOT = Path(__file__).resolve().parents[1]
CODE = "synthetic-code"
TOKEN = "synthetic-private-token"
STATE_SECRET = "synthetic-oauth-state-signing-secret"


@pytest.fixture
def load_application(monkeypatch):
    monkeypatch.setenv("STARTGG_CLIENT_ID", "synthetic-oauth-client")
    monkeypatch.setenv("STARTGG_CLIENT_SECRET", STATE_SECRET)
    monkeypatch.setattr(oauth, "STARTGG_CLIENT_ID", "synthetic-oauth-client")
    monkeypatch.setattr(oauth, "STARTGG_CLIENT_SECRET", STATE_SECRET)

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
            auth.build_authorize_url.side_effect = oauth.build_authorize_url
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


def start_login(client):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code in (302, 307)
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


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
    with TestClient(module.app, base_url="https://dashboard.example.invalid") as client:
        state = start_login(client)
        response = client.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location.path == ("/auth/callback/finalize" if prod else "/admin/auth/callback/finalize")
    assert parse_qs(location.query) == {"code": [code], "state": [state]}
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
        state = start_login(client) if path.endswith("finalize") else ""
        response = client.get(path, params={"code": CODE, "state": state})
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
        state = start_login(client)
        response = client.get("/auth/callback/finalize", params={"code": CODE, "state": state})
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


@pytest.mark.parametrize(
    "prod,scheme", [(False, "http"), (False, "https"), (True, "http"), (True, "https")]
)
def test_login_state_matches_authorize_url_and_cookie_flags(
    load_application, monkeypatch, prod, scheme
):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    with TestClient(module.app, base_url=f"{scheme}://dashboard.example.invalid") as client:
        response = client.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        state = query["state"][0]
        cookie = SimpleCookie()
        header = response.headers["set-cookie"]
        cookie.load(header)
        name = "__Host-fgt_oauth_state" if prod or scheme == "https" else "fgc_oauth_state"
        assert header.startswith(name + "=")
        assert set(cookie) == {name}
        challenge = cookie[name]
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
        assert oauth.verify_oauth_state(challenge.value, state)
        assert challenge.value.split(".")[0] == state
        assert challenge["httponly"] is True
        assert bool(challenge["secure"]) is (prod or scheme == "https")
        assert challenge["samesite"].lower() == "lax"
        assert challenge["max-age"] == "600"
        assert challenge["path"] == "/"
        assert challenge["domain"] == ""
    assert query["redirect_uri"] == [module.OAUTH_REDIRECT_URI]
    assert query["scope"] == ["user.identity user.email tournament.manager"]
    auth.build_authorize_url.assert_called_once_with(module.OAUTH_REDIRECT_URI, state=state)


@pytest.mark.parametrize("prod", [False, True])
def test_valid_state_survives_callback_redirect_and_is_consumed_on_finalize(
    load_application, monkeypatch, prod
):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    cookie_name = "__Host-fgt_oauth_state"
    with TestClient(module.app, base_url="https://dashboard.example.invalid") as client:
        state = start_login(client)
        params = {"code": CODE, "state": state}
        callback = client.get("/auth/callback", params=params, follow_redirects=False)
        assert callback.status_code == 302 and callback.content == b""
        assert client.cookies.get(cookie_name)
        target = urlsplit(callback.headers["location"])
        # Direct TestClient requests stand in for nginx stripping the dev /admin prefix.
        response = client.get(target.path.removeprefix("/admin"), params=parse_qs(target.query))
        assert response.status_code == 200
        assert client.cookies.get(cookie_name) is None
        assert client.cookies.get(module.SESSION_COOKIE_NAME) == "synthetic-session"
        assert (
            CODE not in response.text and state not in response.text and TOKEN not in response.text
        )
        for path in ("/auth/callback", "/auth/callback/finalize"):
            replay = client.get(path, params=params, follow_redirects=False)
            assert replay.status_code == 400
            assert replay.json() == {"error": "Invalid authentication state"}
        module.create_session.assert_called_once()
        auth.exchange_code_for_token.assert_called_once_with(CODE, module.OAUTH_REDIRECT_URI)


@pytest.mark.parametrize("prod", [False, True])
@pytest.mark.parametrize("path", ["/auth/callback", "/auth/callback/finalize"])
def test_secure_handlers_reject_parent_domain_legacy_state_cookie(
    load_application, monkeypatch, prod, path
):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    state = "s" * 43
    with TestClient(module.app, base_url="https://dashboard.example.invalid") as client:
        # A sibling can plant the legacy name on the parent domain, even with a valid signature.
        client.cookies.set(
            "fgc_oauth_state", oauth.seal_oauth_state(state), domain=".example.invalid", path="/"
        )
        response = client.get(path, params={"code": CODE, "state": state}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid authentication state"}
    header = response.headers["set-cookie"]
    assert header.startswith("__Host-fgt_oauth_state=")
    cookie = SimpleCookie()
    cookie.load(header)
    deletion = cookie["__Host-fgt_oauth_state"]
    assert deletion["max-age"] == "0" and deletion["path"] == "/"
    assert deletion["secure"] is True and deletion["httponly"] is True
    assert deletion["domain"] == "" and deletion["samesite"].lower() == "lax"
    auth.exchange_code_for_token.assert_not_called()
    module.create_session.assert_not_called()


@pytest.mark.parametrize("prod", [False, True])
def test_host_state_cookie_is_used_despite_parent_domain_legacy_cookie(
    load_application, monkeypatch, prod
):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    with TestClient(module.app, base_url="https://dashboard.example.invalid") as client:
        state = start_login(client)
        assert client.cookies.get("__Host-fgt_oauth_state")
        client.cookies.set(
            "fgc_oauth_state", oauth.seal_oauth_state("a" * 43), domain=".example.invalid", path="/"
        )
        params = {"code": CODE, "state": state}
        callback = client.get("/auth/callback", params=params, follow_redirects=False)
        assert callback.status_code == 302
        response = client.get("/auth/callback/finalize", params=params)
        assert response.status_code == 200
        assert client.cookies.get("__Host-fgt_oauth_state") is None
        assert client.cookies.get("fgc_oauth_state")
        assert client.cookies.get(module.SESSION_COOKIE_NAME) == "synthetic-session"
    module.create_session.assert_called_once()
    auth.exchange_code_for_token.assert_called_once_with(CODE, module.OAUTH_REDIRECT_URI)


@pytest.mark.parametrize("path", ["/auth/callback", "/auth/callback/finalize"])
@pytest.mark.parametrize("bad_state", [None, "x" * 43, '<script>state-secret</script>'])
def test_invalid_state_has_no_reflection_session_or_provider_call(
    load_application, caplog, path, bad_state
):
    module, auth = load_application("dashboard")
    with TestClient(module.app) as client:
        state = start_login(client)
        params = {"code": CODE}
        if bad_state is not None:
            params["state"] = bad_state
        with caplog.at_level(logging.WARNING):
            response = client.get(path, params=params, follow_redirects=False)
        assert response.status_code == 400
        assert response.json() == {"error": "Invalid authentication state"}
        assert client.cookies.get(oauth.OAUTH_STATE_COOKIE_NAME) is None
        assert module.SESSION_COOKIE_NAME not in client.cookies
    auth.exchange_code_for_token.assert_not_called()
    module.create_session.assert_not_called()
    assert state not in response.text and CODE not in response.text
    records = [
        record.getMessage() for record in caplog.records if record.name == module.logger.name
    ]
    assert records and all("reason=invalid_state" in value for value in records)
    assert all(state not in value and CODE not in value for value in records)
    if bad_state:
        assert bad_state not in response.text
        assert all(bad_state not in value for value in records)


@pytest.mark.parametrize("path", ["/auth/callback", "/auth/callback/finalize"])
def test_state_without_its_browser_cookie_is_rejected(load_application, path):
    module, auth = load_application("dashboard")
    with TestClient(module.app) as client:
        response = client.get(
            path, params={"code": CODE, "state": "s" * 43}, follow_redirects=False
        )
    assert response.status_code == 400
    auth.exchange_code_for_token.assert_not_called()
    module.create_session.assert_not_called()


@pytest.mark.parametrize("change", ["state", "timestamp", "signature", "expired", "future"])
def test_signed_cookie_rejects_tampering_and_expiry(load_application, monkeypatch, change):
    module, auth = load_application("dashboard")
    with TestClient(module.app) as client:
        state = start_login(client)
        value = client.cookies.get(oauth.OAUTH_STATE_COOKIE_NAME)
        parts = value.split(".")
        if change == "state":
            parts[0] = "z" * 43
        elif change == "timestamp":
            parts[1] = str(int(parts[1]) + 1)
        elif change == "signature":
            parts[2] = ("0" if parts[2][0] != "0" else "1") + parts[2][1:]
        elif change == "expired":
            monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: int(parts[1]) + 600))
        else:
            monkeypatch.setattr(oauth, "time", SimpleNamespace(time=lambda: int(parts[1]) - 1))
        client.cookies.clear()
        client.cookies.set(oauth.OAUTH_STATE_COOKIE_NAME, ".".join(parts), path="/")
        response = client.get("/auth/callback/finalize", params={"code": CODE, "state": state})
        assert response.status_code == 400
    auth.exchange_code_for_token.assert_not_called()
    module.create_session.assert_not_called()


def test_state_issued_by_one_worker_finalizes_in_a_fresh_worker(load_application, monkeypatch):
    first, _ = load_application("dashboard")
    monkeypatch.setattr(first, "IS_PROD", True)
    with TestClient(first.app, base_url="https://dashboard.example.invalid") as issuer:
        state = start_login(issuer)
        cookies = issuer.cookies
        spec = importlib.util.spec_from_file_location(
            "fresh_oauth_worker", ROOT / "shared" / "auth.py"
        )
        fresh_oauth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh_oauth)
        import shared

        monkeypatch.setitem(sys.modules, "shared.auth", fresh_oauth)
        monkeypatch.setattr(shared, "auth", fresh_oauth)
        second, second_auth = load_application("dashboard")
        monkeypatch.setattr(second, "IS_PROD", True)
        with TestClient(second.app, base_url="https://dashboard.example.invalid") as finalizer:
            finalizer.cookies.update(cookies)
            callback = finalizer.get(
                "/auth/callback", params={"code": CODE, "state": state}, follow_redirects=False
            )
            assert callback.status_code == 302
            response = finalizer.get(
                "/auth/callback/finalize", params={"code": CODE, "state": state}
            )
            assert response.status_code == 200
            assert finalizer.cookies.get("__Host-fgt_oauth_state") is None
            second.create_session.assert_called_once()
            second_auth.exchange_code_for_token.assert_called_once_with(
                CODE, second.OAUTH_REDIRECT_URI
            )


def test_missing_state_signing_secret_fails_closed_with_startup_diagnostic(
    load_application, monkeypatch, caplog
):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(oauth, "STARTGG_CLIENT_SECRET", "")
    with TestClient(module.app) as client:
        response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 500
    assert response.json()["error"] == "Unable to start authentication."
    auth.build_authorize_url.assert_not_called()
    monkeypatch.delenv("STARTGG_CLIENT_SECRET")
    spec = importlib.util.spec_from_file_location(
        "missing_oauth_config", ROOT / "shared" / "auth.py"
    )
    fresh_oauth = importlib.util.module_from_spec(spec)
    with caplog.at_level(logging.ERROR):
        spec.loader.exec_module(fresh_oauth)
    assert "oauth_state_config_missing" in caplog.text
    assert "STARTGG_CLIENT_SECRET must be configured" in caplog.text


def test_oauth_token_exchange_preserves_hotfix_json_redirect_and_scope(
    load_application, monkeypatch
):
    load_application("dashboard")
    requests = []

    def token_endpoint(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": TOKEN})

    client_class = httpx.Client
    monkeypatch.setattr(
        oauth.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(token_endpoint), **kwargs),
    )
    redirect_uri = "https://admin.example.invalid/auth/callback"
    assert oauth.exchange_code_for_token(CODE, redirect_uri) == {"access_token": TOKEN}
    request = requests[0]
    assert str(request.url) == "https://api.start.gg/oauth/access_token"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["redirect_uri"] == redirect_uri
    assert body["scope"] == "user.identity user.email tournament.manager"
    assert body["grant_type"] == "authorization_code" and body["code"] == CODE


@pytest.mark.parametrize("prod", [False, True])
@pytest.mark.parametrize("failure", ["exchange", "missing_token", "user", "denied", "session"])
def test_finalize_failure_also_consumes_browser_state(load_application, monkeypatch, prod, failure):
    module, auth = load_application("dashboard")
    monkeypatch.setattr(module, "IS_PROD", prod)
    scheme = "https" if prod else "http"
    cookie_name = "__Host-fgt_oauth_state" if prod else "fgc_oauth_state"
    if failure == "exchange":
        auth.exchange_code_for_token.side_effect = RuntimeError("Synthetic provider failure")
    elif failure == "missing_token":
        auth.exchange_code_for_token.return_value = {}
    elif failure == "user":
        auth.get_startgg_user.return_value = {}
    elif failure == "denied":
        auth.is_event_admin.return_value = False
    else:
        module.create_session.return_value = None
    with TestClient(module.app, base_url=f"{scheme}://dashboard.example.invalid") as client:
        state = start_login(client)
        params = {"code": CODE, "state": state}
        response = client.get("/auth/callback/finalize", params=params)
        assert response.status_code == (403 if failure == "denied" else 500)
        assert client.cookies.get(cookie_name) is None
        assert client.cookies.get(module.SESSION_COOKIE_NAME) is None
        replay = client.get("/auth/callback/finalize", params=params)
        assert replay.status_code == 400
    auth.exchange_code_for_token.assert_called_once()


@pytest.mark.parametrize("code", ["", "<script>bad-code</script>"])
def test_finalize_rejects_invalid_code_without_reflection(load_application, code):
    module, auth = load_application("dashboard")
    with TestClient(module.app) as client:
        state = start_login(client)
        response = client.get("/auth/callback/finalize", params={"code": code, "state": state})
        assert response.status_code == 400
        assert response.json() == {"error": "Invalid authorization code"}
        assert client.cookies.get(oauth.OAUTH_STATE_COOKIE_NAME) is None
    auth.exchange_code_for_token.assert_not_called()
    module.create_session.assert_not_called()


def test_cookie_is_bound_to_the_configured_signing_secret(load_application, monkeypatch):
    load_application("dashboard")
    state = "s" * 43
    cookie = oauth.seal_oauth_state(state)
    assert oauth.verify_oauth_state(cookie, state)
    monkeypatch.setattr(oauth, "STARTGG_CLIENT_SECRET", "another-synthetic-signing-secret")
    assert oauth.verify_oauth_state(cookie, state) is False

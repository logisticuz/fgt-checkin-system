"""Acceptance CLI contracts with in-memory HTTP only; no live DEV writes."""

import httpx
import pytest

from scripts import acceptance_smoke as smoke

BASE = "https://checkin.fgctrollhattan.se"
ADMIN = "https://admin.fgctrollhattan.se"
DEV = "http://localhost:8088"
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy-Report-Only": "default-src 'self'",
    "Strict-Transport-Security": "max-age=31536000",
}


def test_readonly_follows_canonical_gets_without_sending_any_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert not request.content
        for header in ("authorization", "cookie", "proxy-authorization"):
            assert header not in request.headers
        if request.url.host == "checkin.fgctrollhattan.se":
            return httpx.Response(
                301,
                headers={
                    **HEADERS,
                    "Location": str(request.url.copy_with(host="www.checkin.fgctrollhattan.se")),
                },
            )
        if request.url.host == "admin.fgctrollhattan.se":
            return httpx.Response(401, headers=HEADERS)
        status = (
            404
            if request.url.path in {*smoke.INTERNAL_PATHS.values(), *smoke.DEV_DASH_PATHS.values()}
            else 200
        )
        return httpx.Response(
            status, headers={**HEADERS, "Set-Cookie": "private-session=must-never-forward"}
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-password"),
        headers={"Authorization": "private-default", "Cookie": "private-default=1"},
    ) as client:
        checks = smoke.readonly(
            client,
            BASE,
            admin_base=ADMIN,
            meetup_base="https://meetup.fgctrollhattan.se",
            membercard_base="https://membercard.fgctrollhattan.se",
        )
    assert requests
    assert all(check.passed is True for check in checks)


@pytest.mark.parametrize("problem", ["status", "headers", "cookie"])
def test_readonly_fails_without_printing_private_response_data(problem, capsys):
    def handler(request):
        status = 404 if request.url.path in smoke.INTERNAL_PATHS.values() else 200
        headers = HEADERS.copy()
        if request.url.host == "admin.fgctrollhattan.se":
            status = 401
            if problem == "cookie":
                headers["Set-Cookie"] = "private=must-not-print"
        elif request.url.path == "/health":
            if problem == "status":
                status = 500
            if problem == "headers":
                headers.pop("X-Frame-Options")
        return httpx.Response(status, headers=headers, text="private-response-personal-data")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checks = smoke.readonly(client, "https://www.checkin.fgctrollhattan.se", admin_base=ADMIN)
    smoke.print_checks(checks)
    assert any(check.passed is False for check in checks)
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "location",
    [
        "https://outside.invalid/",
        "https://user:private@checkin.fgctrollhattan.se/",
        "/?token=private",
    ],
)
def test_readonly_never_follows_unapproved_or_credential_redirect(location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": location})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(smoke.SmokeError):
            smoke.HTTP(client).request("GET", BASE + "/health", redirects=True)
    assert len(requests) == 1


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_readonly_transport_rejects_writes_before_sending(method):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("No request allowed"))
    ) as client:
        with pytest.raises(smoke.SmokeError, match="Writes are disabled"):
            smoke.HTTP(client).request(method, DEV + "/api/checkin/orchestrate")


@pytest.mark.parametrize(
    "base",
    [
        BASE,
        "http://localhost.invalid:8088",
        "http://127.0.0.2:8088",
        "http://localhost:private@outside.invalid",
        "http://localhost:8088/?to=private",
        "http://localhost:8088/#private",
    ],
)
def test_dev_guard_rejects_nonlocal_or_ambiguous_base_without_http(base):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("No request allowed"))
    ) as client:
        with pytest.raises(smoke.SmokeError):
            smoke.dev_guard(client, base)


@pytest.mark.parametrize(
    "body,status",
    [
        ({"components": {"integration_engine": "n8n"}}, 200),
        ({"integration_engine": "none"}, 200),
        ({"components": {"integration_engine": None}}, 200),
        ({"components": {"integration_engine": "none"}}, 503),
        (None, 200),
    ],
)
def test_dev_guard_fails_closed_unless_engine_none_is_explicit(body, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(smoke.SmokeError, match="integration_engine=none"):
            smoke.dev_guard(client, DEV)
    assert [(request.method, request.url.path) for request in requests] == [("GET", "/health")]


def test_dev_guard_enables_only_loopback_writes_without_following_redirects():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"components": {"integration_engine": "none"}})
        return httpx.Response(307, headers={"Location": "https://outside.invalid/"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        http = smoke.dev_guard(client, DEV)
        assert http.request("POST", DEV + "/api/checkin/orchestrate", json={}).status_code == 307
        with pytest.raises(smoke.SmokeError):
            http.request("POST", BASE + "/api/checkin/orchestrate", json={})
    assert len(requests) == 2


class Fixture:
    def __init__(self, *, setup_fails=False, finish_fails=False):
        self.slug = None
        self.rows = {"real-player": {"event_slug": "existing-event", "real": True}}
        self.settings = {"active_event_slug": "existing-event", "unchanged": "private-setting"}
        self.snapshot = self.settings.copy()
        self.setup_fails, self.finish_fails = setup_fails, finish_fails
        self.started = self.finished = False

    def setup(self, slug):
        self.started = True
        self.slug = slug
        self.settings["active_event_slug"] = slug
        if self.setup_fails:
            raise smoke.SmokeError("Fixture setup refused")

    def verify(self, checkin_id=None):
        assert self.settings["active_event_slug"] == self.slug
        if checkin_id is not None:
            assert self.rows[checkin_id]["event_slug"] == self.slug

    def finish(self):
        self.finished = True
        self.rows = {key: row for key, row in self.rows.items() if row["event_slug"] != self.slug}
        self.settings = self.snapshot.copy()
        if self.finish_fails:
            raise smoke.SmokeError("Fixture cleanup not confirmed")


def dev_client(fixture, *, fail_at=None, optional_payment=False):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host in smoke.LOOPBACK_HOSTS
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path == "/health":
            return httpx.Response(200, json={"components": {"integration_engine": "none"}})
        assert fixture.started
        import json

        body = json.loads(request.content) if request.content else {}
        assert "personnummer" not in body
        if request.url.path == "/api/checkin/orchestrate":
            key = f"synthetic-{len(fixture.rows)}"
            fixture.rows[key] = {"event_slug": fixture.slug, "games": [], "paid": False}
            if fail_at == "orchestrate":
                return httpx.Response(500, text="private provider response")
            return httpx.Response(
                200,
                json={
                    "slug": fixture.slug,
                    "checkin_id": key,
                    "status_token": "private-capability",
                },
            )
        if request.url.path.startswith("/api/checkin/"):
            assert request.url.params["t"] == "private-capability"
            row = fixture.rows[request.url.path.split("/")[-2]]
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "games": row["games"],
                    "payment_ok": row["paid"],
                    "ready": bool(row["games"]) and row["paid"],
                },
            )
        if request.url.path == "/api/player/games":
            if fail_at == "games":
                return httpx.Response(403, text="private body")
            fixture.rows[body["checkin_id"]]["games"] = body["games"]
            return httpx.Response(200, json={"success": True})
        if request.url.path.startswith("/players/"):
            if optional_payment:
                return httpx.Response(404)
            key = request.url.path.split("/")[-2]
            fixture.rows[key]["paid"] = True
            return httpx.Response(200, json={"success": True, "payment_valid": True})
        pytest.fail("Unexpected synthetic HTTP route")

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_dev_three_player_flow_restores_settings_and_only_cleans_its_event(capsys):
    fixture = Fixture()
    client, requests = dev_client(fixture)
    with client:
        checks = smoke.dev_write(client, DEV, fixture=fixture)
    smoke.print_checks(checks)
    assert all(check.passed is True for check in checks)
    assert fixture.finished
    assert fixture.settings == fixture.snapshot
    assert fixture.rows == {"real-player": {"event_slug": "existing-event", "real": True}}
    assert len([request for request in requests if request.method == "POST"]) == 3
    assert len([request for request in requests if request.method == "PATCH"]) == 6
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["setup", "orchestrate", "games", "cleanup"])
def test_dev_finally_restores_even_after_partial_failure(failure):
    fixture = Fixture(setup_fails=failure == "setup", finish_fails=failure == "cleanup")
    client, _ = dev_client(fixture, fail_at=failure)
    with client:
        checks = smoke.dev_write(client, DEV, fixture=fixture)
    assert any(check.passed is False for check in checks)
    assert fixture.finished
    assert fixture.settings == fixture.snapshot
    assert list(fixture.rows) == ["real-player"]


def test_dev_payment_api_can_be_absent_without_touching_another_row():
    fixture = Fixture()
    client, _ = dev_client(fixture, optional_payment=True)
    with client:
        checks = smoke.dev_write(client, DEV, fixture=fixture)
    assert not any(check.passed is False for check in checks)
    assert len([check for check in checks if check.passed is None]) == 3
    assert list(fixture.rows) == ["real-player"]


@pytest.mark.parametrize(
    "container",
    ["fgt-checkin-system-backend-1", "fgt-prod-backend-1", "fgt-dev-backend-1-extra", "backend"],
)
def test_helper_refuses_every_container_except_fixed_dev(container, monkeypatch):
    monkeypatch.setattr(
        smoke.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("No Docker allowed")
    )
    with pytest.raises(smoke.SmokeError, match="fixed DEV"):
        smoke.DockerFixture(container)


def test_helper_rechecks_engine_inside_container_before_importing_storage(monkeypatch, capsys):
    import builtins
    import json

    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"shared", "psycopg_pool"}:
            pytest.fail("Storage must not even load before the container engine guard")
        return original(name, *args, **kwargs)

    monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    scope = {"__builtins__": {**vars(builtins), "__import__": guarded_import}}
    exec(compile(smoke.DEV_HELPER, "<mocked-dev-helper>", "exec"), scope)
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_dev_refuses_engine_or_nonlocal_admin_before_helper_or_write():
    fixture = Fixture()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"components": {"integration_engine": "n8n"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(smoke.SmokeError):
            smoke.dev_write(client, DEV, fixture=fixture)
        with pytest.raises(smoke.SmokeError):
            smoke.dev_write(client, DEV, admin_base=ADMIN, fixture=fixture)
    assert not fixture.started
    assert all(request.method == "GET" for request in requests)


def test_readonly_cli_exit_code_is_nonzero_for_failed_probes(monkeypatch, capsys):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="private diagnostic")
        )
    )
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kwargs: client)
    assert smoke.main(["--mode", "readonly", "--base", DEV]) == 1
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize("scenario", ["finish", "eof", "unexpected-player", "delete-failure"])
def test_embedded_helper_finally_restores_and_preserves_real_players(scenario, monkeypatch):
    import builtins
    import contextlib
    import io
    import json
    from types import SimpleNamespace

    slug = "acceptance-20261107123456-123456abcdef"
    original = {
        "active_event_slug": "existing-event",
        "event_display_name": "Existing Event",
        "require_membership": True,
        "require_startgg": True,
        "require_payment": False,
        "swish_expected_per_game": 15,
        "default_game": ["Existing Game"],
        "events_json": [{"name": "Existing Game"}],
        "offer_membership": True,
        "collect_acquisition_source": True,
    }
    settings = original.copy()
    rows = {
        "real": {
            "event_slug": "existing-event",
            "tag": "real",
            "email": "real@example.invalid",
            "name": "Existing Player",
        }
    }

    def update_settings(record_id, fields):
        settings.update(fields)
        if fields.get("active_event_slug") == slug:
            for index in range(1, 4):
                rows[f"synthetic-{index}"] = {
                    "event_slug": slug,
                    "tag": f"acc-123456abcdef-{index}",
                    "email": f"acceptance-123456abcdef-{index}@example.invalid",
                    "name": f"Acceptance Smoke 123456abcdef {index}",
                }
            if scenario == "unexpected-player":
                rows["unexpected"] = {**rows["real"], "event_slug": slug}
        return {"record_id": record_id, "fields": settings.copy()}

    def delete_checkin(record_id):
        assert rows[record_id]["event_slug"] == slug
        assert record_id.startswith("synthetic-")
        if scenario == "delete-failure":
            raise RuntimeError("synthetic failure")
        del rows[record_id]
        return True

    class Connection:
        def transaction(self):
            return contextlib.nullcontext()

        def execute(self, query, params=None):
            if query.startswith("SELECT pg_advisory"):
                result = [(None,)]
            elif query.startswith("SELECT id FROM settings"):
                result = [(1,)]
            elif query.startswith("SELECT active_event_slug"):
                result = [(settings["active_event_slug"],)]
            elif query.startswith("SELECT EXISTS"):
                result = [(False,)]
            elif query.startswith("SELECT record_id"):
                result = [
                    (key, row["tag"], row["email"], row["name"])
                    for key, row in rows.items()
                    if row["event_slug"] == params[0]
                ]
            else:
                pytest.fail("Unexpected helper query")
            return SimpleNamespace(fetchone=lambda: result[0], fetchall=lambda: result)

    class Pool:
        def connection(self):
            return contextlib.nullcontext(Connection())

        def close(self):
            pass

    storage = SimpleNamespace(
        DATABASE_URL="dummy-test-configuration",
        _pool=None,
        get_active_settings_with_id=lambda: {"record_id": "1", "fields": settings.copy()},
        update_settings=update_settings,
        delete_checkin=delete_checkin,
    )
    fake_signal = SimpleNamespace(
        SIGALRM=1, SIGTERM=2, signal=lambda *args: None, alarm=lambda *args: None
    )
    real_import = builtins.__import__

    def imported(name, *args, **kwargs):
        if name == "shared":
            return SimpleNamespace(postgres_api=storage)
        if name == "psycopg_pool":
            return SimpleNamespace(ConnectionPool=lambda *args, **kwargs: Pool())
        if name == "signal":
            return fake_signal
        return real_import(name, *args, **kwargs)

    messages = [json.dumps({"action": "setup", "slug": slug})]
    if scenario != "eof":
        messages.append(json.dumps({"action": "finish"}))
    monkeypatch.setattr(smoke.sys, "stdin", io.StringIO("\n".join(messages) + "\n"))
    monkeypatch.setenv("INTEGRATION_ENGINE", "none")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(
            compile(smoke.DEV_HELPER, "<mocked-dev-helper>", "exec"),
            {"__builtins__": {**vars(builtins), "__import__": imported}},
        )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == {"ok": True}
    assert replies[-1]["restored"] is True
    assert replies[-1]["cleanup_ok"] is (scenario in {"finish", "eof"})
    assert settings == original
    assert rows["real"]["event_slug"] == "existing-event"
    if scenario == "unexpected-player":
        assert rows["unexpected"]["tag"] == "real"
    assert "Existing Player" not in output.getvalue()


@pytest.mark.parametrize("bad_internal_status", [None, 401, 405])
def test_dev_profile_oauth_dash_and_only_explicit_sse_exception(bad_internal_status):
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/admin/":
            return httpx.Response(302, headers={**HEADERS, "Location": "/admin/auth/login"})
        if path in {"/api/events/stream", "/admin/api/events/stream"}:
            return httpx.Response(401, headers=HEADERS)
        if path in smoke.DEV_DASH_PATHS.values():
            return httpx.Response(302, headers={**HEADERS, "Location": "/admin/auth/login"})
        if path in smoke.INTERNAL_PATHS.values():
            return httpx.Response(bad_internal_status or 404, headers=HEADERS)
        return httpx.Response(200, headers=HEADERS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checks = smoke.readonly(client, DEV, profile="dev")
    assert all(request.method == "GET" for request in requests)
    assert any(check.name == "DEV admin OAuth" and check.passed is True for check in checks)
    assert any(check.name == "DEV Dash SSE token gate" and check.passed is True for check in checks)
    assert any(check.name == "admin SSE token gate" and check.passed is True for check in checks)
    assert any(check.passed is False for check in checks) is (bad_internal_status is not None)


def test_prod_profile_rejects_public_token_gate_and_admin_sse_cookie():
    def handler(request):
        if request.url.host == "admin.fgctrollhattan.se":
            return httpx.Response(
                401,
                headers={
                    **HEADERS,
                    **(
                        {"Set-Cookie": "private-cookie=never-send"}
                        if request.url.path == "/api/events/stream"
                        else {}
                    ),
                },
            )
        private = {*smoke.INTERNAL_PATHS.values(), *smoke.DEV_DASH_PATHS.values()}
        status = (
            401
            if request.url.path == "/api/events/stream"
            else 404 if request.url.path in private else 200
        )
        return httpx.Response(status, headers=HEADERS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checks = smoke.readonly(
            client, "https://www.checkin.fgctrollhattan.se", admin_base=ADMIN, profile="prod"
        )
    failed = {check.name for check in checks if check.passed is False}
    assert failed == {"deny public SSE", "admin SSE token gate"}


@pytest.mark.parametrize(
    "body,accepted",
    [
        ("<a href='/admin/auth/login'>Login with Start.gg</a>", True),
        ('<a href="/auth/login">Login</a>', True),
        ("<p>/auth/login</p>", False),
        ("<a href='https://outside.invalid/auth/login'>Login</a>", False),
        ("<a href='/auth/login'>Login</a><script id='_dash-config'>private</script>", False),
        ("<a href='/auth/login'>Login</a><div id='sse-token-store'>private</div>", False),
        (
            "<a href='/auth/login'>Login</a><table id='players-table'><tr>private</tr></table>",
            False,
        ),
        ('<a href="/auth/login">Login</a><pre>{"checkin_id":"private"}</pre>', False),
        ("<a href='/auth/login'>Login</a><div id='&#95;dash-app-content'>private</div>", False),
    ],
)
def test_dev_admin_200_only_accepts_anonymous_landing_without_data(body, accepted, capsys):
    def handler(request):
        path = request.url.path
        if path == "/admin/":
            return httpx.Response(200, headers=HEADERS, text=body)
        if path in {"/api/events/stream", "/admin/api/events/stream"}:
            return httpx.Response(401, headers=HEADERS)
        if path in smoke.INTERNAL_PATHS.values():
            return httpx.Response(404, headers=HEADERS)
        return httpx.Response(200, headers=HEADERS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checks = smoke.readonly(client, DEV, profile="dev")
    landing = next(check for check in checks if check.name == "DEV admin OAuth")
    assert landing.passed is accepted
    smoke.print_checks(checks)
    assert "private" not in capsys.readouterr().out

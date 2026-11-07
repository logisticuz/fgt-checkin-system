"""Public routing contracts, independent of Docker and provider credentials."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r'''\s+|\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]|[^\s{};]+''')


def parse(text):
    """Parse directive/block boundaries, preserving quoted header values."""
    text = re.sub(r"\$\{[A-Z_]+\}", "SYNTHETIC_ENV_VALUE", text)
    tokens = [
        m.group()
        for m in TOKEN.finditer(text)
        if not m.group().isspace() and not m.group().startswith("#")
    ]
    root, stack, words = [], [], []
    current = root
    for token in tokens:
        if token == "{":
            children = []
            current.append((words, children))
            stack.append(current)
            current, words = children, []
        elif token == "}":
            assert not words
            current = stack.pop()
        elif token == ";":
            current.append((words, []))
            words = []
        else:
            words.append(token.strip('"'))
    assert not stack and not words
    return root


def directives(block, name):
    return [words[1:] for words, _ in block if words[0] == name]


def server(config, hostname):
    return next(
        children
        for words, children in config
        if words == ["server"] and [hostname] in directives(children, "server_name")
    )


def location(block, path):
    """NGINX exact, longest prefix, then ordered regex location selection."""
    path = path.split("?", 1)[0]
    prefixes, regexes = [], []
    for words, children in block:
        if words[0] != "location":
            continue
        args = words[1:]
        if args[0] == "=":
            if args[1] == path:
                return children
            continue
        if args[0] in ("~", "~*"):
            regexes.append((args[1], children, args[0] == "~*"))
        else:
            prefix = args[-1]
            if path.startswith(prefix):
                prefixes.append((len(prefix), args[0] == "^~", children))
    prefix = max(prefixes, key=lambda item: item[0], default=None)
    if prefix and prefix[1]:
        return prefix[2]
    for pattern, children, insensitive in regexes:
        if re.search(pattern, path, re.I if insensitive else 0):
            return children
    return prefix[2] if prefix else []


@pytest.fixture(params=["nginx.conf", "nginx.dev.conf"])
def public(request):
    config = parse((ROOT / request.param).read_text(encoding="utf-8"))
    hostname = "www.checkin.fgctrollhattan.se" if request.param == "nginx.conf" else "localhost"
    return request.param, config, server(config, hostname)


PUBLIC_PATHS = [
    "/",
    "/kiosk",
    "/register",
    "/register.html",
    "/health",
    "/status/Synthetic%20Player",
    "/static/js/membership_v2.js",
    "/api/checkin/orchestrate",
    "/api/participant/Synthetic%20Player/status",
    "/api/checkin/synthetic-id/status?t=synthetic",
    "/api/ebas/register",
    "/api/player/games",
    "/api/membership/start",
    "/api/membership/verify",
    "/api/membership/resend",
    "/api/membership/synthetic-id/status",
    "/member/confirm/synthetic-token",
]
PRIVATE_PATHS = [
    "/api/checkin/begin",
    "/api/integration/result",
    "/api/checkin/synthetic/member-status",
    "/api/notify/update",
    "/api/notify/anything",
    "/n8n/webhook",
    "/n8n/webhook/callback",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/player/member",
    "/api/player/games/extra",
    "/api/ebas/validate",
    "/api/membership/internal",
    "/api/checkin/orchestrate/extra",
    "/auth/select-event",
    "/auth/login",
    "/api/admin/state",
    "/not-allowlisted",
]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_routes_proxy_in_every_membership_mode(public, path):
    assert directives(location(public[2], path), "proxy_pass") == [["http://backend:8000"]]


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_routes_never_reach_backend(public, path):
    selected = location(public[2], path)
    assert directives(selected, "return") == [["404"]]
    assert not directives(selected, "proxy_pass")


def test_template_and_javascript_routes_are_public(public):
    sources = sorted((ROOT / "backend/templates").rglob("*.html")) + sorted(
        (ROOT / "backend/static").rglob("*.js")
    )
    checked = set()
    for source in sources:
        text = source.read_text(encoding="utf-8")
        # Resolve the concatenated membership poll URL and template placeholders.
        text = re.sub(
            r'"/api/membership/"\s*\+\s*encodeURIComponent\([^)]*\)\s*\+\s*"/status"',
            '"/api/membership/synthetic/status"',
            text,
        )
        text = re.sub(r"\{\{.*?\}\}", "synthetic", text, flags=re.S)
        text = re.sub(r"\$\{[^}]*\}", "synthetic", text)
        for path in re.findall(r'''["'`](/[^"'`\s<>]*)''', text):
            path = path.split("?", 1)[0]
            selected = location(public[2], path)
            assert directives(
                selected, "proxy_pass"
            ), f"{source.relative_to(ROOT)} calls blocked {path}"
            checked.add(path)
    assert "/api/checkin/orchestrate" in checked
    assert "/api/ebas/register" in checked
    assert "/api/membership/synthetic/status" in checked
    assert "/api/checkin/synthetic/status" in checked


def test_venue_write_budgets_and_polling_are_separate(public):
    _, config, block = public
    zones = {
        args[1].split(":")[0].removeprefix("zone="): args[2]
        for args in directives(config, "limit_req_zone")
    }
    assert zones == {
        "one": "rate=5r/s",
        "pages": "rate=5r/s",
        "webhook": "rate=10r/m",
        "checkin": "rate=60r/m",
        "assets": "rate=50r/s",
        "games": "rate=60r/m",
        "membership_start": "rate=10r/m",
        "membership_verify": "rate=20r/m",
        "membership_resend": "rate=5r/m",
        "membership_confirm": "rate=20r/m",
        "polling": "rate=30r/s",
        **({"dev_admin": "rate=50r/s"} if public[0] == "nginx.dev.conf" else {}),
    }
    assert all(args[0] == "$binary_remote_addr" for args in directives(config, "limit_req_zone"))
    assert directives(block, "limit_req_status") == [["429"]]
    assert directives(location(block, "/api/checkin/orchestrate"), "limit_req") == [
        ["zone=checkin", "burst=40", "nodelay"]
    ]
    assert directives(block, "limit_req") == [["zone=pages", "burst=100", "nodelay"]]
    for path, zone, burst in (
        ("/static/logo.png", "assets", 300),
        ("/api/ebas/register", "webhook", 30),
        ("/api/player/games", "games", 40),
    ):
        assert directives(location(block, path), "limit_req") == [
            [f"zone={zone}", f"burst={burst}", "nodelay"]
        ]
    for path, zone, burst in (
        ("/api/membership/start", "membership_start", 40),
        ("/api/membership/verify", "membership_verify", 40),
        ("/api/membership/resend", "membership_resend", 5),
        ("/member/confirm/token", "membership_confirm", 40),
    ):
        assert directives(location(block, path), "limit_req") == [
            [f"zone={zone}", f"burst={burst}", "nodelay"]
        ]
    for path in (
        "/api/participant/name/status",
        "/api/checkin/id/status",
        "/api/membership/id/status",
    ):
        assert directives(location(block, path), "limit_req")[0][0] == "zone=polling"
    retry_map = next(
        children
        for words, children in config
        if words == ["map", "$limit_req_status", "$nginx_retry_after"]
    )
    assert directives(retry_map, "REJECTED") == [["60"]]
    assert ["Retry-After", "$nginx_retry_after", "always"] in directives(block, "add_header")


def test_long_requests_have_margin_and_all_locations_inherit_headers(public):
    _, config, block = public
    for path in (
        "/api/checkin/orchestrate",
        "/api/ebas/register",
        "/api/membership/start",
        "/api/membership/verify",
        "/api/membership/resend",
        "/api/membership/id/status",
        "/member/confirm/token",
    ):
        assert (
            int(directives(location(block, path), "proxy_read_timeout")[0][0].removesuffix("s"))
            >= 90
        )

    def check_locations(nodes):
        for words, children in nodes:
            if words[0] == "location":
                assert not directives(
                    children, "add_header"
                ), "nginx 1.27.5 would discard inherited security headers/cookies"
            check_locations(children)

    for words, children in config:
        if words == ["server"]:
            check_locations(children)


def test_security_headers_and_tls_only_hsts(public):
    filename, config, _ = public
    names = (
        (
            "www.checkin.fgctrollhattan.se",
            "checkin.fgctrollhattan.se",
            "admin.fgctrollhattan.se",
            "www.admin.fgctrollhattan.se",
        )
        if filename == "nginx.conf"
        else ("localhost",)
    )
    for hostname in names:
        block = server(config, hostname)
        # The renewal HTTP server has the same name; select the TLS block explicitly.
        if filename == "nginx.conf":
            block = next(
                children
                for words, children in config
                if words == ["server"]
                and [hostname] in directives(children, "server_name")
                and ["443", "ssl"] in directives(children, "listen")
            )
        headers = {args[0]: args[1:] for args in directives(block, "add_header")}
        assert headers["X-Content-Type-Options"] == ["nosniff", "always"]
        assert headers["Referrer-Policy"] == ["same-origin", "always"]
        assert headers["X-Frame-Options"] == ["DENY", "always"]
        assert "Content-Security-Policy-Report-Only" in headers
        assert "Content-Security-Policy" not in headers
        assert ("Strict-Transport-Security" in headers) == (filename == "nginx.conf")
    for words, block in config:
        if words == ["server"] and any(args[0] == "80" for args in directives(block, "listen")):
            assert not any(
                args[0] == "Strict-Transport-Security" for args in directives(block, "add_header")
            )


def test_prod_auth_cookie_oauth_renewal_and_optional_siblings():
    text = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    config = parse(text)
    assert set(re.findall(r"\$\{([^}]+)\}", text)) == {"ADMIN_AUTH_COOKIE_TOKEN"}
    admin = server(config, "admin.fgctrollhattan.se")
    for path in ("/auth/select-event", "/auth/select-event/child", "/auth/select-event-extra"):
        selected = location(admin, path)
        assert directives(selected, "auth_basic") == [["$admin_auth_realm"]]
        assert directives(selected, "auth_basic_user_file") == [["/etc/nginx/.htpasswd"]]
    for path in (
        "/auth/login",
        "/auth/callback",
        "/auth/callback/finalize",
        "/auth/logout",
        "/auth/me",
    ):
        assert not directives(location(admin, path), "auth_basic")
        assert directives(location(admin, path), "proxy_pass")
    realm = next(
        children
        for words, children in config
        if words == ["map", "$admin_auth_cookie_ok", "$admin_auth_realm"]
    )
    assert directives(realm, "1") == [["off"]]
    assert directives(config, "resolver") == [["127.0.0.11", "valid=30s"]]
    for hostname, variable, upstream in (
        ("meetup.fgctrollhattan.se", "$meetup_upstream", "http://meetup:8000"),
        ("membercard.fgctrollhattan.se", "$membercard_upstream", "http://member-card:8002"),
    ):
        sibling = next(
            children
            for words, children in config
            if words == ["server"] and [hostname] in directives(children, "server_name")
        )
        selected = location(sibling, "/")
        assert directives(selected, "set") == [[variable, upstream]]
        assert directives(selected, "proxy_pass") == [[variable]]
        assert directives(sibling, "limit_req") == [["zone=one", "burst=30", "nodelay"]]
    renewal = next(
        children
        for words, children in config
        if words == ["server"] and ["80"] in directives(children, "listen")
    )
    assert len(directives(renewal, "server_name")[0]) == 6
    assert directives(location(renewal, "/.well-known/acme-challenge/synthetic"), "root") == [
        ["/var/www/certbot"]
    ]


def test_dev_only_dash_exceptions_never_leak_to_prod():
    prod = server(
        parse((ROOT / "nginx.conf").read_text(encoding="utf-8")), "www.checkin.fgctrollhattan.se"
    )
    dev = server(parse((ROOT / "nginx.dev.conf").read_text(encoding="utf-8")), "localhost")
    for path in (
        "/admin/",
        "/_dash-layout",
        "/_dash-dependencies",
        "/_dash-update-component",
        "/_dash-component-suites/dash.js",
        "/_reload-hash",
        "/_favicon.ico",
        "/assets/styles.css",
        "/api/events/stream",
    ):
        assert directives(location(prod, path), "return") == [["404"]]
        assert directives(location(dev, path), "proxy_pass")
    assert not directives(location(dev, "/admin/auth/select-event"), "auth_basic")
    assert directives(location(dev, "/admin/auth/select-event"), "proxy_pass")


@pytest.mark.parametrize(
    "path,user,status,issues_cookie",
    [
        ("/", "synthetic", 200, True),
        ("/auth/select-event", "synthetic", 302, True),
        ("/auth/select-event/child", "synthetic", 200, True),
        ("/auth/select-event", "synthetic", 401, False),
        ("/", "synthetic", 403, False),
        ("/", "synthetic", 500, False),
        ("/", "", 200, False),
        ("/auth/login", "synthetic", 302, False),
        ("/auth/callback/finalize", "synthetic", 200, False),
        ("/auth/me", "synthetic", 200, False),
        ("/api/events/stream", "synthetic", 200, False),
        ("/api/events/stream/child", "synthetic", 200, False),
    ],
)
def test_bypass_cookie_requires_success_on_a_basic_gated_route(path, user, status, issues_cookie):
    config = parse((ROOT / "nginx.conf").read_text(encoding="utf-8"))

    def map_value(block, key):
        default = ""
        for words, _ in block:
            if words[0] == "default":
                default = words[1]
            elif words[0].startswith("~") and re.search(words[0][1:], key):
                return words[1]
            elif words[0] == key:
                return words[1]
        return default

    path_map = next(
        children
        for words, children in config
        if words == ["map", "$uri", "$admin_basic_cookie_path"]
    )
    cookie_map = next(
        children
        for words, children in config
        if words
        == ["map", "$admin_basic_cookie_path:$remote_user:$status", "$admin_auth_set_cookie"]
    )
    eligible = map_value(path_map, path)
    assert bool(map_value(cookie_map, f"{eligible}:{user}:{status}")) == issues_cookie


def model_venue_requests(config, block, events):
    """Token capacity equivalent to nodelay leaky-bucket admission, for one source IP."""
    rates = {}
    for args in directives(config, "limit_req_zone"):
        quantity, period = args[2].removeprefix("rate=").split("r/")
        rates[args[1].split(":")[0].removeprefix("zone=")] = float(quantity) / (
            60 if period == "m" else 1
        )
    buckets, results = {}, []
    for now, path in sorted(events, key=lambda event: event[0]):
        limits = directives(location(block, path), "limit_req") or directives(block, "limit_req")
        args = limits[0]
        zone = args[0].removeprefix("zone=")
        capacity = (
            int(next(arg.removeprefix("burst=") for arg in args if arg.startswith("burst="))) + 1
        )
        previous, tokens = buckets.get(zone, (now, capacity))
        tokens = min(capacity, tokens + (now - previous) * rates[zone])
        accepted = tokens >= 1
        buckets[zone] = (now, tokens - 1 if accepted else tokens)
        results.append(200 if accepted else 429)
    return results


def test_thirty_player_rush_model_has_no_rejections(public):
    _, config, block = public
    events = []
    for player in range(30):
        # Three pages, ten assets, check-in/games plus v2 start/verify, and polls every10s.
        for path in ("/kiosk", "/register", f"/status/Synthetic{player}"):
            events.append((0, path))
        events.extend((0, f"/static/logo.png?asset={asset}") for asset in range(10))
        events.extend(
            (0, path)
            for path in (
                "/api/checkin/orchestrate",
                "/api/player/games",
                "/api/membership/start",
                "/api/membership/verify",
            )
        )
        for tick in (0, 10, 20):
            events.extend(
                (tick, path)
                for path in (
                    f"/api/checkin/id{player}/status",
                    f"/api/participant/Synthetic{player}/status",
                    f"/api/membership/id{player}/status",
                )
            )
    assert len(events) == 780
    assert model_venue_requests(config, block, events) == [200] * len(events)


def test_legacy_probe_at_one_hundred_per_minute_is_rejected_without_affecting_games(public):
    _, config, block = public
    probe = [(attempt * 0.6, "/api/ebas/register") for attempt in range(100)]
    statuses = model_venue_requests(config, block, probe)
    assert 429 in statuses
    assert statuses.count(200) <= 41  # Initial burst plus at most ten new admissions per minute.
    games = [(60, "/api/player/games")] * 30
    assert model_venue_requests(config, block, probe + games)[-30:] == [200] * 30


def test_sibling_budgets_stay_separate_from_checkin_pages():
    config = parse((ROOT / "nginx.conf").read_text(encoding="utf-8"))
    for hostname in ("meetup.fgctrollhattan.se", "membercard.fgctrollhattan.se"):
        assert directives(server(config, hostname), "limit_req") == [
            ["zone=one", "burst=30", "nodelay"]
        ]


def test_dev_dash_requests_have_independent_generous_capacity():
    config = parse((ROOT / "nginx.dev.conf").read_text(encoding="utf-8"))
    block = server(config, "localhost")
    pages = [(0, "/kiosk")] * 200
    callbacks = [(0, "/_dash-update-component")] * 200
    assert 429 in model_venue_requests(config, block, pages)
    assert model_venue_requests(config, block, pages + callbacks)[-200:] == [200] * 200
    for path in (
        "/admin/",
        "/admin/auth/select-event",
        "/admin/api/events/stream",
        "/_dash-layout",
        "/_dash-dependencies",
        "/_dash-component-suites/dash.js",
        "/_reload-hash",
        "/_favicon.ico",
        "/assets/style.css",
        "/api/events/stream",
    ):
        assert directives(location(block, path), "limit_req") == [
            ["zone=dev_admin", "burst=300", "nodelay"]
        ]

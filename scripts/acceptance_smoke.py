"""Acceptance checks: credential-free readonly probes and guarded synthetic DEV writes."""

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

import httpx

LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}
DEV_CONTAINER = "fgt-dev-backend-1"
DEV_GAME = "Acceptance Smoke Game"
# Runs only inside the fixed DEV container. A persistent stdin session keeps the
# snapshot in memory and restores it on finish, EOF, timeout or a normal signal.
DEV_HELPER = r'''
import contextlib
import json
import os
import re
import signal
import sys

pool = storage = None
prepared = restored = False
snapshot = settings_id = slug = None

def guard():
    if os.environ.get("INTEGRATION_ENGINE") != "none":
        raise RuntimeError("DEV engine guard")

def emit(ok, **extra):
    print(json.dumps({"ok": bool(ok), **extra}), flush=True)

class BorrowedPool:
    def __init__(self, connection):
        self.connection_value = connection
    @contextlib.contextmanager
    def connection(self):
        yield self.connection_value

@contextlib.contextmanager
def transaction():
    guard()
    with pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (slug,))
        connection.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
        previous = storage._pool
        storage._pool = BorrowedPool(connection)
        try:
            yield connection
        finally:
            storage._pool = previous

def cleanup_and_restore(connection):
    guard()
    clean = True
    token = slug.rsplit("-", 1)[1]
    try:
        with connection.transaction():
            rows = connection.execute(
                "SELECT record_id, tag, email, name FROM active_event_data WHERE event_slug=%s FOR UPDATE",
                (slug,),
            ).fetchall()
            for record_id, tag, email, name in rows:
                owned = any(
                    tag == f"acc-{token}-{index}"
                    and email == f"acceptance-{token}-{index}@example.invalid"
                    and name == f"Acceptance Smoke {token} {index}"
                    for index in range(1, 4)
                )
                if owned:
                    guard()
                    if not storage.delete_checkin(record_id):
                        raise RuntimeError("Synthetic cleanup failed")
                else:
                    clean = False  # Never delete a different player in this event.
    except Exception:
        clean = False
    finally:
        guard()
        if not storage.update_settings(settings_id, snapshot):
            raise RuntimeError("Settings restoration failed")
    return clean

def interrupted(signum, frame):
    raise TimeoutError("DEV fixture interrupted")

try:
    guard()  # Before importing storage, opening a pool or executing any write.
    message = json.loads(sys.stdin.readline())
    slug = message.get("slug")
    if message.get("action") != "setup" or not isinstance(slug, str) or not re.fullmatch(r"acceptance-[0-9]{14}-[a-f0-9]{12}", slug):
        raise RuntimeError("Invalid synthetic event")
    from shared import postgres_api as storage
    from psycopg_pool import ConnectionPool
    # Preseed the pool: the helper must not run application startup migrations.
    pool = ConnectionPool(storage.DATABASE_URL, min_size=1, max_size=2, timeout=5,
        kwargs={"autocommit": True, "connect_timeout": 5,
            "options": "-c lock_timeout=5000 -c statement_timeout=10000"})
    storage._pool = pool
    changes = {
        "active_event_slug": slug, "event_display_name": "Acceptance Smoke",
        "require_membership": False, "require_startgg": False,
        "require_payment": True, "swish_expected_per_game": 25,
        "default_game": ["Acceptance Smoke Game"], "events_json": [],
        "offer_membership": False, "collect_acquisition_source": False,
    }
    with transaction() as connection:
        row = connection.execute("SELECT id FROM settings WHERE is_active=true ORDER BY id DESC LIMIT 1 FOR UPDATE").fetchone()
        current = storage.get_active_settings_with_id()
        if not row or not current or str(row[0]) != current["record_id"]:
            raise RuntimeError("Active settings missing")
        exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM active_event_data WHERE event_slug=%s UNION ALL SELECT 1 FROM event_archive WHERE event_slug=%s UNION ALL SELECT 1 FROM event_stats WHERE event_slug=%s)",
            (slug, slug, slug),
        ).fetchone()[0]
        if exists:
            raise RuntimeError("Synthetic event collision")
        settings_id = current["record_id"]
        snapshot = {key: current["fields"].get(key) for key in changes}
        guard()
        prepared = True  # Even an ambiguous setup commit must attempt restoration.
        if not storage.update_settings(settings_id, changes):
            raise RuntimeError("Could not set synthetic event")
    overall = True
    with transaction() as connection:
        row = connection.execute("SELECT active_event_slug FROM settings WHERE id=%s FOR UPDATE", (int(settings_id),)).fetchone()
        if not row or row[0] != slug:
            raise RuntimeError("Active event changed before smoke")
        # Reads continue seeing the committed synthetic event, while concurrent
        # settings writes wait until the snapshot is restored in this transaction.
        signal.signal(signal.SIGALRM, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        signal.alarm(120)
        emit(True)
        try:
            for line in sys.stdin:
                guard()
                message = json.loads(line)
                if message.get("action") == "finish":
                    break
                if message.get("action") != "verify":
                    raise RuntimeError("Invalid helper operation")
                record_id = message.get("checkin_id")
                if record_id is not None:
                    record = storage.get_checkin_by_record_id(record_id)
                    fields = (record or {}).get("fields", {})
                    token = slug.rsplit("-", 1)[1]
                    if fields.get("event_slug") != slug or not any(
                        fields.get("tag") == f"acc-{token}-{index}"
                        and fields.get("email") == f"acceptance-{token}-{index}@example.invalid"
                        and fields.get("name") == f"Acceptance Smoke {token} {index}"
                        for index in range(1, 4)
                    ):
                        raise RuntimeError("Row does not belong to this smoke")
                emit(True)
        except BaseException:
            overall = False
        finally:
            signal.alarm(0)
            clean = cleanup_and_restore(connection)
    restored = True
    emit(overall and clean, done=True, cleanup_ok=clean, restored=True)
except BaseException:
    clean = not prepared
    if prepared and not restored:
        try:
            with transaction() as connection:
                row = connection.execute("SELECT active_event_slug FROM settings WHERE id=%s FOR UPDATE", (int(settings_id),)).fetchone()
                if not row or row[0] != slug:
                    raise RuntimeError("Refuse to overwrite another active event")
                clean = cleanup_and_restore(connection)
            restored = True
        except BaseException:
            clean = False
    emit(False, done=True, cleanup_ok=clean, restored=restored or not prepared)
finally:
    if pool is not None:
        pool.close()
'''
INTERNAL_PATHS = {
    "begin": "/api/checkin/begin",
    "integration result": "/api/integration/result",
    "member callback": "/api/checkin/acceptance-smoke/member-status",
    "notify update": "/api/notify/update",
    "notify checkin": "/api/notify/checkin",
    "registered count": "/api/startgg/registered-count",
    "public SSE": "/api/events/stream",
    "API docs": "/docs",
    "OpenAPI": "/openapi.json",
    "n8n webhook": "/n8n/webhook/acceptance-smoke",
}
PUBLIC_PATHS = {
    "health": "/health",
    "checkin page": "/",
    "kiosk page": "/kiosk",
    "register page": "/register",
    "status page": "/status/acceptance-smoke-no-player",
    "static asset": "/static/js/validation.js",
}
DEV_DASH_PATHS = {
    "Dash layout": "/_dash-layout",
    "Dash dependencies": "/_dash-dependencies",
    "Dash stylesheet": "/assets/styles.css",
    "Dash favicon": "/_favicon.ico",
}


class SmokeError(Exception):
    """A fixed, safe error message; never include response data or request secrets."""


@dataclass
class Check:
    name: str
    passed: bool | None
    detail: str = ""


def base_url(value, *, loopback=False, prefix=False):
    try:
        parts = urlsplit(value)
        port = parts.port
    except (TypeError, ValueError):
        raise SmokeError("Invalid HTTP base URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (not prefix and parts.path not in {"", "/"})
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise SmokeError("Base must be an HTTP origin without credentials, query or fragment")
    if loopback and parts.hostname.lower() not in LOOPBACK_HOSTS:
        raise SmokeError("DEV writes require localhost or 127.0.0.1")
    if port == 0:
        raise SmokeError("Invalid HTTP port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class HTTP:
    def __init__(self, client, *, dev_write=False):
        self.client = client
        self.dev_write = dev_write
        self.write_enabled = False

    def request(self, method, url, *, json=None, redirects=False):
        method = method.upper()
        writing = method not in {"GET", "HEAD"}
        if writing and not (self.dev_write and self.write_enabled):
            raise SmokeError("Writes are disabled")
        if writing:
            base_url(str(httpx.URL(url).copy_with(path="/", query=None)), loopback=True)
        original = httpx.URL(url)
        allowed_hosts = {original.host}
        if not self.dev_write and original.host == "checkin.fgctrollhattan.se":
            allowed_hosts.add("www." + original.host)
        history = []
        for _ in range(4):
            # Discard server cookies and client defaults, including injected test-client auth.
            self.client.cookies.clear()
            request = self.client.build_request(method, url, json=json)
            for header in ("authorization", "proxy-authorization", "cookie"):
                request.headers.pop(header, None)
            try:
                response = self.client.send(request, follow_redirects=False, auth=None)
            except httpx.HTTPError:
                raise SmokeError("HTTP request unavailable") from None
            if not redirects or not response.is_redirect:
                response.history = history
                return response
            if writing or method not in {"GET", "HEAD"}:
                raise SmokeError("Write redirects are forbidden")
            try:
                target = response.url.join(response.headers["location"])
                base_url(str(target.copy_with(path="/", query=None, fragment=None)))
            except (KeyError, ValueError, httpx.InvalidURL, SmokeError):
                raise SmokeError("Invalid redirect") from None
            if (
                target.host not in allowed_hosts
                or target.query
                or target.port != original.port
                and not (original.scheme == "http" and original.port == 80 and target.port == 443)
                or original.scheme == "https"
                and target.scheme != "https"
            ):
                raise SmokeError("Redirect outside the declared origin")
            history.append(response)
            url = str(target)
        raise SmokeError("Too many redirects")


def security_headers(response):
    for hop in [*response.history, response]:
        headers = hop.headers
        if (
            headers.get("x-content-type-options", "").lower() != "nosniff"
            or headers.get("referrer-policy", "").lower() != "same-origin"
            or headers.get("x-frame-options", "").upper() != "DENY"
            or not headers.get("content-security-policy-report-only")
        ):
            return False
        if hop.url.scheme == "https":
            age = re.search(
                r"(?:^|;)\s*max-age=([0-9]+)", headers.get("strict-transport-security", "")
            )
            if not age or int(age[1]) == 0:
                return False
    return True


def anonymous_login_landing(response):
    markers = (
        "_dash-",
        "dash_renderer",
        "dash_table",
        "dash-table",
        "datatable",
        "players-table",
        "sse-token-store",
        "requirements-store",
        "events-map-store",
        "checkin_id",
        "player_uuid",
        "payment_valid",
    )

    class Landing(HTMLParser):
        login = False
        data = False

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "table"}:
                self.data = True
            for _, value in attrs:
                if value and any(marker in value.casefold() for marker in markers):
                    self.data = True
            href = dict(attrs).get("href")
            if tag != "a" or not href:
                return
            try:
                target = response.url.join(href)
            except httpx.InvalidURL:
                return
            if (
                target.host == response.url.host
                and target.port == response.url.port
                and target.scheme == response.url.scheme
                and not target.userinfo
                and target.path in {"/auth/login", "/admin/auth/login"}
            ):
                self.login = True

    body = response.text
    parser = Landing()
    parser.feed(body)
    return (
        parser.login
        and not parser.data
        and not any(marker in body.casefold() for marker in markers)
    )


def readonly(
    client, base, *, admin_base=None, meetup_base=None, membercard_base=None, profile="prod"
):
    if profile not in {"dev", "prod"}:
        raise SmokeError("Unknown acceptance profile")
    base = base_url(base, loopback=profile == "dev")
    if admin_base is None:
        if urlsplit(base).hostname not in LOOPBACK_HOSTS:
            raise SmokeError("Readonly checks require --admin-base for a non-local public host")
        admin_base = base + "/admin"
    admin_base = base_url(admin_base, prefix=True, loopback=profile == "dev")
    siblings = {
        "meetup": base_url(meetup_base) if meetup_base else None,
        "membercard": base_url(membercard_base) if membercard_base else None,
    }
    http = HTTP(client)
    results = []

    def probe(name, origin, path, expected, *, headers=True, admin=False, oauth=False):
        try:
            response = http.request("GET", origin + path, redirects=not (admin or oauth))
            statuses = expected if isinstance(expected, tuple) else (expected,)
            passed = response.status_code in statuses
            detail = f"HTTP {response.status_code}; expected " + "/".join(map(str, statuses))
            if headers and not security_headers(response):
                passed, detail = False, detail + "; security headers missing/invalid"
            if admin and response.headers.get_list("set-cookie"):
                passed, detail = False, detail + "; unexpected Set-Cookie"
            if oauth:
                if response.headers.get("www-authenticate"):
                    passed, detail = False, detail + "; unexpected Basic/auth gate"
                if response.status_code == 302:
                    try:
                        target = response.url.join(response.headers.get("location", ""))
                    except httpx.InvalidURL:
                        raise SmokeError("Invalid OAuth redirect") from None
                    if (
                        target.host != response.url.host
                        or target.port != response.url.port
                        or target.scheme != response.url.scheme
                        or target.path != "/admin/auth/login"
                    ):
                        passed, detail = False, detail + "; unexpected OAuth redirect"
                elif (
                    admin and response.status_code == 200 and not anonymous_login_landing(response)
                ):
                    passed, detail = False, detail + "; not an anonymous login landing page"
            results.append(Check(name, passed, detail))
        except SmokeError as error:
            results.append(Check(name, False, str(error)))

    for name, path in PUBLIC_PATHS.items():
        probe(name, base, path, 200)
    for name, path in INTERNAL_PATHS.items():
        if profile == "dev" and path == "/api/events/stream":
            continue
        probe("deny " + name, base, path, 404)
    if profile == "dev":
        probe("DEV Dash SSE token gate", base, "/api/events/stream", 401, admin=True)
        probe("DEV admin OAuth", admin_base, "/", (200, 302), admin=True, oauth=True)
        for name, path in DEV_DASH_PATHS.items():
            probe(name, base, path, (200, 302), oauth=True)
    else:
        probe("admin anonymous", admin_base, "/auth/select-event", 401, admin=True)
        for name, path in DEV_DASH_PATHS.items():
            probe("deny " + name, base, path, 404)
    probe("admin SSE token gate", admin_base, "/api/events/stream", 401, admin=True)
    for name, origin in siblings.items():
        if origin:
            probe(name, origin, "/", 200, headers=False)
        else:
            results.append(Check(name, None, "optional host not requested"))
    return results


def dev_guard(client, base):
    base = base_url(base, loopback=True)
    http = HTTP(client, dev_write=True)
    response = http.request("GET", base + "/health")
    try:
        engine = response.json()["components"]["integration_engine"]
    except (KeyError, TypeError, ValueError):
        raise SmokeError("DEV health must explicitly report integration_engine=none") from None
    if response.status_code != 200 or engine != "none":
        raise SmokeError("DEV health must explicitly report integration_engine=none")
    http.write_enabled = True
    return http


class DockerFixture:
    def __init__(self, container=DEV_CONTAINER):
        if container != DEV_CONTAINER:
            raise SmokeError("Only the fixed DEV backend container is permitted")
        self.container = container
        self.process = None
        self.messages = queue.Queue()
        self.final = None

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SmokeError("DEV fixture connection closed") from None

    def _receive(self):
        try:
            line = self.messages.get(timeout=20)
            message = json.loads(line) if line is not None else None
            if not isinstance(message, dict) or type(message.get("ok")) is not bool:
                raise ValueError
        except (queue.Empty, TypeError, ValueError):
            raise SmokeError("DEV fixture reply unavailable") from None
        if message.get("done") is True:
            self.final = message
        return message

    def setup(self, slug):
        try:
            self.process = subprocess.Popen(
                ["docker", "exec", "-i", self.container, "python", "-u", "-c", DEV_HELPER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        except OSError:
            raise SmokeError("DEV Docker helper unavailable") from None
        threading.Thread(target=self._read, daemon=True).start()
        self._send({"action": "setup", "slug": slug})
        if not self._receive()["ok"]:
            raise SmokeError("DEV fixture setup refused")

    def verify(self, checkin_id=None):
        self._send({"action": "verify", "checkin_id": checkin_id})
        if not self._receive()["ok"]:
            raise SmokeError("DEV fixture ownership check failed")

    def finish(self):
        if self.process is None:
            return
        try:
            if self.final is None and self.process.poll() is None:
                self._send({"action": "finish"})
            while self.final is None:
                self._receive()
        finally:
            self.process.stdin.close()  # EOF also triggers the helper's finally path.
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                raise SmokeError("DEV settings restoration not confirmed") from None
        if not (self.final.get("cleanup_ok") is True and self.final.get("restored") is True):
            raise SmokeError("DEV cleanup or settings restoration failed")


def dev_write(client, base, *, admin_base=None, fixture=None):
    # Validate every write origin before starting the helper or any HTTP write.
    base = base_url(base, loopback=True)
    admin_base = base_url(admin_base or "http://127.0.0.1:8001", loopback=True)
    http = dev_guard(client, base)
    admin = dev_guard(client, admin_base)
    fixture = fixture or DockerFixture()
    token = uuid.uuid4().hex[:12]
    slug = "acceptance-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + token
    results = [Check("DEV guards", True, "loopback; both engines none")]
    setup_attempted = False

    def require_json(response, *, expected=200):
        if response.status_code != expected:
            raise SmokeError("Synthetic flow returned an unexpected HTTP status")
        try:
            data = response.json()
        except ValueError:
            raise SmokeError("Synthetic flow returned invalid JSON") from None
        if not isinstance(data, dict):
            raise SmokeError("Synthetic flow returned invalid JSON")
        return data

    def status(checkin_id, capability):
        for attempt in range(3):
            data = require_json(
                http.request("GET", base + f"/api/checkin/{checkin_id}/status?t={capability}")
            )
            if data.get("found") is True:
                return data
            if attempt < 2:
                time.sleep(0.2)
        raise SmokeError("Synthetic status did not become available")

    try:
        setup_attempted = True
        fixture.setup(slug)
        results.append(Check("synthetic event", True, "isolated settings snapshot active"))
        for index in range(1, 4):
            fixture.verify()
            dev_guard(client, base)
            payload = {
                "name": f"Acceptance Smoke {token} {index}",
                "tag": f"acc-{token}-{index}",
                "email": f"acceptance-{token}-{index}@example.invalid",
                "telephone": "0000000000",
            }
            data = require_json(
                http.request("POST", base + "/api/checkin/orchestrate", json=payload)
            )
            checkin_id, capability = data.get("checkin_id"), data.get("status_token")
            if (
                data.get("slug") != slug
                or not isinstance(checkin_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", checkin_id)
                or not isinstance(capability, str)
                or not re.fullmatch(r"[A-Za-z0-9_.=-]+", capability)
            ):
                raise SmokeError("Synthetic check-in authority was not confirmed")
            fixture.verify(checkin_id)
            status(checkin_id, capability)
            saved = require_json(
                http.request(
                    "PATCH",
                    base + "/api/player/games",
                    json={
                        "checkin_id": checkin_id,
                        "status_token": capability,
                        "games": [DEV_GAME],
                    },
                )
            )
            if saved.get("success") is not True or status(checkin_id, capability).get("games") != [
                DEV_GAME
            ]:
                raise SmokeError("Synthetic game save was not confirmed")
            results.append(Check(f"player {index} flow", True, "orchestrate; signed status; games"))
            fixture.verify(checkin_id)
            dev_guard(client, admin_base)
            payment = admin.request(
                "PATCH", admin_base + f"/players/{checkin_id}/payment", json={"payment_valid": True}
            )
            if payment.status_code in {404, 405}:
                results.append(
                    Check(f"player {index} payment", None, "optional admin API not exposed")
                )
            else:
                paid = require_json(payment)
                final = status(checkin_id, capability)
                if (
                    paid.get("success") is not True
                    or paid.get("payment_valid") is not True
                    or final.get("payment_ok") is not True
                    or final.get("ready") is not True
                ):
                    raise SmokeError("Synthetic TO payment was not confirmed")
                results.append(Check(f"player {index} payment", True, "TO toggle; canonical ready"))
    except SmokeError as error:
        results.append(Check("synthetic flow", False, str(error)))
    finally:
        if setup_attempted:
            try:
                fixture.finish()
                results.append(Check("cleanup + restore", True, "own rows only; settings restored"))
            except SmokeError as error:
                results.append(Check("cleanup + restore", False, str(error)))
    return results


def print_checks(results):
    print(f"{'CHECK':<24} {'RESULT':<6} DETAIL")
    for check in results:
        result = "SKIP" if check.passed is None else "PASS" if check.passed else "FAIL"
        print(f"{check.name:<24} {result:<6} {check.detail}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("readonly", "dev-write"))
    parser.add_argument(
        "--profile",
        choices=("dev", "prod"),
        help="Defaults to prod for readonly and dev for dev-write",
    )
    parser.add_argument("--base", required=True)
    parser.add_argument("--admin-base")
    parser.add_argument("--meetup-base")
    parser.add_argument("--membercard-base")
    args = parser.parse_args(argv)
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            if args.mode == "readonly":
                results = readonly(
                    client,
                    args.base,
                    admin_base=args.admin_base,
                    meetup_base=args.meetup_base,
                    membercard_base=args.membercard_base,
                    profile=args.profile or "prod",
                )
            else:
                if args.profile not in {None, "dev"}:
                    raise SmokeError("Write mode requires the DEV profile")
                results = dev_write(client, args.base, admin_base=args.admin_base)
    except SmokeError as error:
        results = [Check("safety guard", False, str(error))]
    print_checks(results)
    return int(any(check.passed is False for check in results))


if __name__ == "__main__":
    sys.exit(main())

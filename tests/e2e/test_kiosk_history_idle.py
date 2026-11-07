"""T-062: on the kiosk, Back never reopens the previous player's page, and register idles home.

Real templates + checkin_hygiene.js, mocked API (page.route), synthetic players only.
"""

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e

NAME, PHONE, TAG = "Synthetic Kiosk", "0700000201", "pw-kiosk"
ORCHESTRATE = {
    "ready": False,
    "missing": ["Start.gg"],
    "member": True,
    "startgg": False,
    "payment_valid": True,
    "slug": "synthetic-event",
    "checkin_id": "chk-k",
    "status_token": "tok-chk-k",
    "integrations": {"ebas": "ok", "startgg": "ok"},
    "degraded": False,
}
ID_STATUS = {
    "found": True,
    "ready": False,
    "missing": ["Start.gg"],
    "member": True,
    "startgg": False,
    "payment_ok": True,
    "games": [],
    "status": "Pending",
}
REGISTER_KIOSK = (
    "/register?name=Synthetic%20Kiosk&startgg=false&event=synthetic-event&tag=pw-kiosk"
    "&kiosk=true&checkin_id=chk-k&t=tok-chk-k"
)
STATUS_KIOSK = "/status/Synthetic%20Kiosk?kiosk=true&checkin_id=chk-k&t=tok-chk-k"


@pytest.fixture
def site(browser, app_templates):  # noqa: F811
    contexts, errors = [], []

    def open_site(
        flow="legacy", hold_games=False, clock=False, viewport=TABLET, require_membership=False
    ):
        calls = {"pages": [], "games": [], "held": [], "failed": [], "order": []}
        base = {
            "membership_flow": flow,
            "membership_year": 2026,
            "n8n_token": "",
            "sse_token": "",
            "collect_acquisition_source": False,
            "require_membership": require_membership,
            "require_payment": False,
            "require_startgg": True,
            "offer_membership": False,
            "is_member": False,
            "games": [{"id": "1", "name": "Street Fighter 6"}, {"id": "2", "name": "Tekken 8"}],
            "swish_number": "000 000 00 00",
            "swish_expected_per_game": 25,
            "tag": TAG,
            "event_slug": "synthetic-event",
            "event_name": "Synthetic Event",
            "status": {},
        }

        def reply(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def page_html(route, template, context):
            html = app_templates.get_template(template).render(context)
            route.fulfill(content_type="text/html; charset=utf-8", body=html)

        def router(route):
            url = urlparse(route.request.url)
            query = dict(parse_qsl(url.query))
            if url.path in ("/", "/kiosk", "/register") or url.path.startswith("/status/"):
                calls["pages"].append(route.request.url)
                calls["order"].append(("page", url.path))
            if url.path in ("/", "/kiosk"):
                return page_html(route, "checkin.html", {**base, "kiosk": url.path == "/kiosk"})
            if url.path == "/register":
                return page_html(route, "register.html", {**base, "name": NAME})
            if url.path.startswith("/status/"):
                context = {**base, "name": NAME, "kiosk": query.get("kiosk") == "true"}
                if query.get("checkin_id") and query.get("t"):
                    context.update(checkin_id=query["checkin_id"], status_token=query["t"])
                template = "status_ready.html" if query.get("confirmed") else "status_pending.html"
                return page_html(route, template, context)
            if url.path.startswith("/static/"):
                file = BACKEND_DIR / url.path.lstrip("/")
                if not file.is_file():
                    return route.fulfill(status=404, body="")
                kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                return route.fulfill(content_type=kind, body=file.read_bytes())
            if url.path == "/api/checkin/orchestrate":
                return reply(route, ORCHESTRATE)
            if url.path == "/api/player/games":
                calls["games"].append(route.request.post_data_json)
                if hold_games:
                    calls["held"].append(route)
                    return None
                return reply(route, {"success": True})
            if url.path == "/api/membership/start":
                return reply(route, {"ok": True, "state": "already_member"})
            if url.path.startswith("/api/checkin/") and url.path.endswith("/status"):
                ready = query.get("t") and "confirmed" in route.request.headers.get("referer", "")
                return reply(route, {**ID_STATUS, "ready": bool(ready)})
            if url.path.startswith("/api/"):
                return reply(route, {"member": False})
            return route.abort()

        context = browser.new_context(viewport=viewport, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        if clock:
            page.clock.install()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: (
                calls["failed"].append(request.url),
                calls["order"].append(("games-failed", request.failure)),
            )
            if "/api/player/games" in request.url
            else None,
        )
        page.route("**/*", router)
        return page, calls

    yield open_site
    for context in contexts:
        context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"


def shot(page, name):
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True, animations="disabled")


def kiosk_check_in(page):
    """Returns history.length on the kiosk start (about:blank + /kiosk in a fresh tab)."""
    page.goto("/kiosk")
    start = history_length(page)
    page.fill("#namn", NAME)
    page.fill("#telefon", PHONE)
    page.fill("#tag", TAG)
    if page.locator("#consent").count():
        page.check("#consent")
    page.click(".btn-submit")
    page.wait_for_url("**/register?**")
    return start


def history_length(page):
    return page.evaluate("history.length")


def leaves_no_player_page(page):
    url = page.url
    return "/register" not in url and "/status/" not in url and "tok-" not in url


# --- B1: no Back to the previous player's page --------------------------------------------


def test_back_after_back_to_start_never_shows_the_previous_player(site):
    page, calls = site()
    start = kiosk_check_in(page)
    assert history_length(page) == start  # check-in -> register replaced the entry
    page.click("#back-link")  # "Back to start"
    page.wait_for_url("**/kiosk")
    assert history_length(page) == start
    shot(page, "T-062-1-kiosk-after-back-to-start")

    page.go_back()
    page.wait_for_timeout(500)
    assert leaves_no_player_page(page), page.url
    assert not any("tok-" in p for p in calls["pages"][calls["pages"].index(BASE_URL + "/kiosk", 1):])


@pytest.mark.parametrize(
    "path, seconds", [(STATUS_KIOSK, 16), (STATUS_KIOSK + "&confirmed=1", 6)], ids=["pending", "ready"]
)
def test_status_page_countdowns_return_without_a_history_entry(site, path, seconds):
    page, calls = site(clock=True)
    page.goto("/kiosk")
    page.goto(path)  # (a pushed entry, like an older flow would leave)
    page.clock.run_for(seconds * 1000)
    page.wait_for_url("**/kiosk")
    page.go_back()  # back to the FIRST /kiosk entry, never to the status page
    page.wait_for_timeout(300)
    assert page.url == BASE_URL + "/kiosk"


def test_an_older_player_page_opened_from_history_goes_home(site):
    """Second line of defence: an entry left by any push navigation (e.g. the v2 step's redirect)."""
    page, calls = site()
    page.goto("/kiosk")
    page.goto(REGISTER_KIOSK)  # pushed on purpose: an old player page in history
    page.wait_for_selector("#back-link")
    page.goto("/kiosk")  # the next person starts
    page.go_back()  # tries to reopen the old player page
    page.wait_for_url("**/kiosk", timeout=5000)
    page.wait_for_timeout(300)
    assert page.url == BASE_URL + "/kiosk"
    assert page.input_value("#namn") == ""


def test_kiosk_links_on_player_pages_replace_the_entry(site):
    page, _ = site()
    page.goto("/kiosk")
    page.goto(REGISTER_KIOSK)
    before = history_length(page)
    page.click("#back-link")
    page.wait_for_url("**/kiosk")
    assert history_length(page) == before  # replaced, not pushed


# --- B2: register idles home -------------------------------------------------------------


def test_kiosk_register_idles_home_with_a_countdown_and_still_here(site):
    page, _ = site(clock=True)
    start = kiosk_check_in(page)
    warning = page.locator("#kiosk-idle-warning")
    page.clock.run_for(79_000)
    assert warning.is_hidden()
    page.clock.run_for(2_000)
    assert warning.is_visible()
    assert "returning to check-in in" in page.inner_text("#kiosk-idle-text")
    shot(page, "T-062-2-register-idle-countdown")

    page.click("#kiosk-idle-stay")  # "Still here" restarts the full 90 s
    assert warning.is_hidden()
    page.clock.run_for(60_000)
    assert "/register" in page.url and warning.is_hidden()

    page.clock.run_for(31_000)
    page.wait_for_url("**/kiosk")
    assert history_length(page) == start
    assert page.input_value("#namn") == ""


def test_any_interaction_restarts_the_idle_timer(site):
    page, _ = site(clock=True)
    kiosk_check_in(page)
    page.clock.run_for(70_000)
    page.mouse.click(5, 5)
    page.clock.run_for(70_000)
    assert "/register" in page.url
    assert page.locator("#kiosk-idle-warning").is_hidden()


RECORD_ABORTS = """
(function () {
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = String(input && input.url ? input.url : input);
    if (init && init.signal && url.indexOf("/api/player/games") !== -1) {
      // Fires only for an explicit controller.abort(), never for a page unload.
      init.signal.addEventListener("abort", function () {
        sessionStorage.setItem("__games_aborted", "1");
      });
    }
    return realFetch.apply(this, arguments);
  };
})();
"""


def test_idle_return_aborts_a_pending_games_save(site):
    page, calls = site(clock=True, hold_games=True)
    page.add_init_script(RECORD_ABORTS)
    kiosk_check_in(page)
    page.locator("text=Street Fighter 6").first.click()
    page.click("text=Confirm Selection")
    for _ in range(100):
        if calls["held"]:
            break
        page.wait_for_timeout(20)
    assert calls["held"], "games PATCH was not sent"
    assert page.evaluate("sessionStorage.getItem('__games_aborted')") is None
    page.clock.run_for(91_000)
    page.wait_for_url("**/kiosk")
    # The idle return aborted it itself (an AbortSignal abort), not just the page unload.
    assert page.evaluate("sessionStorage.getItem('__games_aborted')") == "1"


def test_v2_membership_step_keeps_its_own_idle_handling(site):
    page, _ = site(flow="v2", clock=True, require_membership=True)
    page.goto("/kiosk")
    page.goto(REGISTER_KIOSK.replace("&checkin_id", "&ebas=true&checkin_id"))
    page.wait_for_selector('#section-ebas[data-membership-flow="v2"]')
    page.clock.run_for(100_000)
    page.wait_for_timeout(200)
    assert "/register" in page.url  # v2 wipes its own fields; no navigation from here


# --- Normal (non-kiosk) mode is unchanged --------------------------------------------------


def test_normal_mode_history_and_no_idle_return(site):
    page, _ = site(clock=True)
    page.goto("/")
    start = history_length(page)
    page.fill("#namn", NAME)
    page.fill("#telefon", PHONE)
    page.fill("#tag", TAG)
    if page.locator("#consent").count():
        page.check("#consent")
    page.click(".btn-submit")
    page.wait_for_url("**/register?**")
    assert history_length(page) == start + 1  # normal navigation still pushes
    assert page.locator("#kiosk-idle-warning").count() == 0
    page.clock.run_for(120_000)
    assert "/register" in page.url
    assert page.get_attribute("#back-link", "href") == "/"
    page.go_back()
    page.wait_for_url(BASE_URL + "/")


@pytest.mark.parametrize("kiosk", [True, False], ids=["kiosk", "normal"])
def test_v2_membership_only_success_redirect(site, kiosk):
    """MSG scope extension: membership_v2.js's success redirect replaces the entry on the kiosk."""
    page, calls = site(flow="v2", clock=True, require_membership=True)
    page.goto("/kiosk" if kiosk else "/")
    url = REGISTER_KIOSK.replace("&checkin_id", "&ebas=true&membership_only=true&checkin_id")
    page.goto(url if kiosk else url.replace("&kiosk=true", ""))
    start = history_length(page)
    page.fill("#mv2-pnr", synthetic_personnummer())
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")
    page.click("#mv2-start")
    page.wait_for_function("document.body.innerText.includes('already a member')")
    page.clock.run_for(2_500)
    page.wait_for_url("**/status/**")
    assert "t=tok-chk-k" in page.url
    if kiosk:
        assert history_length(page) == start  # replaced: no token page left behind
        page.go_back()
        page.wait_for_timeout(300)
        assert "/register" not in page.url
    else:
        assert history_length(page) == start + 1  # normal mode unchanged


# --- Review fixes (T-062 CHANGES) ---------------------------------------------------------


def test_v2_success_hands_idle_back_to_the_register_page(site):
    """Review #2: after an ordinary v2 success the register page idles home without a touch."""
    page, _ = site(flow="v2", clock=True, require_membership=True)
    page.goto("/kiosk")
    page.goto(REGISTER_KIOSK.replace("&checkin_id", "&ebas=true&checkin_id"))
    page.fill("#mv2-pnr", synthetic_personnummer())
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")
    page.click("#mv2-start")
    page.wait_for_function("document.body.innerText.includes('already a member')")
    page.clock.run_for(5_000)  # v2 hides its section; no further interaction from here
    assert "/register" in page.url
    page.clock.run_for(100_000)
    page.wait_for_url("**/kiosk")


DIRECT_KIOSK = (
    "/register?name=Synthetic%20Kiosk&startgg=true&event=synthetic-event&tag=pw-kiosk"
    "&kiosk=true&checkin_id=chk-k&t=tok-chk-k"
)


def test_direct_kiosk_url_keeps_kiosk_mode_on_continue_and_back(site):
    """Review #3: a fresh tab opened straight on a kiosk register URL stays a kiosk flow."""
    page, _ = site(clock=True)
    page.goto(DIRECT_KIOSK)  # no /kiosk visit first: no session flag yet
    assert page.get_attribute("#back-link", "href") == "/kiosk"
    start = history_length(page)
    page.click("#btn-final-checkin")
    page.wait_for_url("**/status/**")
    status = dict(parse_qsl(urlparse(page.url).query))
    assert status.get("kiosk") == "true" and status.get("t") == "tok-chk-k"
    assert history_length(page) == start  # replaced
    page.wait_for_selector("#kiosk-back:not(.hidden)")  # the status page knows it is a kiosk
    page.clock.run_for(16_000)
    page.wait_for_url("**/kiosk")


@pytest.mark.parametrize("path", ["/", "/kiosk"], ids=["normal", "kiosk"])
def test_late_written_personal_storage_is_purged_before_the_redirect(site, path):
    """Review #4: phone/personal id written after load never reach the register page."""
    page, _ = site()
    page.goto(path)
    page.fill("#namn", NAME)
    page.fill("#telefon", PHONE)
    page.fill("#tag", TAG)
    if page.locator("#consent").count():
        page.check("#consent")
    page.evaluate(
        "localStorage.setItem('telefon', '0709999999');"
        "localStorage.setItem('personnummer', '189001019802')"
    )
    page.click(".btn-submit")
    page.wait_for_url("**/register?**")
    assert page.evaluate("localStorage.getItem('telefon')") is None
    assert page.evaluate("localStorage.getItem('personnummer')") is None


# --- Review #1: real back/forward cache ---------------------------------------------------
# page.route() and Playwright's default --disable-back-forward-cache both keep BFCache off, so
# this test serves the real templates from a loopback HTTP server to a BFCache-enabled Chromium.


@pytest.fixture(scope="module")
def bfcache_browser(browser):
    # A second Chromium from the same Playwright instance (one sync loop per thread).
    chromium = browser.browser_type.launch(
        channel="chromium", ignore_default_args=["--disable-back-forward-cache"]
    )
    yield chromium
    chromium.close()


@pytest.fixture
def loopback_site(app_templates):  # noqa: F811
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    base = {
        "membership_flow": "legacy",
        "membership_year": 2026,
        "n8n_token": "",
        "sse_token": "",
        "collect_acquisition_source": False,
        "require_membership": False,
        "require_payment": False,
        "require_startgg": True,
        "offer_membership": False,
        "is_member": False,
        "games": [{"id": "1", "name": "Street Fighter 6"}],
        "swish_number": "000 000 00 00",
        "swish_expected_per_game": 25,
        "tag": TAG,
        "event_slug": "synthetic-event",
        "event_name": "Synthetic Event",
        "status": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, kind, status=200):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/kiosk"):
                html = app_templates.get_template("checkin.html").render(
                    {**base, "kiosk": url.path == "/kiosk"}
                )
                return self.send(html, "text/html; charset=utf-8")
            if url.path == "/register":
                html = app_templates.get_template("register.html").render({**base, "name": NAME})
                return self.send(html, "text/html; charset=utf-8")
            if url.path.startswith("/static/"):
                file = BACKEND_DIR / url.path.lstrip("/")
                if file.is_file():
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return self.send(file.read_bytes(), kind)
                return self.send("", "text/plain", 404)
            if url.path.startswith("/api/checkin/") and url.path.endswith("/status"):
                return self.send(json.dumps(ID_STATUS), "application/json")
            if url.path.startswith("/api/"):
                return self.send(json.dumps({"member": False}), "application/json")
            return self.send("", "text/plain", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


RECORD_PAGESHOW = """
window.addEventListener("pageshow", function (e) {
  var seen = JSON.parse(sessionStorage.getItem("__pageshow") || "[]");
  seen.push([location.pathname, e.persisted]);
  sessionStorage.setItem("__pageshow", JSON.stringify(seen));
});
"""


def test_bfcache_back_then_forward_never_restores_the_previous_player(
    bfcache_browser, loopback_site
):
    context = bfcache_browser.new_context(viewport=TABLET, base_url=loopback_site)
    context.add_init_script(RECORD_PAGESHOW)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto("/kiosk")
        page.goto(REGISTER_KIOSK)  # a pushed, stamped player page (the worst case)
        page.wait_for_selector("#back-link")
        # A BFCache restore fires pageshow but no load event: only wait for the commit.
        page.go_back(wait_until="commit")  # restores the kiosk start (from BFCache)
        page.wait_for_url("**/kiosk", wait_until="commit")
        page.wait_for_timeout(300)
        page.go_forward(wait_until="commit")  # tries to bring back the player page
        page.wait_for_timeout(1000)
        page.wait_for_url("**/kiosk", wait_until="commit")
        assert page.url.endswith("/kiosk"), page.url
        assert "tok-" not in page.url
        seen = page.evaluate("JSON.parse(sessionStorage.getItem('__pageshow') || '[]')")
        assert ["/kiosk", True] in seen, f"BFCache was not exercised: {seen}"
    finally:
        context.close()
    assert not errors, errors

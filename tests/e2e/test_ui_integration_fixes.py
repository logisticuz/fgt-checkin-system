"""T-034 browser flows: games save with checkin_id, and the degraded (eBas outage) chain."""

import json
import mimetypes
from urllib.parse import parse_qsl, urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, TABLET

pytestmark = pytest.mark.e2e

NAME = "Synthetic Player"
GAMES = [{"id": "1", "name": "Street Fighter 6"}, {"id": "2", "name": "Tekken 8"}]
DEGRADED = {
    "ready": False,
    "missing": [],
    "member": None,
    "payment_valid": True,
    "startgg": True,
    "status": "Pending",
    "slug": "synthetic-event",
    "checkin_id": "chk-a",
    "status_token": "tok-chk-a",
    "integrations": {"ebas": "unavailable", "startgg": "ok"},
    "degraded": True,
}


@pytest.fixture
def site(browser, app_templates):  # noqa: F811
    contexts, errors = [], []

    def open_site(
        orchestrate=None, games_reply=(200, {"success": True}), participant=None, id_status=None
    ):
        calls = {"games": [], "pages": [], "participant": [], "id_status": []}
        base = {
            "membership_flow": "v2",
            "membership_year": 2026,
            "kiosk": False,
            "collect_acquisition_source": False,
            "require_membership": True,
            "require_payment": False,
            "require_startgg": True,
            "games": GAMES,
            "swish_number": "000 000 00 00",
            "swish_expected_per_game": 25,
            "name": NAME,
            "tag": "synthetic",
            "event_slug": "synthetic-event",
            "event_name": "Synthetic Event",
            "status": {},
            "offer_membership": False,
            "is_member": False,
        }

        def reply(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def router(route):
            url = urlparse(route.request.url)
            if url.path in ("/", "/register") or url.path.startswith("/status/"):
                calls["pages"].append(route.request.url)
                name = {
                    "/": "checkin.html",
                    "/register": "register.html",
                }.get(url.path, "status_pending.html")
                context = dict(base)
                query = dict(parse_qsl(url.query))
                if url.path.startswith("/status/") and query.get("checkin_id") and query.get("t"):
                    # Stand-in for the server's verified capability context (T-024/T-032).
                    context.update(checkin_id=query["checkin_id"], status_token=query["t"])
                html = app_templates.get_template(name).render(context)
                return route.fulfill(content_type="text/html", body=html)
            if url.path.startswith("/static/"):
                file = BACKEND_DIR / url.path.lstrip("/")
                if file.is_file():
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return route.fulfill(content_type=kind, body=file.read_bytes())
                return route.fulfill(status=404, body="")
            if url.path == "/api/checkin/orchestrate":
                return reply(route, orchestrate)
            if url.path == "/api/player/games":
                calls["games"].append(route.request.post_data_json)
                status, body = games_reply
                return reply(route, body, status)
            if url.path.startswith("/api/checkin/") and url.path.endswith("/status"):
                calls["id_status"].append(route.request.url)
                return reply(route, id_status or {"found": False, "ready": False})
            if url.path.startswith("/api/participant/"):
                calls["participant"].append(route.request.url)
                return reply(route, participant or {"member": False, "startgg": True})
            if url.path.startswith("/api/"):
                return reply(route, {}, 404)
            return route.abort()

        context = browser.new_context(viewport=TABLET, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", router)
        return page, calls

    yield open_site
    for context in contexts:
        context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"


# The reviewer's exact destination response during an outage.
ID_STATUS_UNVERIFIED = {
    "found": True,
    "ready": False,
    "member": False,
    "startgg": True,
    "payment_ok": True,
    "missing": ["Membership"],
    "games": [],
    "status": "Pending",
}

REGISTER = "/register?name=Synthetic%20Player&startgg=false&event=synthetic-event&tag=synthetic"


# --- #2 games save -----------------------------------------------------------------------------


def test_games_are_saved_with_the_checkin_id(site):
    page, calls = site()
    page.goto(REGISTER + "&checkin_id=chk-a&t=tok-chk-a")
    page.check("#game-1")
    page.check("#game-2")
    page.click("#btn-confirm-games")
    page.wait_for_selector("#section-startgg.hidden", state="attached")
    assert calls["games"] == [
        {
            "checkin_id": "chk-a",
            "games": ["Street Fighter 6", "Tekken 8"],
            "tag": "synthetic",
            "slug": "synthetic-event",
            "status_token": "tok-chk-a",
        }
    ]
    assert "2 game(s) saved" in page.inner_text("#messages")
    assert page.is_visible("#btn-final-checkin")
    assert page.evaluate("localStorage.getItem('selectedGames')") is None


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"detail": {"error": "Unknown game", "allowed_games": ["X"]}}, "Unknown game"),
        (
            {"detail": "<img src=x onerror=window.__pwned=1>"},
            "<img src=x onerror=window.__pwned=1>",
        ),
    ],
    ids=["structured-detail", "hostile-text"],
)
def test_games_save_errors_show_the_server_text_as_text(site, body, expected):
    page, _ = site(games_reply=(400, body))
    page.goto(REGISTER + "&checkin_id=chk-a")
    page.check("#game-1")
    page.click("#btn-confirm-games")
    page.wait_for_selector("#messages .message.error")
    assert page.inner_text("#messages") == expected
    assert page.locator("#messages img").count() == 0
    assert page.evaluate("window.__pwned") is None


def test_games_save_without_a_checkin_id_explains_and_sends_nothing(site):
    page, calls = site()
    page.goto(REGISTER)
    page.check("#game-1")
    page.click("#btn-confirm-games")
    page.wait_for_selector("#messages .message.error")
    assert "couldn't link this to your check-in" in page.inner_text("#messages")
    assert calls["games"] == []


# --- #3 degraded chain -------------------------------------------------------------------------


def check_in(page):
    page.goto("/")
    page.fill("#namn", NAME)
    page.fill("#telefon", "0700000000")
    page.fill("#tag", "synthetic")
    page.check("#consent")
    page.click(".btn-submit")


@pytest.mark.parametrize(
    "integrations",
    [{"ebas": "unavailable", "startgg": "ok"}, {"ebas": "skipped", "startgg": "ok"}],
    ids=["ebas-outage", "engine-off"],
)
def test_degraded_ebas_keeps_membership_unknown_through_the_whole_chain(site, integrations):
    page, calls = site(
        orchestrate={**DEGRADED, "integrations": integrations},
        id_status=ID_STATUS_UNVERIFIED,
    )
    check_in(page)
    page.wait_for_url("**/register?**")
    params = dict(parse_qsl(urlparse(page.url).query))
    assert params["membership"] == "unknown"
    assert "ebas" not in params
    assert params["checkin_id"] == "chk-a" and params["t"] == "tok-chk-a"

    assert (
        page.inner_text("#messages") == "Verification temporarily unavailable – a TO will help you"
    )
    assert page.inner_text("#status-member .status-icon") == "?"
    assert not page.is_visible("#section-ebas")  # no "become a member"
    page.wait_for_function("document.querySelectorAll('#messages').length === 1")
    page.wait_for_timeout(300)  # the name-based status refresh must not flip it to ✗
    assert calls["participant"], "status refresh ran"
    assert page.inner_text("#status-member .status-icon") == "?"

    page.click("#btn-final-checkin")
    for _ in range(100):
        if any("/status/" in url for url in calls["pages"]):
            break
        page.wait_for_timeout(25)
    status_url = next(url for url in calls["pages"] if "/status/" in url)
    assert dict(parse_qsl(urlparse(status_url).query)) == {
        "checkin_id": "chk-a",
        "t": "tok-chk-a",
        "membership": "unknown",
    }

    # Destination: the id poll still reports the stored member=false / "Membership" missing,
    # but the page shows membership as unknown (TO helps), not as missing.
    page.wait_for_url("**/status/**")
    for _ in range(100):
        if calls["id_status"]:
            break
        page.wait_for_timeout(25)
    page.wait_for_selector("#membership-unknown-note:not([hidden])")
    assert page.inner_text("#membership-unknown-note") == "? Membership: unknown – a TO will help"
    assert "Member" not in page.locator("#missing-list").inner_text()
    assert "Member" not in page.locator("#ready-list").inner_text()


def test_a_verified_non_member_still_gets_the_membership_step(site):
    verified_no = {
        **DEGRADED,
        "member": False,
        "missing": ["Membership"],
        "integrations": {"ebas": "ok", "startgg": "ok"},
        "degraded": False,
    }
    page, _ = site(orchestrate=verified_no)
    check_in(page)
    page.wait_for_url("**/register?**")
    params = dict(parse_qsl(urlparse(page.url).query))
    assert params["ebas"] == "true" and "membership" not in params
    assert page.is_visible("#section-ebas")
    assert page.inner_text("#status-member .status-icon") == "✗"


def test_status_page_shows_membership_done_once_the_poll_confirms_it(site):
    page, _ = site(id_status={**ID_STATUS_UNVERIFIED, "member": True, "missing": []})
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a&membership=unknown")
    page.wait_for_function("document.getElementById('ready-list').textContent.includes('Member')")
    assert page.locator("#membership-unknown-note").is_hidden()
    assert "Member" not in page.locator("#missing-list").inner_text()


def test_without_the_unknown_hint_a_non_member_is_shown_as_missing(site):
    page, _ = site(id_status=ID_STATUS_UNVERIFIED)
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page.wait_for_function("document.getElementById('missing-list').textContent.includes('Member')")
    assert page.locator("#membership-unknown-note").is_hidden()

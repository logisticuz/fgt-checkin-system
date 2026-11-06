"""checkin_id travels check-in -> register/status -> membership v2 start payload (T-020)."""

import json
import mimetypes
import re
from urllib.parse import parse_qsl, urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, DESKTOP, MockApi, synthetic_personnummer

pytestmark = pytest.mark.e2e

CHECKIN_ID = "rec Synthetic/42&x"  # opaque; deliberately needs URL encoding
NAME = "Synthetic Player"
CODE_SENT = {
    "ok": True,
    "state": "code_sent",
    "attempt_id": "att-1",
    "masked_destination": "+46******12",
    "resend_after_s": 60,
}
NOT_READY = {
    "ready": False,
    "member": False,
    "startgg": True,
    "payment_valid": True,
    "missing": ["Membership"],
    "slug": "synthetic-event",
}


@pytest.fixture
def site(browser, app_templates):  # noqa: F811
    """Factory: a browser page serving the real check-in/register/status templates."""
    contexts, errors = [], []
    base = {
        "membership_flow": "v2",
        "membership_year": 2026,
        "n8n_token": "",
        "sse_token": "",
        "kiosk": False,
        "collect_acquisition_source": False,
        "games": [],
        "require_membership": True,
        "require_payment": False,
        "require_startgg": False,
        "swish_number": "000 000 00 00",
        "swish_expected_per_game": 25,
        "name": NAME,
        "tag": "synthetic",
        "event_slug": "synthetic-event",
        "event_name": "Synthetic Event",
        "status": {},
        "offer_membership": True,
        "is_member": False,
    }

    def open_site(
        orchestrate=None, participant=None, status_templates=("status_ready.html",), **ctx
    ):
        api = MockApi()
        calls = {"orchestrate": [], "status_pages": []}
        context_vars = {**base, **ctx}

        def render(name):
            return app_templates.get_template(name).render(context_vars)

        def json_reply(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def router(route):
            path = urlparse(route.request.url).path
            if path in ("/", "/kiosk"):
                return route.fulfill(content_type="text/html", body=render("checkin.html"))
            if path == "/register":
                return route.fulfill(content_type="text/html", body=render("register.html"))
            if path.startswith("/status/"):
                # Serve the templates in order (e.g. pending, then ready); the last one repeats.
                calls["status_pages"].append(route.request.url)
                index = min(len(calls["status_pages"]), len(status_templates)) - 1
                return route.fulfill(content_type="text/html", body=render(status_templates[index]))
            if path.startswith("/static/"):
                file = BACKEND_DIR / path.lstrip("/")
                if file.is_file():
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return route.fulfill(content_type=kind, body=file.read_bytes())
                return route.fulfill(status=404, body="")
            if path == "/api/checkin/orchestrate":
                calls["orchestrate"].append(route.request.post_data_json)
                return json_reply(route, orchestrate)
            match = re.fullmatch(r"/api/membership/(start|verify|resend)", path)
            if match:
                return api.handle(route, match.group(1))
            if path.startswith("/api/participant/") and participant is not None:
                return json_reply(route, participant)
            if path.startswith("/api/"):
                return json_reply(route, {}, status=404)
            return route.abort()

        context = browser.new_context(viewport=DESKTOP, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", router)
        return page, api, calls

    yield open_site
    for context in contexts:
        context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"


def check_in(page):
    page.goto("/")
    page.fill("#namn", NAME)
    page.fill("#telefon", "0700000000")
    page.fill("#tag", "synthetic")
    page.check("#consent")
    page.click(".btn-submit")


def query(page):
    return dict(parse_qsl(urlparse(page.url).query))


def test_checkin_to_register_carries_id_into_v2_start_payload(site):
    page, api, _ = site(orchestrate={**NOT_READY, "checkin_id": CHECKIN_ID})
    check_in(page)
    page.wait_for_url("**/register?**")
    params = query(page)
    assert params["checkin_id"] == CHECKIN_ID
    assert params["name"] == NAME and params["ebas"] == "true"
    assert params["event"] == "synthetic-event"
    # Only opaque/flow data in the URL: no phone, personnummer or email.
    assert "0700000000" not in page.url and "telefon" not in page.url
    assert "personnummer" not in page.url

    api.queue("start", CODE_SENT)
    page.fill("#mv2-pnr", synthetic_personnummer())
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-step-code:not([hidden])")
    assert api.calls["start"][0]["checkin_id"] == CHECKIN_ID


def test_register_url_is_unchanged_without_an_id(site):
    page, _, _ = site(orchestrate=NOT_READY)
    check_in(page)
    page.wait_for_url("**/register?**")
    assert urlparse(page.url).query == "name=Synthetic%20Player&ebas=true&event=synthetic-event"


def test_ready_checkin_goes_to_status_with_id_and_member_link_keeps_it(site):
    page, _, _ = site(
        orchestrate={"ready": True, "checkin_id": CHECKIN_ID}, require_membership=False
    )  # the "Join as Member" offer only shows when membership is optional
    check_in(page)
    page.wait_for_url("**/status/**")
    assert urlparse(page.url).path == "/status/Synthetic%20Player"
    assert query(page) == {"checkin_id": CHECKIN_ID}

    href = page.get_attribute("a.support-link", "href")
    assert href.startswith(
        "/register?name=Synthetic%20Player&tag=synthetic&event=synthetic-event"
        "&membership_only=true&checkin_id="
    )
    assert dict(parse_qsl(urlparse(href).query))["checkin_id"] == CHECKIN_ID


def test_ready_checkin_without_id_keeps_old_urls(site):
    page, _, _ = site(orchestrate={"ready": True}, require_membership=False)
    check_in(page)
    page.wait_for_url("**/status/**")
    assert page.url == BASE_URL + "/status/Synthetic%20Player"
    assert page.get_attribute("a.support-link", "href") == (
        "/register?name=Synthetic%20Player&tag=synthetic&event=synthetic-event&membership_only=true"
    )


@pytest.mark.parametrize("incoming", [CHECKIN_ID, None], ids=["with-id", "without-id"])
def test_pending_page_keeps_id_when_it_turns_ready(site, incoming):
    page, _, calls = site(
        participant={"ready": True, "missing": []},
        status_templates=("status_pending.html", "status_ready.html"),
    )
    encoded = "rec%20Synthetic%2F42%26x"
    page.goto("/status/Synthetic%20Player" + (f"?checkin_id={encoded}" if incoming else ""))
    for _ in range(200):  # the pending page checks status on load and redirects when ready
        if len(calls["status_pages"]) >= 2:
            break
        page.wait_for_timeout(25)
    redirected = calls["status_pages"][1]
    assert urlparse(redirected).path == "/status/Synthetic%20Player"
    assert dict(parse_qsl(urlparse(redirected).query)) == (
        {"checkin_id": CHECKIN_ID} if incoming else {}
    )

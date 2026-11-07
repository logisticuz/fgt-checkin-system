"""T-040: terminal manual_review in the v2 step, and the ownership-unverified state (T-038)."""

import json
import mimetypes
from urllib.parse import urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e

CODE_SENT = {
    "ok": True,
    "state": "code_sent",
    "attempt_id": "att-1",
    "masked_destination": "+46******12",
    "resend_after_s": 60,
}
MANUAL_REVIEW = [
    {"ok": False, "state": "manual_review", "error_code": "checkin_missing"},
    {"ok": False, "state": "manual_review", "error_code": None},
]


def fill_details(page):
    page.fill("#mv2-pnr", synthetic_personnummer())
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")


def to_code_step(page, api):
    api.queue("start", CODE_SENT)
    fill_details(page)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-step-code:not([hidden])")


def assert_manual_review_terminal(page):
    page.wait_for_selector("#mv2-step-help:not([hidden])")
    assert page.inner_text("#mv2-help-heading") == (
        "Your registration was received – a TO will finish it with you"
    )
    assert "don't register again" in page.inner_text("#mv2-help-text")
    # No new code, no retry: the code step and its buttons are gone.
    assert not page.is_visible("#mv2-step-code")
    assert not page.is_visible("#mv2-resend")
    assert not page.is_visible("#mv2-verify")
    assert page.evaluate("sessionStorage.getItem('mv2_attempt_id')") is None
    assert page.is_visible("#mv2-reset")  # next-person reset still available


@pytest.mark.parametrize("answer", MANUAL_REVIEW, ids=["checkin-missing", "plain"])
def test_verify_manual_review_is_terminal(kiosk, answer):
    page, api = kiosk()
    to_code_step(page, api)
    api.queue("verify", answer)
    page.fill("#mv2-code", "123456")
    page.click("#mv2-verify")
    assert_manual_review_terminal(page)
    assert len(api.calls["verify"]) == 1


def test_status_poll_turning_manual_review_stops_polling(kiosk):
    page, api = kiosk(clock=True)
    to_code_step(page, api)
    api.queue("verify", {"ok": True, "state": "outcome_unknown"})
    api.queue("status", {"state": "outcome_unknown"}).queue("status", MANUAL_REVIEW[0])
    page.fill("#mv2-code", "123456")
    page.click("#mv2-verify")
    page.wait_for_selector("#mv2-step-busy:not([hidden])")
    for expected in (1, 2):
        page.clock.run_for(5_000)
        for _ in range(100):
            if len(api.calls["status"]) >= expected:
                break
            page.wait_for_timeout(20)
    assert_manual_review_terminal(page)
    polls = len(api.calls["status"])
    page.clock.run_for(30_000)
    page.wait_for_timeout(100)
    assert len(api.calls["status"]) == polls  # stopped


def test_start_not_owner_points_to_a_to(kiosk):
    page, api = kiosk()
    api.queue("start", {"ok": False, "error_code": "not_owner", "user_message": "x"}, status=403)
    fill_details(page)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-error:not([hidden])")
    assert page.inner_text("#mv2-error") == "Ask a TO to help you continue."


# --- ownership unverified (T-038) ------------------------------------------------------------


@pytest.fixture
def site(browser, app_templates):  # noqa: F811
    contexts, errors = [], []

    def open_site(orchestrate=None, flow="v2"):
        calls = {"pages": [], "games": [], "membership": [], "ebas": []}
        base = {
            "membership_flow": flow,
            "membership_year": 2026,
            "kiosk": False,
            "collect_acquisition_source": False,
            "require_membership": True,
            "require_payment": False,
            "require_startgg": True,
            "games": [{"id": "1", "name": "Street Fighter 6"}],
            "swish_number": "000 000 00 00",
            "swish_expected_per_game": 25,
        }

        def reply(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def router(route):
            url = urlparse(route.request.url)
            if url.path in ("/", "/register"):
                calls["pages"].append(route.request.url)
                name = "checkin.html" if url.path == "/" else "register.html"
                html = app_templates.get_template(name).render(base)
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
                return reply(route, {"success": True})
            if url.path.startswith("/api/membership/"):
                calls["membership"].append(route.request.url)
                return reply(route, CODE_SENT)
            if url.path == "/api/ebas/register":
                calls["ebas"].append(route.request.post_data_json)
                return reply(route, {"success": True})
            if url.path.startswith("/api/"):
                return reply(route, {"member": False}, 200)
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


UNVERIFIED = {
    "ready": False,
    "missing": ["Membership", "Start.gg"],
    "member": False,
    "startgg": False,
    "payment_valid": True,
    "slug": "synthetic-event",
    "checkin_id": "chk-a",
    "ownership": "unverified",  # T-038: no status_token for an unproven re-check-in
    "integrations": {"ebas": "ok", "startgg": "ok"},
    "degraded": False,
}


def test_checkin_with_unverified_ownership_stops_with_the_to_notice(site):
    page, calls = site(orchestrate=UNVERIFIED)
    page.goto("/")
    page.fill("#namn", "Synthetic Player")
    page.fill("#telefon", "0700000000")
    page.fill("#tag", "synthetic")
    page.check("#consent")
    page.click(".btn-submit")
    page.wait_for_selector("#error-container.show")
    assert page.locator("#error-container p").first.inner_text() == "Ask a TO to help you continue"
    page.wait_for_timeout(300)
    assert calls["pages"] == [BASE_URL + "/"]  # no redirect to register/status
    assert page.input_value("#namn") == "" and page.input_value("#telefon") == ""
    assert calls["games"] == calls["membership"] == []


@pytest.mark.parametrize("flow", ["v2", "legacy"])
def test_register_without_a_token_offers_no_token_flows(site, flow):
    page, calls = site(flow=flow)
    page.goto(
        "/register?name=Synthetic%20Player&ebas=true&startgg=false&event=synthetic-event"
        "&tag=synthetic&checkin_id=chk-a"
    )
    page.wait_for_function("document.getElementById('messages').textContent.length > 0")
    assert page.inner_text("#messages") == "Ask a TO to help you continue"
    for section in ("#section-ebas", "#section-startgg", "#section-final"):
        assert not page.is_visible(section), section
    page.wait_for_timeout(200)
    assert calls["games"] == calls["membership"] == calls["ebas"] == []


def test_legacy_registration_sends_the_checkin_capability(site):
    page, calls = site(flow="legacy")
    page.goto(
        "/register?name=Synthetic%20Player&ebas=true&event=synthetic-event&tag=synthetic"
        "&checkin_id=chk-a&t=tok-chk-a"
    )
    page.click("#pnr-toggle-manual")
    page.fill("#pnr-manual-input", synthetic_personnummer())
    page.fill("#phone-ebas", "0700000000")
    page.click("#btn-register-ebas")
    for _ in range(100):
        if calls["ebas"]:
            break
        page.wait_for_timeout(25)
    body = calls["ebas"][0]
    assert body["checkin_id"] == "chk-a" and body["status_token"] == "tok-chk-a"
    assert body["personnummer"] == synthetic_personnummer()

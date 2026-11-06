"""Check-in page: kiosk hygiene, Personal ID validation and error UX (T-025)."""

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e

NAME_A, PHONE_A, TAG_A = "Synthetic Alpha", "0700000001", "alpha_tag"
NOT_READY = {
    "ready": False,
    "missing": ["Membership"],
    "slug": "synthetic-event",
    "checkin_id": "chk-a",
}


@pytest.fixture
def checkin(browser, app_templates):  # noqa: F811
    """Factory: (page, calls) for the real check-in template with a scripted orchestrate."""
    contexts, errors = [], []

    def open_page(responses, path="/", clock=False, viewport=TABLET, **ctx):
        context_vars = {
            "membership_flow": "v2",
            "n8n_token": "",
            "kiosk": path == "/kiosk",
            "collect_acquisition_source": False,
            "require_membership": True,
            "require_payment": False,
            "require_startgg": False,
            "games": [],
            **ctx,
        }
        queue = list(responses)
        calls = {"orchestrate": [], "pages": [], "held": []}

        def router(route):
            request = route.request
            url = urlparse(request.url)
            if url.path in ("/", "/kiosk"):
                calls["pages"].append(request.url)
                html = app_templates.get_template("checkin.html").render(context_vars)
                return route.fulfill(content_type="text/html", body=html)
            if url.path.startswith(("/register", "/status/")):
                calls["pages"].append(request.url)
                return route.fulfill(content_type="text/html", body="<p id='next-page'>next</p>")
            if url.path.startswith("/static/"):
                file = BACKEND_DIR / url.path.lstrip("/")
                kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                return route.fulfill(content_type=kind, body=file.read_bytes())
            if url.path == "/api/checkin/orchestrate":
                calls["orchestrate"].append(request.post_data_json)
                status, body = queue.pop(0) if len(queue) > 1 else queue[0]
                if status is None:
                    return route.abort("connectionreset")
                if status == "hold":  # answer later via release()
                    calls["held"].append(route)
                    return None
                return route.fulfill(
                    status=status, content_type="application/json", body=json.dumps(body)
                )
            return route.fulfill(status=404, content_type="application/json", body="{}")

        context = browser.new_context(viewport=viewport, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", router)
        if clock:
            page.clock.install()
        page.goto(path)
        return page, calls

    yield open_page
    for context in contexts:
        context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"


def fill_basics(page, name=NAME_A, phone=PHONE_A, tag=TAG_A):
    page.fill("#namn", name)
    page.fill("#telefon", phone)
    page.fill("#tag", tag)
    page.check("#consent")


def fill_pnr(page, pnr):
    # Manual mode: the synthetic Personal ID (born 1900) is older than the year picker's range.
    page.click("text=Type manually instead")
    page.fill("#ci-pnr-manual-input", pnr)


def storage_dump(page):
    return page.evaluate(
        "JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])"
    )


def error_text(page):
    return page.locator("#error-container").inner_text().strip()


def screens_dir():
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    return Path(out) if out else None


def shot(page, name):
    folder = screens_dir()
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(folder / f"{name}.tablet-820x1180.png"), full_page=True, animations="disabled"
        )


# --- Personal ID -------------------------------------------------------------------------


def test_half_typed_personal_id_shows_error_and_sends_nothing(checkin):
    page, calls = checkin([(200, NOT_READY)])
    fill_basics(page)
    page.select_option("#ci-pnr-year", "1990")
    page.select_option("#ci-pnr-month", "05")
    page.click(".btn-submit")

    assert "Personal ID is incomplete" in page.inner_text("#ci-pnr-error")
    assert page.evaluate("document.activeElement.id") == "ci-pnr-day"
    assert page.get_attribute("#ci-pnr-day", "aria-invalid") == "true"
    assert page.is_enabled(".btn-submit")
    page.wait_for_timeout(200)
    assert calls["orchestrate"] == []
    shot(page, "01-personal-id-incomplete")

    page.select_option("#ci-pnr-day", "12")  # editing clears the invalid mark
    assert page.get_attribute("#ci-pnr-day", "aria-invalid") is None


def test_half_typed_manual_personal_id_is_caught_too(checkin):
    page, calls = checkin([(200, NOT_READY)])
    fill_basics(page)
    page.click("text=Type manually instead")
    page.fill("#ci-pnr-manual-input", "19900512")
    page.click(".btn-submit")
    assert "10 or 12 digits" in page.inner_text("#ci-pnr-error")
    assert page.evaluate("document.activeElement.id") == "ci-pnr-manual-input"
    assert calls["orchestrate"] == []


def test_empty_personal_id_is_still_allowed(checkin):
    page, calls = checkin([(200, NOT_READY)])
    fill_basics(page)
    page.click(".btn-submit")
    page.wait_for_selector("#next-page")
    assert calls["orchestrate"][0]["personnummer"] == ""


# --- Server errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [(500, {"detail": "Check-in creation failed: boom"}), (None, None)],
    ids=["http-500", "network-error"],
)
def test_server_failure_says_may_have_been_registered_with_status_link(checkin, response):
    page, _ = checkin([response], path="/kiosk")
    fill_basics(page)
    page.click(".btn-submit")
    page.wait_for_selector("#error-container.show")

    assert "Your check-in may have been registered – check with a TO." in error_text(page)
    assert "boom" not in error_text(page)  # internals are not shown to players
    link = page.locator("#error-container a")
    assert link.inner_text() == "Check my status →"
    assert link.get_attribute("href") == "/status/Synthetic%20Alpha?kiosk=true"
    assert page.is_enabled(".btn-submit")
    if response[0] == 500:
        shot(page, "02-server-error-maybe-registered")


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"detail": "No active event configured"}, ["No active event configured"]),
        (
            {"detail": {"errors": ["Name is required", "Tag too long"]}},
            ["Name is required", "Tag too long"],
        ),
        (
            {"detail": "<img src=x onerror=window.__pwned=1>"},
            ["<img src=x onerror=window.__pwned=1>"],
        ),
        ({}, ["Check-in failed (error 400). Please try again or ask a TO."]),
    ],
    ids=["detail-string", "detail-errors", "hostile-text", "no-detail"],
)
def test_client_errors_show_the_server_message_as_text(checkin, body, expected):
    page, _ = checkin([(400, body)])
    fill_basics(page)
    page.click(".btn-submit")
    page.wait_for_selector("#error-container.show")
    assert page.locator("#error-container p").all_inner_texts() == expected
    assert page.locator("#error-container img").count() == 0
    assert page.evaluate("window.__pwned") is None
    if body == {"detail": "No active event configured"}:
        shot(page, "03-server-message")


# --- Kiosk hygiene -----------------------------------------------------------------------


def test_next_person_inherits_nothing(checkin):
    page, calls = checkin([(200, NOT_READY)], path="/kiosk")
    # Leftovers from the old flow are removed on load.
    page.evaluate(
        "localStorage.setItem('namn', 'Old Person'); localStorage.setItem('telefon', '0709999999');"
        "localStorage.setItem('tag', 'old_tag'); localStorage.setItem('eventSlug', 'old-event');"
        "localStorage.setItem('selectedGames', '[1]')"
    )
    page.reload()
    assert page.evaluate("localStorage.length") == 0

    fill_basics(page)  # person A
    fill_pnr(page, synthetic_personnummer())
    page.click(".btn-submit")
    page.wait_for_selector("#next-page")
    register = urlparse(calls["pages"][-1])
    params = dict(parse_qsl(register.query))
    assert params["tag"] == TAG_A and params["name"] == NAME_A  # carried by URL instead
    for secret in (PHONE_A, synthetic_personnummer()):
        assert secret not in calls["pages"][-1]
    dump = storage_dump(page)
    for value in (NAME_A, PHONE_A, TAG_A, "synthetic-event"):
        assert value not in dump, value

    page.go_back()  # Back on the kiosk must not show A's details
    page.wait_for_selector("#checkin-form")
    for selector in ("#namn", "#telefon", "#tag", "#ci-pnr-last4"):
        assert page.input_value(selector) == "", selector
    assert page.input_value("#personnummer") == ""

    page.goto("/kiosk")  # person B
    for selector in ("#namn", "#telefon", "#tag", "#ci-pnr-last4", "#ci-pnr-manual-input"):
        assert page.input_value(selector) == "", selector
    assert not page.is_checked("#consent")
    dump = storage_dump(page)
    for value in (NAME_A, PHONE_A, TAG_A):
        assert value not in dump, value


def test_kiosk_idle_clears_a_half_filled_form(checkin):
    page, _ = checkin([(200, NOT_READY)], path="/kiosk", clock=True)
    fill_basics(page)
    page.select_option("#ci-pnr-year", "1990")
    page.clock.run_for(89_000)
    assert page.input_value("#namn") == NAME_A
    page.clock.run_for(2_000)
    for selector in ("#namn", "#telefon", "#tag"):
        assert page.input_value(selector) == "", selector
    assert page.input_value("#ci-pnr-year") == ""
    assert not page.is_checked("#consent")


def test_kiosk_idle_during_a_slow_submit_drops_the_late_answer(checkin):
    page, calls = checkin([("hold", None)], path="/kiosk", clock=True)
    fill_basics(page)
    page.click(".btn-submit")
    for _ in range(100):
        if calls["held"]:
            break
        page.wait_for_timeout(20)
    page.clock.run_for(91_000)
    assert page.input_value("#namn") == ""
    assert page.is_enabled(".btn-submit")

    calls["held"][0].fulfill(  # the server answers for person A after the reset
        status=200, content_type="application/json", body=json.dumps(NOT_READY)
    )
    page.wait_for_timeout(300)
    assert len(calls["pages"]) == 1  # no redirect to A's register page
    assert page.is_visible("#checkin-form")


def test_no_idle_reset_outside_kiosk(checkin):
    page, _ = checkin([(200, NOT_READY)], clock=True)
    fill_basics(page)
    page.clock.run_for(120_000)
    assert page.input_value("#namn") == NAME_A


def test_kiosk_mode_uses_larger_text(checkin):
    page, _ = checkin([(200, NOT_READY)], path="/kiosk")
    size = page.evaluate("parseFloat(getComputedStyle(document.querySelector('label')).fontSize)")
    assert size >= 16
    shot(page, "04-kiosk-form")

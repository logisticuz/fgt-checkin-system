"""Browser walk-through of the membership v2 kiosk step against a mocked API (T-013)."""

import os
from pathlib import Path

import pytest

from tests.e2e.conftest import DESKTOP, REGISTER_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e

PNR = synthetic_personnummer()
ATTEMPT = "att-synthetic-1"
CODE_SENT = {
    "ok": True,
    "state": "code_sent",
    "attempt_id": ATTEMPT,
    "masked_destination": "+46******12",
    "resend_after_s": 60,
}
SUBMITTED = {"ok": True, "state": "submitted"}
UNKNOWN = {"ok": True, "state": "outcome_unknown"}
URL_A = REGISTER_URL + "&checkin_id=checkin-A"
URL_B = REGISTER_URL + "&checkin_id=checkin-B"


def wait_for(page, condition, timeout_ms=3000):
    """Wait for a Python-side condition (mock call counts) while the page keeps running."""
    for _ in range(timeout_ms // 25):
        if condition():
            return
        page.wait_for_timeout(25)
    assert condition()


def fill_details(page, channel="sms"):
    page.fill("#mv2-pnr", PNR)
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    if channel == "email":
        page.check("#mv2-channel-email")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")


def to_code_step(page, api, channel="sms"):
    api.queue("start", CODE_SENT)
    fill_details(page, channel)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-step-code:not([hidden])")


def enter_code(page, code="123456"):
    page.fill("#mv2-code", code)
    page.click("#mv2-verify")


def text(page, selector):
    return page.locator(selector).text_content().strip()


def stored_attempt(page):
    return page.evaluate("JSON.parse(sessionStorage.getItem('mv2_attempt_id') || 'null')")


def to_pending(page, api):
    """Get to the outcome_unknown polling state (Sverok result not known yet)."""
    to_code_step(page, api)
    api.queue("verify", UNKNOWN)
    api.queue("status", UNKNOWN)
    enter_code(page)
    page.wait_for_selector("#mv2-step-busy:not([hidden])")


def test_happy_path_continues_check_in(kiosk):
    page, api = kiosk()
    assert page.is_visible("#section-ebas")
    assert not page.is_visible("#section-ebas-legacy")

    to_code_step(page, api)
    (payload,) = api.calls["start"]
    assert payload == {
        "personnummer": PNR,
        "email": "player@example.invalid",
        "phone": "0700000000",
        "channel": "sms",
        "accepted_bylaws": True,
        "accepted_privacy": True,
        "bylaws_version": "2026-11-06",
        "privacy_version": "2026-11-06-ebas+app-v1",
        "checkin_id": None,
    }
    assert text(page, "#mv2-destination") == "We sent a code by text message to +46******12."
    assert page.evaluate("document.activeElement.id") == "mv2-code"

    api.queue("verify", SUBMITTED)
    enter_code(page)
    page.wait_for_selector("#section-final:not(.hidden)")
    assert api.calls["verify"] == [{"attempt_id": ATTEMPT, "code": "123456"}]
    assert "You're a member!" in text(page, "#messages")
    assert not page.is_visible("#section-ebas")
    assert page.evaluate("document.activeElement.id") == "btn-final-checkin"
    # Hygiene: nothing sensitive left behind in storage or in the (hidden) inputs.
    assert page.evaluate("JSON.stringify(localStorage)").find(PNR) == -1
    assert page.evaluate("sessionStorage.getItem('mv2_attempt_id')") is None
    assert page.input_value("#mv2-pnr") == ""


def test_old_pii_in_local_storage_is_removed_on_load(kiosk):
    page, _ = kiosk()
    page.evaluate(
        f"localStorage.setItem('personnummer', '{PNR}'); localStorage.setItem('tag', 't')"
    )
    page.reload()
    assert page.evaluate("localStorage.getItem('personnummer')") is None
    assert page.evaluate("localStorage.getItem('tag')") == "t"  # non-PII flow keys stay


def test_consent_gate_and_inline_validation(kiosk):
    page, api = kiosk()
    assert page.is_disabled("#mv2-start")
    page.check("#mv2-consent-bylaws")
    assert page.is_disabled("#mv2-start")
    page.check("#mv2-consent-privacy")
    assert page.is_enabled("#mv2-start")
    assert not page.is_visible("#mv2-consent-hint")
    page.uncheck("#mv2-consent-bylaws")
    assert page.is_disabled("#mv2-start")
    page.check("#mv2-consent-bylaws")

    page.fill("#mv2-pnr", "190001010000")
    page.fill("#mv2-email", "not-an-email")
    page.click("#mv2-start")
    assert "personal ID" in text(page, "#mv2-pnr-error")
    assert text(page, "#mv2-email-error") == "Enter a valid email address."
    assert text(page, "#mv2-phone-error") == "Enter a valid phone number."
    assert page.get_attribute("#mv2-pnr", "aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == "mv2-pnr"
    assert api.calls["start"] == []


def test_verify_locks_on_first_click(kiosk):
    page, api = kiosk()
    to_code_step(page, api)
    api.hold.add("verify")
    page.fill("#mv2-code", "123456")
    page.dblclick("#mv2-verify")
    page.evaluate(
        "for (let i = 0; i < 3; i++) document.getElementById('mv2-step-code').requestSubmit()"
    )
    page.keyboard.press("Enter")
    wait_for(page, lambda: len(api.calls["verify"]) >= 1)
    page.wait_for_selector("#mv2-step-busy:not([hidden])")  # after the short delay
    assert text(page, "#mv2-busy-text") == "Registering with Sverok – this can take up to 3 minutes"
    # A next-person action stays available while Sverok works; nothing personal stays on screen.
    assert text(page, "#mv2-reset") == "Next person – clear screen"
    assert page.is_visible("#mv2-reset")
    for selector in ("#mv2-pnr", "#mv2-email", "#mv2-phone", "#mv2-code"):
        assert page.input_value(selector) == "", selector
    assert len(api.calls["verify"]) == 1

    api.queue("verify", SUBMITTED)
    api.release()
    page.wait_for_selector("#section-final:not(.hidden)")
    assert len(api.calls["verify"]) == 1


def test_wrong_code_shows_tries_left_then_locks(kiosk):
    page, api = kiosk()
    to_code_step(page, api)
    api.queue("verify", {"ok": False, "error_code": "wrong_code", "tries_left": 2}, status=400)
    api.queue("verify", {"ok": False, "error_code": "wrong_code", "tries_left": 1}, status=400)
    api.queue("verify", {"ok": False, "error_code": "locked"}, status=423)

    enter_code(page, "111111")
    page.wait_for_selector("#mv2-error:not([hidden])")
    assert text(page, "#mv2-error") == "Wrong code. 2 tries left."
    assert page.input_value("#mv2-code") == ""
    assert page.evaluate("document.activeElement.id") == "mv2-code"
    assert page.is_enabled("#mv2-verify")

    enter_code(page, "222222")
    page.wait_for_function("document.getElementById('mv2-error').textContent.includes('1 try')")

    enter_code(page, "333333")
    page.wait_for_selector("#mv2-step-help:not([hidden])")
    assert text(page, "#mv2-help-heading") == "Too many wrong codes"
    assert "ask a TO" in text(page, "#mv2-help-text")
    assert page.evaluate("document.activeElement.id") == "mv2-help-heading"
    assert page.is_visible("#mv2-reset")


def test_expired_code_enables_resend_now(kiosk):
    page, api = kiosk()
    to_code_step(page, api)
    assert page.is_disabled("#mv2-resend")
    api.queue("verify", {"ok": False, "error_code": "expired"}, status=410)
    enter_code(page)
    page.wait_for_selector("#mv2-error:not([hidden])")
    assert "expired" in text(page, "#mv2-error")
    assert page.is_enabled("#mv2-resend")


def test_rate_limited_start_uses_retry_after(kiosk):
    page, api = kiosk(clock=True)
    api.queue(
        "start",
        {"ok": False, "error_code": "rate_limited", "user_message": "x"},
        status=429,
        headers={"Retry-After": "12"},
    )
    fill_details(page)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-error:not([hidden])")
    assert text(page, "#mv2-error") == "Too many attempts. Wait 12 seconds and try again."
    assert text(page, "#mv2-start") == "Become a member (12 s)"
    assert page.is_disabled("#mv2-start")
    page.clock.run_for(12_000)
    assert text(page, "#mv2-start") == "Become a member"
    assert page.is_enabled("#mv2-start")


def test_api_message_is_rendered_as_text(kiosk):
    page, api = kiosk()
    hostile = "<img src=x onerror=window.__pwned=1>"
    api.queue("start", {"ok": False, "error_code": "invalid_phone", "user_message": hostile}, 400)
    fill_details(page)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-error:not([hidden])")
    assert text(page, "#mv2-error") == hostile
    assert page.locator("#mv2-error img").count() == 0
    assert page.evaluate("window.__pwned") is None


def test_outcome_unknown_polls_until_submitted(kiosk):
    page, api = kiosk(clock=True)
    to_code_step(page, api)
    api.queue("verify", UNKNOWN)
    api.queue("status", UNKNOWN).queue("status", UNKNOWN).queue("status", SUBMITTED)
    enter_code(page)
    page.wait_for_selector("#mv2-step-busy:not([hidden])")
    assert text(page, "#mv2-busy-text") == "We're checking your registration…"
    assert stored_attempt(page) == {"attempt_id": ATTEMPT, "checkin_id": None}

    page.clock.run_for(4_900)
    assert api.calls["status"] == []
    for expected in (1, 2, 3):
        page.clock.run_for(5_000)
        wait_for(page, lambda: len(api.calls["status"]) >= expected)
    assert api.calls["status"][0].endswith(f"/api/membership/{ATTEMPT}/status")
    page.wait_for_selector("#section-final:not(.hidden)")
    assert page.evaluate("sessionStorage.getItem('mv2_attempt_id')") is None


def test_outcome_unknown_gives_up_after_four_minutes(kiosk):
    page, api = kiosk(clock=True)
    to_pending(page, api)
    for step in range(48):  # 48 x 5 s = 240 s
        if step % 12 == 0:
            page.dispatch_event("body", "pointerdown")  # the person is still there
        calls = len(api.calls["status"])
        page.clock.run_for(5_000)
        wait_for(
            page, lambda: len(api.calls["status"]) > calls or page.is_visible("#mv2-step-help")
        )
        if page.is_visible("#mv2-step-help"):
            break
    page.wait_for_selector("#mv2-step-help:not([hidden])")
    assert text(page, "#mv2-help-heading") == "We couldn't confirm your registration yet"
    assert "don't register again" in text(page, "#mv2-help-text")
    assert 45 <= len(api.calls["status"]) <= 49


def test_idle_while_polling_clears_screen_and_stops_tracking(kiosk):
    page, api = kiosk(clock=True, url=URL_A)
    to_pending(page, api)
    assert stored_attempt(page) == {"attempt_id": ATTEMPT, "checkin_id": "checkin-A"}
    page.clock.run_for(91_000)

    page.wait_for_selector("#mv2-step-form:not([hidden])")
    for selector in ("#mv2-pnr", "#mv2-email", "#mv2-phone", "#mv2-code"):
        assert page.input_value(selector) == "", selector
    assert stored_attempt(page) is None
    assert "previous registration is still being processed" in text(page, "#mv2-status")
    polls = len(api.calls["status"])
    api.queue("status", SUBMITTED)
    page.clock.run_for(30_000)
    page.wait_for_timeout(100)
    assert len(api.calls["status"]) == polls  # no more polling for the previous person
    assert page.is_visible("#mv2-step-form")


def test_idle_during_in_flight_verify_drops_the_late_answer(kiosk):
    page, api = kiosk(clock=True, url=URL_A)
    to_code_step(page, api)
    api.hold.add("verify")
    enter_code(page)
    wait_for(page, lambda: len(api.calls["verify"]) == 1)
    page.clock.run_for(91_000)
    page.wait_for_selector("#mv2-step-form:not([hidden])")
    assert page.input_value("#mv2-pnr") == ""

    api.queue("verify", SUBMITTED)
    api.release()  # the server finished for the previous person
    page.wait_for_timeout(200)
    assert page.is_visible("#mv2-step-form")
    assert not page.is_visible("#section-final")
    assert "You're a member" not in text(page, "#messages")


def test_same_person_reload_resumes_polling(kiosk):
    page, api = kiosk(url=URL_A)
    to_pending(page, api)
    page.reload()
    page.wait_for_selector("#mv2-step-busy:not([hidden])")
    assert not page.is_visible("#mv2-step-form")
    assert stored_attempt(page) == {"attempt_id": ATTEMPT, "checkin_id": "checkin-A"}


def test_reload_without_a_checkin_id_does_not_resume(kiosk):
    page, api = kiosk()
    to_pending(page, api)
    page.reload()
    page.wait_for_selector("#mv2-step-form:not([hidden])")
    assert stored_attempt(page) is None


@pytest.mark.parametrize("via_back_link", [True, False], ids=["back-link", "direct"])
def test_next_person_in_same_tab_never_inherits_a_pending_attempt(kiosk, via_back_link):
    page, api = kiosk(url=URL_A)
    to_pending(page, api)
    if via_back_link:
        # Run the link's click handlers but stay on the page (the mock has no "/" route).
        page.evaluate(
            "const a = document.getElementById('back-link');"
            "a.addEventListener('click', e => e.preventDefault());"
            "a.click();"
        )
    polls = len(api.calls["status"])
    api.queue("status", SUBMITTED)  # A's registration completes meanwhile

    page.goto(URL_B)  # person B's check-in, same tab
    page.wait_for_selector("#mv2-step-form:not([hidden])")
    page.wait_for_timeout(5_500)  # longer than one poll interval
    assert len(api.calls["status"]) == polls
    assert stored_attempt(page) is None
    assert page.is_visible("#section-ebas")
    assert not page.is_visible("#section-final")
    assert page.evaluate("typeof state === 'object' && state.needsEbas") is True


def test_resend_countdown(kiosk):
    page, api = kiosk(clock=True)
    to_code_step(page, api, channel="email")
    assert text(page, "#mv2-destination") == "We sent a code by email to +46******12."
    assert page.is_disabled("#mv2-resend")
    assert text(page, "#mv2-resend") == "Send again (60 s)"
    page.clock.run_for(30_000)
    assert text(page, "#mv2-resend") == "Send again (30 s)"
    page.clock.run_for(30_000)
    assert text(page, "#mv2-resend") == "Send again"
    assert page.is_enabled("#mv2-resend")

    api.queue("resend", {**CODE_SENT, "masked_destination": "p*****@example.invalid"})
    page.click("#mv2-resend")
    wait_for(page, lambda: len(api.calls["resend"]) == 1)
    assert api.calls["resend"] == [{"attempt_id": ATTEMPT}]
    page.wait_for_function("document.getElementById('mv2-resend').disabled")
    assert text(page, "#mv2-resend") == "Send again (60 s)"
    assert text(page, "#mv2-status") == "A new code has been sent."
    assert "p*****@example.invalid" in text(page, "#mv2-destination")


def test_idle_reset_clears_every_field(kiosk):
    page, api = kiosk(clock=True)
    fill_details(page, channel="email")
    page.click("text=Read bylaws")
    page.clock.run_for(89_000)
    assert page.input_value("#mv2-pnr") == PNR  # not yet

    api.queue("start", CODE_SENT)
    page.click("#mv2-start")  # activity: restarts the 90 s window
    page.wait_for_selector("#mv2-step-code:not([hidden])")
    page.fill("#mv2-code", "12")
    page.clock.run_for(91_000)

    page.wait_for_selector("#mv2-step-form:not([hidden])")
    for selector in ("#mv2-pnr", "#mv2-email", "#mv2-phone", "#mv2-code"):
        assert page.input_value(selector) == "", selector
    assert not page.is_checked("#mv2-consent-bylaws")
    assert not page.is_checked("#mv2-consent-privacy")
    assert page.is_checked("#mv2-channel-sms")
    assert page.is_disabled("#mv2-start")
    assert text(page, "#mv2-destination") == ""
    assert page.locator("#section-ebas details[open]").count() == 0
    assert page.evaluate("sessionStorage.getItem('mv2_attempt_id')") is None


def test_cancel_and_clear_button(kiosk):
    page, api = kiosk()
    to_code_step(page, api)
    page.click("#mv2-reset")
    assert page.is_visible("#mv2-step-form")
    assert page.input_value("#mv2-pnr") == ""
    assert page.evaluate("document.activeElement.id") == "mv2-pnr"


# --- Screenshots of every state (opt-in: E2E_SCREENSHOT_DIR) ------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, TABLET], ids=["desktop-1280x800", "tablet-820x1180"])
def test_capture_state_screens(kiosk, viewport, request):
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    if not out:
        pytest.skip("set E2E_SCREENSHOT_DIR to capture screenshots")
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = request.node.callspec.id

    def shot(page, name):
        page.screenshot(
            path=str(folder / f"{name}.{suffix}.png"), full_page=True, animations="disabled"
        )

    page, api = kiosk(viewport=viewport, clock=True)
    shot(page, "01-form-empty")
    page.fill("#mv2-pnr", "123")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")
    page.click("#mv2-start")
    shot(page, "02-form-errors")
    page.click("#mv2-reset")
    page.click("text=Read bylaws")
    page.click("#section-ebas details[open] summary >> text=Show Swedish original")
    shot(page, "03-bylaws-expanded")
    page.click("#mv2-reset")
    fill_details(page)
    shot(page, "04-form-ready")
    to_code_step(page, api)
    shot(page, "05-code-sent-countdown")
    api.queue("verify", {"ok": False, "error_code": "wrong_code", "tries_left": 2}, status=400)
    enter_code(page, "111111")
    page.wait_for_selector("#mv2-error:not([hidden])")
    shot(page, "06-wrong-code")

    api.queue("verify", UNKNOWN)
    api.queue("status", UNKNOWN)
    enter_code(page)
    page.wait_for_selector("#mv2-step-busy:not([hidden])")
    shot(page, "07-checking-outcome-unknown")
    page.click("#mv2-step-busy")  # no reset button while busy
    page.evaluate("window.MembershipV2.reset()")

    page, api = kiosk(viewport=viewport)
    to_code_step(page, api)
    api.hold.add("verify")
    enter_code(page)
    page.wait_for_selector("#mv2-step-busy:not([hidden])")
    shot(page, "08-submitting")
    api.queue("verify", SUBMITTED)
    api.release()
    page.wait_for_selector("#section-final:not(.hidden)")
    shot(page, "09-success-continue")

    for code, name in (("locked", "10-help-locked"), ("failed", "11-help-failed")):
        page, api = kiosk(viewport=viewport)
        to_code_step(page, api)
        api.queue("verify", {"ok": False, "error_code": code}, status=409)
        enter_code(page)
        page.wait_for_selector("#mv2-step-help:not([hidden])")
        shot(page, name)

    page, api = kiosk(viewport=viewport, clock=True)
    api.queue(
        "start",
        {"ok": False, "error_code": "rate_limited"},
        status=429,
        headers={"Retry-After": "30"},
    )
    fill_details(page)
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-error:not([hidden])")
    shot(page, "12-rate-limited")

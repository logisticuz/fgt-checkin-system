"""Legacy registration's three outcomes in a real browser, with all HTTP mocked."""

import json
import mimetypes
from urllib.parse import urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, REGISTER_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e


@pytest.fixture
def legacy_page(browser, register_html):
    contexts, errors = [], []

    def open_page(reply, status=200):
        state = {"posts": [], "member": False, "polls": []}
        context = browser.new_context(viewport=TABLET, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        html = register_html(membership_flow="legacy")

        def route_request(route):
            path = urlparse(route.request.url).path
            if path == "/register":
                return route.fulfill(content_type="text/html", body=html)
            if path.startswith("/static/"):
                asset = BACKEND_DIR / path.lstrip("/")
                if asset.is_file():
                    return route.fulfill(
                        content_type=mimetypes.guess_type(asset.name)[0]
                        or "application/octet-stream",
                        body=asset.read_bytes(),
                    )
                return route.fulfill(status=404, body="")
            if path == "/api/ebas/register":
                state["posts"].append(route.request.post_data_json)
                return route.fulfill(
                    status=status, content_type="application/json", body=json.dumps(reply)
                )
            if path.startswith("/api/checkin/") and path.endswith("/status"):
                state["polls"].append(route.request.url)
                return route.fulfill(
                    content_type="application/json",
                    body=json.dumps({"found": True, "member": state["member"]}),
                )
            if path.startswith("/api/participant/"):
                return route.fulfill(
                    content_type="application/json",
                    body=json.dumps({"member": False, "startgg": True}),
                )
            return route.abort()

        page.route("**/*", route_request)
        page.clock.install()
        page.goto(REGISTER_URL)
        page.click("#pnr-toggle-manual")
        page.fill("#pnr-manual-input", synthetic_personnummer())
        page.fill("#email-ebas", "synthetic@example.invalid")
        page.fill("#phone-ebas", "0000000000")
        return page, state

    yield open_page
    for context in contexts:
        context.close()
    assert not errors


def test_unknown_is_to_help_without_retry_or_second_post(legacy_page):
    page, state = legacy_page(
        {"success": False, "state": "outcome_unknown", "retry_allowed": False}, 202
    )
    page.click("#btn-register-ebas")
    page.wait_for_function(
        "document.getElementById('messages').textContent.includes('a TO will check')"
    )
    assert page.is_disabled("#btn-register-ebas")
    assert page.is_disabled("#pnr-manual-input")
    page.dispatch_event("#btn-register-ebas", "click")
    page.clock.run_for(30_000)
    assert len(state["posts"]) == 1
    assert page.evaluate("state.needsEbas") is True
    assert "Membership registered!" not in page.inner_text("#messages")


def test_verified_rejection_shows_text_field_errors_and_allows_retry(legacy_page):
    page, state = legacy_page(
        {
            "success": False,
            "state": "rejected",
            "retry_allowed": True,
            "field_errors": {"email": ["Check this email <img src=x>"]},
        },
        422,
    )
    page.click("#btn-register-ebas")
    page.wait_for_function("document.getElementById('messages').textContent.includes('Email:')")
    assert "Check this email <img src=x>" in page.inner_text("#messages")
    assert page.locator("#messages img").count() == 0
    assert not page.is_disabled("#btn-register-ebas")
    page.click("#btn-register-ebas")
    page.wait_for_function("document.getElementById('btn-register-ebas').disabled === false")
    assert len(state["posts"]) == 2


def test_canonical_accepted_result_completes_membership(legacy_page):
    page, state = legacy_page(
        {"success": True, "state": "registered", "member": True, "registered": True}
    )
    page.click("#btn-register-ebas")
    page.wait_for_function(
        "document.getElementById('messages').textContent.includes('Membership registered!')"
    )
    assert page.evaluate("state.needsEbas") is False
    assert not page.is_visible("#section-ebas")
    assert len(state["posts"]) == 1


def test_unacknowledged_local_write_stays_pending_until_signed_status_confirms(legacy_page):
    page, state = legacy_page(
        {"success": False, "state": "local_sync_pending", "provider_accepted": True}, 202
    )
    page.click("#btn-register-ebas")
    page.wait_for_function(
        "document.getElementById('messages').textContent.includes('a TO will finish')"
    )
    assert page.evaluate("state.needsEbas") is True
    assert page.is_disabled("#btn-register-ebas")
    page.clock.run_for(5_000)
    page.wait_for_timeout(50)
    assert len(state["posts"]) == 1
    assert all("/api/checkin/chk-test/status?t=tok-test" in url for url in state["polls"])
    state["member"] = True
    page.clock.run_for(5_000)
    page.wait_for_function(
        "document.getElementById('messages').textContent.includes('Membership registered!')"
    )
    assert page.evaluate("state.needsEbas") is False
    assert len(state["posts"]) == 1

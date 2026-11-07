"""T-032 browser flows: status by checkin_id + capability token (mocked API, real templates)."""

import json
import mimetypes
from urllib.parse import parse_qsl, urlparse

import pytest

from tests.e2e.conftest import BACKEND_DIR, BASE_URL, TABLET, synthetic_personnummer

pytestmark = pytest.mark.e2e

NAME = "Same Name"
STATUS_PENDING = {
    "found": True,
    "ready": False,
    "missing": ["Payment"],
    "member": True,
    "startgg": True,
    "payment_ok": False,
    "games": ["Street Fighter 6"],
    "status": "Pending",
}
STATUS_READY = {
    **STATUS_PENDING,
    "ready": True,
    "missing": [],
    "payment_ok": True,
    "status": "Ready",
}


@pytest.fixture
def site(browser, app_templates):  # noqa: F811
    """Factory: page + call log. The id endpoint answers per checkin_id from `statuses`."""
    contexts, errors = [], []

    def open_site(
        statuses=None,
        orchestrate=None,
        status_templates=("status_pending.html",),
        offer_membership=False,
    ):
        statuses = statuses or {}
        calls = {"id_status": [], "name_status": [], "status_pages": [], "media": []}
        base = {
            "membership_flow": "v2",
            "n8n_token": "",
            "sse_token": "",
            "kiosk": False,
            "collect_acquisition_source": False,
            "require_membership": False,
            "require_payment": True,
            "require_startgg": True,
            "swish_number": "000 000 00 00",
            "swish_expected_per_game": 25,
            "tag": "same",
            "games": [],
            "event_slug": "synthetic-event",
            "event_name": "Synthetic Event",
            "status": {},
            "offer_membership": offer_membership,
            "is_member": False,
            "membership_year": 2026,
        }

        def json_reply(route, body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        def router(route):
            url = urlparse(route.request.url)
            if route.request.resource_type == "media" or url.path.endswith("/clear.mp3"):
                calls["media"].append(route.request.url)
            query = dict(parse_qsl(url.query))
            if url.path == "/":
                html = app_templates.get_template("checkin.html").render(base)
                return route.fulfill(content_type="text/html", body=html)
            if url.path.startswith("/status/"):
                calls["status_pages"].append(route.request.url)
                index = min(len(calls["status_pages"]), len(status_templates)) - 1
                context = {**base, "name": NAME}
                # Stand-in for T-024's status_context: a "verified" id/token pair goes in.
                if query.get("checkin_id") and query.get("t") == f"tok-{query['checkin_id']}":
                    context.update(checkin_id=query["checkin_id"], status_token=query["t"])
                html = app_templates.get_template(status_templates[index]).render(context)
                return route.fulfill(content_type="text/html", body=html)
            if url.path == "/register":
                html = app_templates.get_template("register.html").render({**base, "name": NAME})
                return route.fulfill(content_type="text/html", body=html)
            if url.path == "/api/membership/start":
                return json_reply(
                    route,
                    {
                        "ok": True,
                        "state": "code_sent",
                        "attempt_id": "att-1",
                        "masked_destination": "+46******00",
                        "resend_after_s": 60,
                    },
                )
            if url.path == "/api/membership/verify":
                return json_reply(route, {"ok": True, "state": "submitted"})
            if url.path.startswith("/static/"):
                file = BACKEND_DIR / url.path.lstrip("/")
                if file.is_file():
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return route.fulfill(content_type=kind, body=file.read_bytes())
                return route.fulfill(status=404, body="")
            if url.path == "/api/checkin/orchestrate":
                return json_reply(route, orchestrate)
            if url.path.startswith("/api/checkin/") and url.path.endswith("/status"):
                checkin_id = url.path.split("/")[3]
                calls["id_status"].append((checkin_id, query.get("t")))
                if query.get("t") != f"tok-{checkin_id}":
                    return json_reply(route, {"found": False, "ready": False}, status=403)
                return json_reply(route, statuses.get(checkin_id, {"found": False, "ready": False}))
            if url.path.startswith("/api/participant/"):
                calls["name_status"].append(route.request.url)
                return json_reply(route, {"ready": True, "status": "Ready", "name": NAME})
            if url.path.startswith("/api/"):
                return json_reply(route, {}, status=404)
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


def wait_for(page, condition, timeout_ms=4000):
    for _ in range(timeout_ms // 25):
        if condition():
            return
        page.wait_for_timeout(25)
    assert condition()


def test_checkin_to_status_polls_the_id_endpoint_with_the_token(site):
    page, calls = site(
        statuses={"chk-a": STATUS_PENDING},
        orchestrate={"ready": True, "checkin_id": "chk-a", "status_token": "tok-chk-a"},
    )
    page.goto("/")
    page.fill("#namn", NAME)
    page.fill("#telefon", "0700000000")
    page.fill("#tag", "same")
    page.check("#consent")
    page.click(".btn-submit")
    page.wait_for_url("**/status/**")
    assert dict(parse_qsl(urlparse(page.url).query)) == {"checkin_id": "chk-a", "t": "tok-chk-a"}
    wait_for(page, lambda: calls["id_status"])
    assert calls["id_status"][0] == ("chk-a", "tok-chk-a")
    page.wait_for_function("document.getElementById('statusText').textContent === 'Pending'")
    assert calls["name_status"] == []  # never the name lookup
    assert (
        page.evaluate("JSON.stringify(localStorage) + JSON.stringify(sessionStorage)").find(
            "tok-chk-a"
        )
        == -1
    )


def test_expired_token_shows_the_expired_message_and_stops(site):
    page, calls = site(statuses={"chk-a": STATUS_PENDING})
    # A token the router's "verification" accepts, but the API now rejects (expired since).
    page.route(
        "**/api/checkin/chk-a/status*",
        lambda route: route.fulfill(
            status=403, content_type="application/json", body='{"found":false,"ready":false}'
        ),
    )
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page.wait_for_function(
        "document.getElementById('statusText').textContent"
        " === 'Your status link expired – ask a TO or check in again'"
    )
    assert page.locator("#swish-section").is_hidden()
    assert calls["name_status"] == []


def test_two_people_with_the_same_name_each_see_their_own_status(site):
    statuses = {"chk-a": STATUS_PENDING, "chk-b": STATUS_READY}
    page_a, calls_a = site(statuses=statuses)
    page_b, calls_b = site(statuses=statuses)  # server picks pending by name for both

    page_a.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page_b.goto(f"/status/{NAME}?checkin_id=chk-b&t=tok-chk-b")

    page_a.wait_for_function("document.getElementById('statusText').textContent === 'Pending'")
    # B is ready by id: one redirect carrying B's own id/token, then the loop guard renders
    # ready inline even though the (name-based) server serves the pending page again.
    wait_for(page_b, lambda: len(calls_b["status_pages"]) >= 2)
    redirected = dict(parse_qsl(urlparse(calls_b["status_pages"][1]).query))
    assert redirected == {"checkin_id": "chk-b", "t": "tok-chk-b", "confirmed": "1"}
    page_b.wait_for_function(
        "document.getElementById('statusText').textContent === \"Ready! You're all set.\""
    )
    page_b.wait_for_timeout(300)
    assert len(calls_b["status_pages"]) == 2  # no redirect loop

    assert {cid for cid, _ in calls_a["id_status"]} == {"chk-a"}
    assert {cid for cid, _ in calls_b["id_status"]} == {"chk-b"}
    assert calls_a["name_status"] == calls_b["name_status"] == []
    assert len(calls_a["status_pages"]) == 1  # A stays on its pending page


SUCCESS = "[data-success]"


def success_visibility(page):
    return page.eval_on_selector_all(SUCCESS, "els => els.map(el => !el.hidden)")


@pytest.mark.parametrize(
    "answer, message",
    [
        (
            (403, '{"found":false,"ready":false}'),
            "Your status link expired – ask a TO or check in again",
        ),
        ((500, "{}"), "We couldn't confirm your check-in right now – ask a TO."),
        ((200, "not json"), "We couldn't confirm your check-in right now – ask a TO."),
        ((None, None), "We couldn't confirm your check-in right now – ask a TO."),
        ((200, json.dumps(STATUS_PENDING)), "Your check-in is not complete yet – ask a TO."),
        ((200, '{"found":false,"ready":false}'), "We couldn't find your check-in – ask a TO"),
    ],
    ids=["expired-403", "server-500", "malformed-json", "network-error", "not-ready", "not-found"],
)
def test_ready_page_shows_no_success_signal_without_a_positive_id_confirmation(
    site, answer, message
):
    page, calls = site(status_templates=("status_ready.html",))
    status, body = answer

    def respond(route):
        if status is None:
            return route.abort("connectionreset")
        return route.fulfill(status=status, content_type="application/json", body=body)

    page.route("**/api/checkin/chk-a/status*", respond)
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page.wait_for_function(
        "document.getElementById('status-check-notice').dataset.tone === 'error'"
    )
    assert page.inner_text("#status-check-notice") == message
    assert success_visibility(page) and not any(success_visibility(page))
    assert page.locator(".success-icon").is_hidden()
    assert page.locator("h1").is_hidden()
    assert page.locator("audio").count() == 0
    assert calls["media"] == []


def test_ready_page_reveals_success_only_after_the_id_confirms_ready(site):
    page, calls = site(statuses={"chk-a": STATUS_READY}, status_templates=("status_ready.html",))
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page.wait_for_selector(".success-icon:not([hidden])")
    assert all(success_visibility(page))
    assert page.locator("#status-check-notice").is_hidden()
    assert page.locator("audio").count() == 0
    assert calls["media"] == []
    assert calls["id_status"] == [("chk-a", "tok-chk-a")]


def test_join_as_member_and_v2_success_keep_the_capability(site):
    page, calls = site(
        statuses={"chk-a": STATUS_READY},
        status_templates=("status_ready.html",),
        offer_membership=True,
    )
    page.goto(f"/status/{NAME}?checkin_id=chk-a&t=tok-chk-a")
    page.wait_for_selector("a.support-link:visible")
    page.click("a.support-link")
    page.wait_for_url("**/register?**")
    assert dict(parse_qsl(urlparse(page.url).query))["t"] == "tok-chk-a"

    page.fill("#mv2-pnr", synthetic_personnummer())
    page.fill("#mv2-email", "player@example.invalid")
    page.fill("#mv2-phone", "0700000000")
    page.check("#mv2-consent-bylaws")
    page.check("#mv2-consent-privacy")
    page.click("#mv2-start")
    page.wait_for_selector("#mv2-step-code:not([hidden])")
    page.fill("#mv2-code", "123456")
    page.click("#mv2-verify")

    wait_for(page, lambda: len(calls["status_pages"]) >= 2, timeout_ms=6000)  # 2 s success pause
    back = dict(parse_qsl(urlparse(calls["status_pages"][1]).query))
    assert back == {"checkin_id": "chk-a", "t": "tok-chk-a"}
    wait_for(page, lambda: len(calls["id_status"]) >= 2)  # the returned page polls by id again
    assert calls["id_status"][-1] == ("chk-a", "tok-chk-a")
    assert calls["name_status"] == []


def test_legacy_link_without_token_keeps_the_name_lookup(site):
    page, calls = site()
    page.goto(f"/status/{NAME}")
    wait_for(page, lambda: calls["name_status"])
    assert calls["id_status"] == []

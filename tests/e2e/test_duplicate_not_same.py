"""T-056 e2e: the real duplicate view + real callbacks on the disposable test Postgres.

scan -> "Not same" -> the card disappears -> re-scan -> still gone -> Undo -> it's back.
A real Dash server runs on 127.0.0.1 with an OS-assigned port; no external network.
"""

import os
import socket
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests.test_safe_archiving import archive_db, seed_player  # noqa: F401  (pytest fixture)

pytestmark = [pytest.mark.e2e, pytest.mark.db]

DESKTOP = {"width": 1280, "height": 900}


def find(component, wanted_id):
    if getattr(component, "id", None) == wanted_id:
        return component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if hasattr(child, "to_plotly_json"):
            hit = find(child, wanted_id)
            if hit is not None:
                return hit
    return None


@pytest.fixture
def dashboard_url(archive_db):  # noqa: F811
    from dash import Dash, html

    from fgt_dashboard.callbacks import register_callbacks
    from fgt_dashboard.layout import create_layout

    def duplicates_only():
        # The production layout, narrowed to the Insights -> Duplicates view (made visible).
        view = find(create_layout(), "insights-view-duplicates")
        assert view is not None, "duplicates view not found in the real layout"
        view.style = {**(view.style or {}), "display": "block"}
        return html.Div(view, style={"backgroundColor": "#0b1220", "padding": "1.5rem"})

    assets = Path(__file__).resolve().parents[2] / "fgt_dashboard" / "assets"
    app = Dash(__name__, suppress_callback_exceptions=True, assets_folder=str(assets))
    app.layout = duplicates_only
    register_callbacks(app)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = make_server("127.0.0.1", port, app.server, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def shot(page, name):
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True, animations="disabled")


def open_dismissed(page):
    page.locator("#duplicates-dismissed-details").evaluate("details => { details.open = true; }")


def card(page, a, b):
    # Dash renders dict ids as a JSON string in the id attribute.
    return (
        page.locator('#duplicates-list [id*="dup-card"]')
        .filter(has_text=f"Tag: {a['tag']}")
        .filter(has_text=f"Tag: {b['tag']}")
    )


def open_page(browser, dashboard_url):
    """A page that records JS errors and failed Dash callback requests (HTTP >= 400)."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    problems = []
    page.on("pageerror", lambda error: problems.append(f"js: {error}"))
    page.on(
        "response",
        lambda r: (
            problems.append(f"http {r.status}: {r.url}")
            if "_dash-update-component" in r.url and r.status >= 400
            else None
        ),
    )
    page.goto(dashboard_url)
    return context, page, problems


def title_is(page, text):
    page.wait_for_function(
        "t => document.getElementById('duplicates-title').textContent === t", arg=text
    )


def test_not_same_sticks_across_scans_and_can_be_undone(
    browser, dashboard_url, archive_db
):  # noqa: F811
    one = seed_player(archive_db, "syn-kenji", name="Synthetic Kenji")
    two = seed_player(archive_db, "syn-kenjj", name="Synthetic Kenji")  # same phone, similar tag
    other_a = seed_player(archive_db, "syn-mira", name="Synthetic Mira", telephone="0000000002")
    other_b = seed_player(archive_db, "syn-mila", name="Synthetic Mila", telephone="0000000003")

    context, page, errors = open_page(browser, dashboard_url)
    try:
        page.click("#btn-scan-duplicates")
        page.wait_for_function(
            "document.getElementById('duplicates-title').textContent.includes('(2)')"
        )
        target = card(page, one, two)
        assert target.count() == 1
        shot(page, "T-056-1-scan")

        target.get_by_role("button", name="Not same").click()
        page.wait_for_function(
            "document.getElementById('duplicates-title').textContent.includes('(1)')"
        )
        assert not target.is_visible()
        assert card(page, other_a, other_b).first.is_visible()  # the other card stays put
        assert page.inner_text("#duplicates-feedback") == "Marked as different players."
        assert "Dismissed pairs (1)" in page.inner_text("#duplicates-dismissed")
        open_dismissed(page)
        shot(page, "T-056-2-after-not-same")

        page.click("#btn-scan-duplicates")  # a full re-scan: the decision is persisted
        # The hidden card is still in the DOM until the re-scan response replaces the list,
        # so wait for its removal (not for the title, which already says (1)).
        expect(card(page, one, two)).to_have_count(0)
        expect(card(page, other_a, other_b)).to_have_count(1)
        title_is(page, "Potential duplicates (1)")
        shot(page, "T-056-3-rescan")

        open_dismissed(page)
        page.locator(".dup-dismissed-row").get_by_role("button", name="Undo").click()
        page.wait_for_function(
            "document.getElementById('duplicates-title').textContent.includes('(2)')"
        )
        assert card(page, one, two).count() == 1
        assert "Dismissed pairs" not in page.inner_text("#duplicates-list")
        shot(page, "T-056-4-after-undo")
    finally:
        context.close()
    assert not errors, errors


def test_overlapping_not_same_clicks_keep_both_cards_hidden(
    browser, dashboard_url, archive_db, monkeypatch
):  # noqa: F811
    """Review P2: the first response is delayed until the second click has been saved."""
    import shared.storage as storage

    players = []
    for i, (tag, near) in enumerate(
        [("syn-ayla", "syn-ayle"), ("syn-bodo", "syn-bodi"), ("syn-cleo", "syn-clea")]
    ):
        phone = f"00000001{i}0"
        players.append(
            (
                seed_player(archive_db, tag, name=f"Synthetic {tag}", telephone=phone),
                seed_player(archive_db, near, name=f"Synthetic {near}", telephone=phone),
            )
        )

    real_dismiss = storage.dismiss_duplicate_pair
    second_saved = threading.Event()
    calls = []

    def gated_dismiss(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:  # hold the first save's response until the second is saved
            assert second_saved.wait(timeout=10), "second click never reached the server"
            return real_dismiss(*args, **kwargs)
        result = real_dismiss(*args, **kwargs)
        second_saved.set()
        return result

    monkeypatch.setattr(storage, "dismiss_duplicate_pair", gated_dismiss)

    context, page, problems = open_page(browser, dashboard_url)
    try:
        page.click("#btn-scan-duplicates")
        title_is(page, "Potential duplicates (3)")
        first, second, third = (card(page, a, b) for a, b in players)
        first.get_by_role("button", name="Not same").click()
        second.get_by_role("button", name="Not same").click()

        title_is(page, "Potential duplicates (1)")
        expect(first).to_be_hidden()
        expect(second).to_be_hidden()
        expect(third).to_be_visible()
        page.wait_for_function(
            "document.getElementById('duplicates-dismissed').textContent"
            ".includes('Dismissed pairs (2)')"
        )
        page.wait_for_timeout(300)  # let any late response land; nothing may re-appear
        expect(first).to_be_hidden()
        expect(second).to_be_hidden()
        title_is(page, "Potential duplicates (1)")
        assert len(calls) == 2
        shot(page, "T-056-5-overlapping-clicks")
    finally:
        context.close()
    assert not problems, problems

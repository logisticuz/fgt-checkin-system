"""T-068 e2e: live "possible duplicate" in Needs attention, on the real layout + test Postgres.

A real Dash server (127.0.0.1, OS port) runs the production create_layout() and callbacks.
A check-in arrives while the dashboard is open; the refresh the SSE client performs on a
check-in event (it clicks #btn-refresh) brings the hint in. Synthetic players only.
"""

import os
import socket
import threading
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from shared import postgres_api as postgres
from tests.test_safe_archiving import archive_db, seed_player  # noqa: F401  (pytest fixture)

pytestmark = [pytest.mark.e2e, pytest.mark.db]
DESKTOP = {"width": 1440, "height": 1000}


@pytest.fixture
def live_slug(archive_db):  # noqa: F811
    slug = "pw-live-" + uuid.uuid4().hex[:8]
    archive_db.seed_settings(
        active_event_slug=slug,
        event_display_name="Synthetic Live Event",
        require_membership=False,
        require_startgg=False,
        require_payment=False,
    )
    return slug


@pytest.fixture
def dashboard_url(archive_db, live_slug):  # noqa: F811
    from dash import Dash

    from fgt_dashboard.callbacks import register_callbacks
    from fgt_dashboard.layout import create_layout

    assets = Path(__file__).resolve().parents[2] / "fgt_dashboard" / "assets"
    app = Dash(__name__, suppress_callback_exceptions=True, assets_folder=str(assets))
    app.layout = create_layout
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


def shot(page, name, target=None):
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    if not out:
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    path = str(Path(out) / f"{name}.png")
    (target or page).screenshot(path=path, animations="disabled")


def row(database, record_id):
    with database.pool.connection() as connection:
        return connection.execute(
            "SELECT player_uuid, tag FROM active_event_data WHERE record_id = %s", (record_id,)
        ).fetchone()


def refresh(page):
    page.click("#btn-refresh")  # what assets/sse-client.js does on a check-in event


def test_live_duplicate_flow(browser, dashboard_url, archive_db, live_slug):  # noqa: F811
    kenji = seed_player(archive_db, "pw-kenji", name="Synthetic Kenji", telephone="0700000301")
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    postgres.begin_checkin(  # an unrelated, already checked-in player
        live_slug, {"name": "Synthetic Other", "tag": "pw-other", "telephone": "0700000300"}
    )

    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(dashboard_url)
        page.wait_for_selector("#checkins-table")
        expect(page.locator(".live-dup-item")).to_have_count(0)

        # A new check-in arrives mid-event with a near-identical tag.
        record = postgres.begin_checkin(
            live_slug, {"name": "Synthetic Kenji", "tag": "pw-kenjj", "telephone": "0700000399"}
        )["checkin_id"]
        refresh(page)
        item = page.locator(".live-dup-item", has_text="pw-kenjj ↔ pw-kenji")
        expect(item).to_have_count(1, timeout=15000)
        expect(page.locator("#needs-attention-section")).to_be_visible()
        expect(
            page.locator("#checkins-table td[data-dash-column='dup_hint']").first
        ).to_contain_text("pw-kenji")
        shot(page, "T-068-1-needs-attention-item", page.locator("#needs-attention-section"))

        # The dialog: both profiles side by side; Cancel changes nothing.
        item.click()
        dialog = page.locator("#live-dup-modal")
        expect(dialog).to_be_visible()
        expect(page.locator("#btn-live-dup-keep-a")).to_have_text("Keep pw-kenjj")
        expect(page.locator("#btn-live-dup-keep-b")).to_have_text("Keep pw-kenji")
        body = page.inner_text("#live-dup-body")
        assert "0700000399" not in body and "0700000301" not in body  # masked
        assert "similar tag" in body
        shot(page, "T-068-2-dialog", dialog)
        page.click("#btn-live-dup-cancel")
        expect(dialog).to_be_hidden()
        assert row(archive_db, record)[0] is None  # no stub, no merge

        # Not same → dismissed (T-056); the item goes away and Insights won't suggest it.
        item.click()
        expect(dialog).to_be_visible()
        page.click("#btn-live-dup-not-same")
        expect(page.locator("#live-dup-feedback")).to_have_text("Marked as different players.")
        expect(page.locator(".live-dup-item", has_text="pw-kenjj")).to_have_count(0, timeout=15000)
        stub = row(archive_db, record)[0]
        assert stub and stub != kenji["uuid"]
        pairs = {
            tuple(sorted((c["player_a"]["uuid"], c["player_b"]["uuid"])))
            for c in postgres.find_duplicate_candidates()
        }
        assert tuple(sorted((stub, kenji["uuid"]))) not in pairs
        shot(page, "T-068-3-after-not-same")

        # Keep B → merged with the existing tool; visible in merge history; undo works.
        second = postgres.begin_checkin(
            live_slug, {"name": "Synthetic Alpha", "tag": "pw-alfa", "telephone": "0700000302"}
        )["checkin_id"]
        refresh(page)
        item = page.locator(".live-dup-item", has_text="pw-alfa ↔ pw-alpha")
        expect(item).to_have_count(1, timeout=15000)
        item.click()
        expect(page.locator("#btn-live-dup-keep-b")).to_have_text("Keep pw-alpha")
        page.click("#btn-live-dup-keep-b")
        expect(page.locator("#live-dup-feedback")).to_contain_text("Merged: kept pw-alpha")
        expect(page.locator(".live-dup-item", has_text="pw-alfa")).to_have_count(0, timeout=15000)
        linked = row(archive_db, second)
        assert linked == (alpha["uuid"], "pw-alfa")  # today's tag stays on the row
        history = postgres.get_merge_history(limit=1)
        assert history and history[0]["keep_uuid"] == alpha["uuid"]
        shot(page, "T-068-4-after-merge")

        postgres.undo_merge(int(history[0]["id"]))
        assert row(archive_db, second)[0] not in (None, alpha["uuid"])  # back on its own stub
    finally:
        context.close()
    assert not errors, errors

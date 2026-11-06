"""T-029: confirmation dialogs, the "paid the difference?" prompt and the SSE stopped banner."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from dash import no_update
from dash._callback_context import context_value
from dash._utils import AttributeDict

from tests.test_dashboard_row_identity import dashboard  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"active_event_slug": "fightbox-4", "event_display_name": "Fightbox 4"}
EVENT_OPTIONS = [
    {"label": "Fightbox 4", "value": "fightbox-4"},
    {"label": "Fightbox 5", "value": "fightbox-5"},
]


def inputs_of(dashboard, name):  # noqa: F811
    return [(i["id"], i["property"]) for i in dashboard.definitions[name]["inputs"]]


def trigger(prop_id):
    """Pretend Dash triggered the callback from `prop_id` (needed for ctx.triggered_id)."""
    context_value.set(AttributeDict(triggered_inputs=[{"prop_id": prop_id, "value": 1}]))


@pytest.fixture
def storage(dashboard):  # noqa: F811
    store = dashboard.storage
    store.get_active_settings.return_value = dict(ACTIVE)
    store.archive_event = Mock(return_value={"event_slug": "x", "archived": 3})
    store.reopen_event = Mock(return_value={"event_slug": "x", "restored_rows": 2})
    store.get_event_history = Mock(return_value=[])
    store.get_all_event_slugs = Mock(return_value=["fightbox-4"])
    return store


# --- (a) archive / reopen / fetch need a confirmation naming both events ----------------------


def test_destructive_actions_only_run_from_the_confirm_dialogs(dashboard):  # noqa: F811
    assert inputs_of(dashboard, "archive_current_event") == [
        ("confirm-archive-event-dialog", "submit_n_clicks")
    ]
    assert inputs_of(dashboard, "reopen_archived_event") == [
        ("confirm-reopen-event-dialog", "submit_n_clicks")
    ]
    fetch_inputs = inputs_of(dashboard, "fetch_event_data")
    assert ("confirm-fetch-event-dialog", "submit_n_clicks") in fetch_inputs
    assert ("btn-fetch-event", "n_clicks") not in fetch_inputs
    # The buttons now only open the dialogs.
    assert inputs_of(dashboard, "show_archive_confirmation") == [
        ("btn-archive-event-quick", "n_clicks"),
        ("btn-archive-event", "n_clicks"),
    ]
    assert inputs_of(dashboard, "show_reopen_confirmation") == [("btn-reopen-event", "n_clicks")]
    assert inputs_of(dashboard, "show_fetch_confirmation") == [("btn-fetch-event", "n_clicks")]


def test_archive_dialog_names_both_events_and_does_not_archive(dashboard, storage):  # noqa: F811
    shown, message, target, feedback = dashboard.callbacks["show_archive_confirmation"](
        1, None, "fightbox-5", EVENT_OPTIONS, ["clear"]
    )
    assert shown is True and feedback is no_update
    assert "Event to archive: Fightbox 5 (fightbox-5)" in message
    assert "Current active event: Fightbox 4 (fightbox-4)" in message
    assert "NOT the current active event" in message
    assert "CLEARED" in message
    assert target == {"slug": "fightbox-5", "clear_active": True}
    storage.archive_event.assert_not_called()


def test_archive_dialog_refuses_without_a_specific_event(dashboard, storage):  # noqa: F811
    shown, _, target, feedback = dashboard.callbacks["show_archive_confirmation"](
        None, 1, "__ALL__", EVENT_OPTIONS, []
    )
    assert shown is False and target is None
    assert "Select a specific event" in feedback.children


def test_archive_runs_only_on_confirmation_and_on_the_confirmed_target(
    dashboard, storage
):  # noqa: F811
    archive = dashboard.callbacks["archive_current_event"]
    assert archive(None, {"slug": "fightbox-5", "clear_active": False}, {}) == (no_update,) * 3
    storage.archive_event.assert_not_called()

    archive(1, {"slug": "fightbox-5", "clear_active": False}, {})
    kwargs = storage.archive_event.call_args.kwargs
    assert kwargs["event_slug"] == "fightbox-5"
    assert kwargs["clear_active"] is False


def test_reopen_dialog_and_confirmed_reopen(dashboard, storage):  # noqa: F811
    options = [{"label": "FN19  (2026-10-01)", "value": "fn19"}]
    shown, message, target, _ = dashboard.callbacks["show_reopen_confirmation"](
        1, "fn19", options, ["restore"]
    )
    assert shown is True
    assert "Event to reopen: FN19  (2026-10-01) (fn19)" in message
    assert "Current active event: Fightbox 4 (fightbox-4)" in message
    assert "restored from the archive" in message
    storage.reopen_event.assert_not_called()

    reopen = dashboard.callbacks["reopen_archived_event"]
    assert reopen(None, target, {}) == (no_update,) * 4
    storage.reopen_event.assert_not_called()
    reopen(1, target, {})
    kwargs = storage.reopen_event.call_args.kwargs
    assert kwargs["event_slug"] == "fn19" and kwargs["restore_active"] is True


def test_fetch_dialog_names_new_and_current_event(dashboard, storage):  # noqa: F811
    show = dashboard.callbacks["show_fetch_confirmation"]
    shown, message, target, _ = show(1, " https://www.start.gg/tournament/fightbox-6/details ")
    assert shown is True
    assert "New event: fightbox-6" in message
    assert "Current active event: Fightbox 4 (fightbox-4)" in message
    assert "REPLACES the active event" in message
    assert target == {
        "link": "https://www.start.gg/tournament/fightbox-6/details",
        "slug": "fightbox-6",
    }

    same = show(1, "https://www.start.gg/tournament/fightbox-4")[1]
    assert "refreshes the current event" in same

    assert show(1, "")[0] is False and "Enter a Start.gg link" in show(1, "")[3]
    assert show(1, "https://example.com/x")[0] is False


def test_fetch_runs_only_after_confirmation_with_the_confirmed_link(
    dashboard, storage
):  # noqa: F811
    fetch = dashboard.callbacks["fetch_event_data"]
    target = {"link": "https://www.start.gg/tournament/fightbox-6", "slug": "fightbox-6"}
    assert fetch(None, None, target, None, None, {}) == (no_update,) * 3

    trigger("confirm-fetch-event-dialog.submit_n_clicks")
    # No API key in tests: reaching that guard proves the confirmed link was used.
    assert fetch(1, None, target, None, None, {})[0] == "❌ Missing STARTGG_API_KEY."
    assert fetch(1, None, None, None, None, {})[0] == "❌ Enter a Start.gg link first."
    storage.update_settings.assert_not_called()


# --- (b) games up on an already paid player -> prompt ------------------------------------------


@pytest.fixture
def games(storage):
    storage.get_active_settings.return_value = {
        **ACTIVE,
        "default_game": ["Street Fighter 6", "Tekken 8"],
        "swish_expected_per_game": 25,
        "require_payment": True,
    }
    storage.begin_checkin = Mock(return_value={"record_id": "rec-1", "created": False})
    storage.get_checkin_by_record_id = Mock(
        return_value={
            "record_id": "rec-1",
            "fields": {
                "tournament_games_registered": ["Street Fighter 6"],
                "payment_valid": True,
                "payment_amount": 25,
            },
        }
    )
    return storage


def add(dashboard, games_list):  # noqa: F811
    return dashboard.callbacks["add_manual_checkin"](
        1, "Player One", "p1", games_list, "fightbox-4", {}
    )


def test_paid_player_with_more_games_gets_the_prompt_and_nothing_is_written(
    dashboard, games
):  # noqa: F811
    result = add(dashboard, ["Street Fighter 6", "Tekken 8"])
    feedback, _, _, _, pending, style, question = result
    assert "already paid" in feedback.children
    assert style["display"] == "block"
    assert pending == {
        "record_id": "rec-1",
        "games": ["Street Fighter 6", "Tekken 8"],
        "old_games": ["Street Fighter 6"],
        "new_expected": 50,
        "old_amount": 25,
        "name": "Player One",
        "slug": "fightbox-4",
    }
    assert "50 kr" in question and "25 kr" in question
    games.update_checkin.assert_not_called()


def test_yes_updates_the_amount_and_keeps_paid(dashboard, games):  # noqa: F811
    pending = add(dashboard, ["Street Fighter 6", "Tekken 8"])[4]
    feedback, cleared, style = dashboard.callbacks["games_paid_difference_yes"](1, pending)
    games.update_checkin.assert_called_once_with(
        "rec-1",
        {
            "tournament_games_registered": ["Street Fighter 6", "Tekken 8"],
            "payment_expected": 50,
            "payment_amount": 50,
        },
    )
    assert cleared is None and style == {"display": "none"}
    assert "50 kr" in feedback.children


def test_no_marks_the_player_unpaid(dashboard, games):  # noqa: F811
    pending = add(dashboard, ["Street Fighter 6", "Tekken 8"])[4]
    dashboard.callbacks["games_paid_difference_no"](1, pending)
    games.update_checkin.assert_called_once_with(
        "rec-1",
        {
            "tournament_games_registered": ["Street Fighter 6", "Tekken 8"],
            "payment_expected": 50,
            "payment_valid": False,
        },
    )


def test_cancel_changes_nothing(dashboard, games):  # noqa: F811
    pending = add(dashboard, ["Street Fighter 6", "Tekken 8"])[4]
    _, cleared, style = dashboard.callbacks["games_paid_difference_cancel"](1, pending)
    games.update_checkin.assert_not_called()
    assert cleared is None and style == {"display": "none"}


@pytest.mark.parametrize(
    "fields",
    [
        {"tournament_games_registered": ["Street Fighter 6"], "payment_valid": False},
        {
            "tournament_games_registered": ["Street Fighter 6"],
            "payment_valid": True,
            "payment_amount": 50,
        },
    ],
    ids=["not-paid", "already-covers-it"],
)
def test_no_prompt_when_not_paid_or_amount_already_covers(dashboard, games, fields):  # noqa: F811
    games.get_checkin_by_record_id.return_value = {"record_id": "rec-1", "fields": fields}
    result = add(dashboard, ["Street Fighter 6", "Tekken 8"])
    assert result[4] is no_update and result[5] is no_update
    games.update_checkin.assert_called_once_with(
        "rec-1",
        {"tournament_games_registered": ["Street Fighter 6", "Tekken 8"], "payment_expected": 50},
    )
    assert "games updated" in result[0].children


def test_unknown_game_is_rejected_before_any_write(dashboard, games):  # noqa: F811
    result = add(dashboard, ["Street Fighter 6", "Made Up Game"])
    assert "Unknown game" in result[0].children
    games.begin_checkin.assert_not_called()
    games.update_checkin.assert_not_called()


def test_unchanged_games_and_new_players_do_not_touch_games(dashboard, games):  # noqa: F811
    add(dashboard, ["Street Fighter 6"])
    games.update_checkin.assert_not_called()

    games.begin_checkin.return_value = {"record_id": "rec-2", "created": True}
    games.get_checkin_by_record_id.reset_mock()
    add(dashboard, ["Street Fighter 6", "Tekken 8"])
    games.get_checkin_by_record_id.assert_not_called()
    games.update_checkin.assert_not_called()


# --- (3) SSE stopped banner (browser, opt-in) --------------------------------------------------

FAKE_EVENT_SOURCE = """
class FakeEventSource {
  constructor(url) {
    this.readyState = 0;
    setTimeout(() => {
      if (window.__sseShouldOpen) { this.readyState = 1; this.onopen && this.onopen({}); }
      else { this.readyState = 2; this.onerror && this.onerror({}); }
    }, 10);
  }
  addEventListener() {}
  close() { this.readyState = 2; }
}
FakeEventSource.OPEN = 1; FakeEventSource.CLOSED = 2;
window.EventSource = FakeEventSource;
"""


@pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="browser test; set RUN_E2E=1")
def test_sse_banner_appears_after_retries_and_goes_away_on_reconnect():
    sync_api = pytest.importorskip("playwright.sync_api")
    script = (ROOT / "fgt_dashboard" / "assets" / "sse-client.js").read_text(encoding="utf-8")
    css = (ROOT / "fgt_dashboard" / "assets" / "sse-banner.css").read_text(encoding="utf-8")
    html = (
        f"<html><head><style>{css}</style></head><body>"
        "<span id='connection-dot'></span><span id='connection-text'></span>"
        f"<script>{script}</script></body></html>"
    )
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200 if route.request.url.endswith("/dash") else 404,
                content_type="text/html; charset=utf-8",
                body=html,
            ),
        )
        page.add_init_script(FAKE_EVENT_SOURCE)
        page.clock.install()
        page.goto("http://dash.test/dash")
        for _ in range(12):  # 1 s start + 10 reconnects x 3 s
            page.clock.run_for(3_500)
            page.wait_for_timeout(30)
        banner = page.locator("#sse-stopped-banner")
        assert banner.is_visible()
        assert banner.inner_text() == "Live updates stopped – click to reload"
        assert banner.get_attribute("role") == "alert"
        assert page.inner_text("#connection-text") == "Disconnected"

        page.evaluate(
            "window.__sseShouldOpen = true; document.dispatchEvent(new Event('visibilitychange'))"
        )
        page.wait_for_timeout(100)
        assert page.locator("#sse-stopped-banner").count() == 0
        browser.close()

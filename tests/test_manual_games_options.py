"""T-066: selected-event options, degraded sources, and unchanged raw stored games."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared import event_games, postgres_api as postgres
from tests.test_dashboard_dialogs import games, storage  # noqa: F401
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_safe_archiving import seed_archive

RAW_GG = "GUILTY GEAR -STRIVE-"
CANONICAL_GG = "Guilty Gear Strive"


@pytest.fixture
def manual(dashboard, monkeypatch):
    dashboard.storage.get_active_settings.return_value = {
        "active_event_slug": "fightbox-5",
        "default_game": ["Wrong Active Game"],
        "events_json": [{"name": "Wrong Active Game"}],
    }
    dashboard.storage.get_active_settings_with_id.return_value = {
        "record_id": "synthetic-settings",
        "fields": dashboard.storage.get_active_settings.return_value,
    }
    dashboard.storage.get_recorded_event_games = Mock(return_value=[RAW_GG])
    dashboard.storage.get_game_aliases = Mock(return_value={RAW_GG.lower(): CANONICAL_GG})
    callback = dashboard.callbacks["fill_manual_game_options"]
    lookup = Mock(return_value=([], "provider lookup failed"))
    monkeypatch.setitem(callback.__globals__, "fetch_tournament_games", lookup)
    return callback, lookup


def options(manual, slug="fightbox-4", panel=None):
    callback, _ = manual
    return callback(slug, 0, panel or {"display": "block"}, None)


def test_recorded_games_read_exact_slug_from_active_and_archive(pg_db):
    pg_db.seed_checkin(event_slug="fightbox-4", tournament_games_registered=["Tekken 8", RAW_GG])
    pg_db.seed_checkin(event_slug="fightbox-5", tournament_games_registered=["Wrong Active Game"])
    seed_archive(
        pg_db, event_slug="fightbox-4", tournament_games_registered=[RAW_GG, "Street Fighter 6"]
    )
    seed_archive(
        pg_db, event_slug="fightbox-5", tournament_games_registered=["Wrong Archived Game"]
    )
    assert set(postgres.get_recorded_event_games("fightbox-4")) == {
        RAW_GG,
        "Tekken 8",
        "Street Fighter 6",
    }
    assert postgres.get_recorded_event_games("__ALL__") == []


def test_other_selected_event_uses_recorded_fallback_and_label_only_aliases(
    manual, dashboard, pg_db
):
    seed_archive(pg_db, event_slug="fightbox-4", tournament_games_registered=[RAW_GG])
    dashboard.storage.get_recorded_event_games = postgres.get_recorded_event_games
    choices, help_text = options(manual)
    assert choices == [{"label": CANONICAL_GG, "value": RAW_GG}]
    assert "fightbox-4" in help_text and "from recorded check-ins" in help_text
    assert "Start.gg games unavailable" in help_text
    assert "Wrong Active Game" not in str(choices)
    assert "Guilty Gear Strive" not in postgres.get_recorded_event_games("fightbox-4")


def test_saved_events_use_all_selected_event_games_instead_of_default_subset(manual, dashboard):
    dashboard.storage.get_active_settings.return_value = {
        "active_event_slug": "fightbox-4",
        "default_game": ["Street Fighter 6"],
        "events_json": {"events": [{"name": "Street Fighter 6"}, {"name": RAW_GG}]},
    }
    choices, help_text = options(manual)
    assert {choice["value"] for choice in choices} == {"Street Fighter 6", RAW_GG}
    assert "saved event data" in help_text
    manual[1].assert_not_called()


def test_provider_lookup_and_recorded_values_are_unioned_for_selected_slug(manual, dashboard):
    manual[1].return_value = (["Street Fighter 6"], "")
    choices, help_text = options(manual)
    assert {choice["value"] for choice in choices} == {"Street Fighter 6", RAW_GG}
    assert "from start.gg event fightbox-4" in help_text
    assert "from recorded check-ins" in help_text
    assert manual[1].call_args.args[0] == "fightbox-4"


def test_empty_degraded_result_explains_both_failure_and_absence(manual, dashboard):
    dashboard.storage.get_recorded_event_games.return_value = []
    choices, help_text = options(manual)
    assert choices == []
    assert "Start.gg games unavailable" in help_text and "No games are available" in help_text


def test_closed_panel_and_all_events_never_fetch(manual):
    options(manual, panel={"display": "none"})
    assert options(manual, slug="__ALL__")[0] == []
    manual[1].assert_not_called()


def test_live_filter_keeps_its_existing_active_default_game_behavior(manual, dashboard):
    result = dashboard.callbacks["fill_game_dropdown"](0, 0, "fightbox-4")
    assert result[0] == [{"label": "Wrong Active Game", "value": "Wrong Active Game"}]
    assert result[1] == "Wrong Active Game" and len(result) == 4
    manual[1].assert_not_called()


def test_manual_submit_accepts_selected_event_raw_game_and_rejects_other_event(manual, dashboard):
    dashboard.storage.begin_checkin = Mock(return_value={"record_id": "test-id", "created": True})
    submit = dashboard.callbacks["add_manual_checkin"]
    result = submit(1, "Synthetic Player", "synthetic", [RAW_GG], "fightbox-4", {})
    assert "Unknown game" not in str(result[0])
    slug, payload = dashboard.storage.begin_checkin.call_args.args
    assert slug == "fightbox-4" and payload["tournament_games_registered"] == [RAW_GG]
    dashboard.storage.begin_checkin.reset_mock()
    rejected = submit(1, "Synthetic Player", "synthetic", ["Wrong Active Game"], "fightbox-4", {})
    assert "Unknown game(s) for this event" in rejected[0].children
    dashboard.storage.begin_checkin.assert_not_called()


@pytest.mark.parametrize(
    "settings",
    [
        {"active_event_slug": "fightbox-5", "events_json": [{"name": RAW_GG}]},
        {
            "active_event_slug": "fightbox-4",
            "events_json": [{"name": RAW_GG, "slug": "fightbox-5/event/gg"}],
        },
        {"active_event_slug": "fightbox-4", "events_json": "invalid json"},
        {"active_event_slug": "fightbox-4", "events_json": [{"name": None}]},
        {
            "active_event_slug": "fightbox-4",
            "startgg_event_url": "https://start.gg/tournament/fightbox-5",
            "events_json": [{"name": RAW_GG}],
        },
    ],
)
def test_saved_metadata_must_be_valid_and_match_selected_slug(settings):
    assert event_games.games_from_settings(settings, "fightbox-4") is None


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (500, {"data": {"tournament": {"events": [{"name": RAW_GG}]}}}),
        (200, {"errors": [], "data": {"tournament": {"events": []}}}),
        (200, {"data": {"tournament": None}}),
        (200, {"data": {"tournament": {"events": None}}}),
        (200, {"data": {"tournament": {"events": [{"name": RAW_GG}, {}]}}}),
        (200, {"request_result": "fail", "data": {"tournament": {"events": []}}}),
    ],
)
def test_provider_errors_and_malformed_metadata_are_explicitly_unavailable(
    monkeypatch, status, payload
):
    monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(event_games, "_CACHE", {})
    monkeypatch.setattr(
        event_games.requests,
        "post",
        Mock(return_value=SimpleNamespace(status_code=status, json=lambda: payload)),
    )
    names, unavailable = event_games.fetch_tournament_games("fightbox-4", "synthetic-key")
    assert names == [] and unavailable
    assert event_games._CACHE == {}


def test_provider_valid_empty_is_available_and_requests_the_selected_slug(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(event_games, "_CACHE", {})
    post = Mock(
        return_value=SimpleNamespace(
            status_code=200, json=lambda: {"data": {"tournament": {"events": []}}}
        )
    )
    monkeypatch.setattr(event_games.requests, "post", post)
    assert event_games.fetch_tournament_games("fightbox-4", "synthetic-key") == ([], "")
    assert post.call_args.kwargs["json"]["variables"] == {"slug": "fightbox-4"}
    assert event_games.fetch_tournament_games("fightbox-4", "synthetic-key") == ([], "")
    assert post.call_count == 1


def test_disabled_lookup_never_calls_provider(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENGINE", "none")
    post = Mock()
    monkeypatch.setattr(event_games.requests, "post", post)
    assert event_games.fetch_tournament_games("fightbox-4", "synthetic-key")[1]
    post.assert_not_called()


def test_transport_failure_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setenv("INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(event_games, "_CACHE", {})
    post = Mock(side_effect=event_games.requests.Timeout("synthetic timeout"))
    monkeypatch.setattr(event_games.requests, "post", post)
    names, unavailable = event_games.fetch_tournament_games("fightbox-4", "synthetic-key")
    assert names == [] and unavailable
    assert event_games._CACHE == {}

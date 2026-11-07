"""T-066b: one canonical choice with deterministic raw values and legacy validation."""

from unittest.mock import Mock

import pytest
from psycopg.types.json import Jsonb

from shared import postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_manual_games_options import manual, options  # noqa: F401
from tests.test_safe_archiving import seed_archive

VARIANTS = ["SF6", "STREET FIGHTER 6 TOURNAMENT", "Street Fighter 6"]
CANONICAL = "Street Fighter 6"
DOUBLES = "SF6 Doubles"


@pytest.fixture
def canonical_games(manual, dashboard):
    store = dashboard.storage
    store.get_active_settings.return_value = {
        "active_event_slug": "fightbox-4",
        "default_game": ["Tekken 8"],
        "require_payment": True,
        "swish_expected_per_game": 25,
    }
    store.get_recorded_event_game_counts = Mock(
        return_value={
            VARIANTS[0]: 7,
            VARIANTS[1]: 3,
            VARIANTS[2]: 5,
            DOUBLES: 2,
            "Tekken 8": 1,
        }
    )
    store.get_game_aliases.return_value = {
        **{name.lower(): CANONICAL for name in VARIANTS},
        DOUBLES.lower(): "Street Fighter 6 Doubles",
    }
    store.get_recorded_event_games.return_value = list(
        store.get_recorded_event_game_counts.return_value
    )
    return store


def sf6_choice(manual):
    choices, _ = options(manual)
    matches = [choice for choice in choices if choice["label"] == CANONICAL]
    assert len(matches) == 1
    return matches[0], choices


def test_three_sf6_variants_offer_one_most_frequent_raw_value_and_separate_doubles(
    canonical_games, manual
):
    choice, all_choices = sf6_choice(manual)
    assert choice["value"] == "SF6"
    assert len(all_choices) == 3
    assert {"label": "Street Fighter 6 Doubles", "value": DOUBLES} in all_choices
    assert {"label": "Tekken 8", "value": "Tekken 8"} in all_choices


@pytest.mark.parametrize("source", ["saved", "provider"])
def test_startgg_raw_event_name_wins_over_more_frequent_recorded_alias(
    canonical_games, manual, source
):
    raw = "STREET FIGHTER 6 TOURNAMENT"
    if source == "saved":
        canonical_games.get_active_settings.return_value["events_json"] = [{"name": raw}]
    else:
        manual[1].return_value = ([raw], "")
    choice, _ = sf6_choice(manual)
    assert choice["value"] == raw


def test_frequency_ties_choose_alphabetical_raw_value_deterministically(canonical_games, manual):
    canonical_games.get_recorded_event_game_counts.return_value = {
        name: 2 for name in reversed(VARIANTS)
    }
    assert sf6_choice(manual)[0]["value"] == "SF6"
    canonical_games.get_recorded_event_game_counts.return_value = {name: 2 for name in VARIANTS}
    assert sf6_choice(manual)[0]["value"] == "SF6"


def test_frequency_counts_are_event_specific_and_once_per_record(pg_db):
    pg_db.seed_checkin(event_slug="fightbox-4", tournament_games_registered=["SF6", "SF6"])
    pg_db.seed_checkin(event_slug="fightbox-4", tournament_games_registered=["SF6"])
    seed_archive(pg_db, event_slug="fightbox-4", tournament_games_registered=[VARIANTS[1]])
    seed_archive(pg_db, event_slug="fightbox-4", tournament_games_registered=[VARIANTS[2]])
    pg_db.seed_checkin(event_slug="fightbox-5", tournament_games_registered=[VARIANTS[1]])
    assert postgres.get_recorded_event_game_counts("fightbox-4") == {
        "SF6": 2,
        VARIANTS[1]: 1,
        VARIANTS[2]: 1,
    }
    assert set(postgres.get_recorded_event_games("fightbox-4")) == set(VARIANTS)


def test_reopen_then_late_backfill_counts_six_logical_checkins_once(pg_db, canonical_games, manual):
    pg_db.seed_settings(active_event_slug="fightbox-4")
    for raw in [VARIANTS[1]] * 3 + ["SF6"] * 2:
        pg_db.seed_checkin(event_slug="fightbox-4", tournament_games_registered=[raw])
    postgres.archive_event(
        "fightbox-4",
        event_date="2026-11-07",
        event_display_name="Synthetic",
        startgg_snapshot={},
        clear_active=True,
    )
    postgres.reopen_event("fightbox-4", restore_active=True)
    pg_db.seed_checkin(event_slug="fightbox-4", tournament_games_registered=["SF6"])
    assert postgres.get_recorded_event_game_counts("fightbox-4") == {
        VARIANTS[1]: 3,
        "SF6": 3,
    }
    canonical_games.get_recorded_event_game_counts = postgres.get_recorded_event_game_counts
    canonical_games.get_recorded_event_games = postgres.get_recorded_event_games
    assert sf6_choice(manual)[0]["value"] == "SF6"


def test_active_uuid_mirror_wins_but_unidentified_legacy_archive_is_preserved(pg_db):
    seed_archive(
        pg_db,
        event_slug="fightbox-4",
        checkin_uuid="legacy-uuid",
        tournament_games_registered=[VARIANTS[1]],
    )
    pg_db.seed_checkin(
        event_slug="fightbox-4",
        checkin_uuid="legacy-uuid",
        tournament_games_registered=["SF6"],
    )
    seed_archive(pg_db, event_slug="fightbox-4", tournament_games_registered=["Tekken 8"])
    seed_archive(
        pg_db,
        event_slug="fightbox-4",
        checkin_uuid="other-event-uuid",
        tournament_games_registered=["Doubles"],
    )
    pg_db.seed_checkin(
        event_slug="fightbox-5",
        checkin_uuid="other-event-uuid",
        tournament_games_registered=["Wrong Event"],
    )
    assert postgres.get_recorded_event_game_counts("fightbox-4") == {
        "SF6": 1,
        "Tekken 8": 1,
        "Doubles": 1,
    }
    assert VARIANTS[1] in postgres.get_recorded_event_games("fightbox-4")


def test_optional_restore_links_exclude_an_active_legacy_mirror_without_ids(pg_db):
    archive_id = seed_archive(
        pg_db, event_slug="fightbox-4", tournament_games_registered=[VARIANTS[1]]
    )
    pg_db.seed_checkin(
        event_slug="fightbox-4", record_id="mapped-active", tournament_games_registered=["SF6"]
    )
    with pg_db.pool.connection() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS event_edit_context "
            "(event_slug TEXT PRIMARY KEY, restore_links JSONB NOT NULL DEFAULT '[]')"
        )
    pg_db.insert(
        "event_edit_context",
        {
            "event_slug": "fightbox-4",
            "restore_links": Jsonb([{"archive_id": archive_id, "active_id": "mapped-active"}]),
        },
    )
    assert postgres.get_recorded_event_game_counts("fightbox-4") == {"SF6": 1}


def test_active_games_replace_archived_frequency_without_losing_legacy_variants(
    pg_db, canonical_games, manual, dashboard
):
    seed_archive(
        pg_db,
        event_slug="fightbox-4",
        source_record_id="active-mirror",
        tournament_games_registered=[VARIANTS[1]],
    )
    pg_db.seed_checkin(
        event_slug="fightbox-4", record_id="active-mirror", tournament_games_registered=["SF6"]
    )
    canonical_games.get_recorded_event_game_counts = postgres.get_recorded_event_game_counts
    canonical_games.get_recorded_event_games = postgres.get_recorded_event_games
    assert postgres.get_recorded_event_game_counts("fightbox-4") == {"SF6": 1}
    assert sf6_choice(manual)[0]["value"] == "SF6"
    canonical_games.begin_checkin = Mock(return_value={"record_id": "new-id", "created": True})
    dashboard.callbacks["add_manual_checkin"](
        1, "Synthetic", "test", [VARIANTS[1]], "fightbox-4", {}
    )
    assert canonical_games.begin_checkin.call_args.args[1]["tournament_games_registered"] == [
        VARIANTS[1]
    ]


@pytest.mark.parametrize("raw", VARIANTS)
def test_initial_submission_still_accepts_each_known_raw_variant(
    canonical_games, manual, dashboard, raw
):
    canonical_games.begin_checkin = Mock(return_value={"record_id": "new-id", "created": True})
    dashboard.callbacks["add_manual_checkin"](1, "Synthetic", "test", [raw], "fightbox-4", {})
    assert canonical_games.begin_checkin.call_args.args[1]["tournament_games_registered"] == [raw]


@pytest.mark.parametrize("raw", VARIANTS)
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_confirmation_keeps_accepting_raw_variants_hidden_by_display_dedupe(
    canonical_games, manual, dashboard, raw, answer
):
    canonical_games.begin_checkin = Mock(return_value={"record_id": "paid-id", "created": False})
    canonical_games.get_checkin_by_record_id = Mock(
        return_value={
            "record_id": "paid-id",
            "fields": {
                "event_slug": "fightbox-4",
                "tournament_games_registered": ["Tekken 8"],
                "payment_valid": True,
                "payment_amount": 25,
            },
        }
    )
    selected = ["Tekken 8", raw]
    pending = dashboard.callbacks["add_manual_checkin"](
        1, "Synthetic", "test", selected, "fightbox-4", {}
    )[4]
    assert pending["games"] == selected
    dashboard.callbacks[f"games_paid_difference_{answer}"](1, pending)
    expected = {"tournament_games_registered": selected, "payment_expected": 50}
    expected.update({"payment_amount": 50} if answer == "yes" else {"payment_valid": False})
    canonical_games.update_checkin.assert_called_once_with("paid-id", expected)

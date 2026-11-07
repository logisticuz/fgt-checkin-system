"""Canonical game reads preserve historical facts and keep doubles separate."""

import json
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from scripts import migration_preflight
from shared import migrate, postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard
from tests.test_safe_archiving import archive_db, seed_archive, seed_player

SF6 = "Street Fighter 6"
GG = "Guilty Gear -Strive-"
SMASH = "Super Smash Bros. Ultimate"
DOUBLES = SMASH + " Doubles"
MIGRATION = Path(__file__).resolve().parents[1] / "shared/migrations/009_game_aliases.sql"
pytestmark = pytest.mark.db


def facts(database):
    with database.pool.connection() as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            for table, order in (
                ("players", "id"),
                ("event_archive", "id"),
                ("event_stats", "event_slug"),
            )
        }


@pytest.fixture
def history(archive_db):
    for index in range(1, 7):
        seed_player(
            archive_db,
            f"test-p{index}",
            uuid=f"p{index}",
            game_counts=Jsonb({"SF6": 2, "STREET FIGHTER 6 TOURNAMENT": 3}),
            favorite_game="SF6",
            games_played=["SF6", "STREET FIGHTER 6 TOURNAMENT"],
        )
    rows = [
        ("event-a", "p1", ["  sF6  ", "STREET FIGHTER 6 TOURNAMENT", SF6]),
        ("event-a", "p2", ["GUILTY GEAR STRIVE", "GUILTY GEAR STRIVE TOURNAMENT"]),
        ("event-a", "p3", [GG, SF6]),
        ("event-a", "p4", ["Doubles", "SSBU"]),
        ("event-a", "p5", [GG]),
        ("event-b", "p1", ["SF6", "T8"]),
        ("event-b", "p6", ["SMASH SINGLES"]),
    ]
    for slug, player, games in rows:
        seed_archive(
            archive_db,
            event_slug=slug,
            event_date="2026-01-01" if slug == "event-a" else "2026-02-01",
            player_uuid=player,
            name=player,
            tag=player,
            tournament_games_registered=games,
        )
    for slug in ("event-a", "event-b"):
        event_rows = [row for row in rows if row[0] == slug]
        archive_db.insert(
            "event_stats",
            {
                "event_slug": slug,
                "event_date": "2026-01-01" if slug == "event-a" else "2026-02-01",
                "event_display_name": slug,
                "total_participants": len(event_rows),
                "games_breakdown": dict(
                    Counter(game for _, _, games in event_rows for game in games)
                ),
                "most_popular_game": "SF6",
                "startgg_snapshot": {
                    "events": [
                        {"name": "SF6 TOURNAMENT", "numEntrants": 0},
                        {
                            "name": "STREET FIGHTER 6 TOURNAMENT",
                            "numEntrants": 2,
                            "setCount": 2,
                            "gamesPlayed": 4,
                        },
                    ]
                },
            },
        )
    return archive_db


def test_history_recomputes_canonical_breakdown_and_top_game_without_writes(history):
    before = facts(history)
    result = {row["event_slug"]: row for row in postgres.get_event_history_dashboard()}
    assert result["event-a"]["games_breakdown"] == {SF6: 2, GG: 3, SMASH: 1, DOUBLES: 1}
    assert result["event-a"]["most_popular_game"] == GG
    assert "GUILTY GEAR STRIVE TOURNAMENT" in result["event-a"]["game_aliases"][GG]
    assert facts(history) == before


def test_canonical_counts_and_crossovers_deduplicate_aliases_but_not_doubles(history):
    before = facts(history)
    assert postgres.get_multi_game_count(event_slugs=["event-a"]) == {
        "multi_game_count": 2,
        "total_count": 5,
    }
    assert postgres.get_multi_game_count(start_date="2026-02-01", end_date="2026-02-01") == {
        "multi_game_count": 1,
        "total_count": 2,
    }
    assert postgres.get_multi_game_count() == {"multi_game_count": 3, "total_count": 7}
    stats = postgres.get_event_game_stats(event_slugs=["event-a"])["event-a"]
    assert stats["total_game_slots"] == 7
    crossover = postgres.get_game_crossover_stats()
    assert {
        frozenset((row["game_a"], row["game_b"])): row["shared_players"] for row in crossover
    } == {
        frozenset((SF6, GG)): 1,
        frozenset((SF6, "TEKKEN 8")): 1,
        frozenset((SMASH, DOUBLES)): 1,
    }
    assert all(row["game_a"] != row["game_b"] for row in crossover)
    assert facts(history) == before


def test_attendance_game_filter_uses_aliases_and_keeps_doubles_separate(history):
    for name in ("sf6", "SF6 TOURNAMENT", "Street Fighter 6"):
        result = postgres.get_top_players_history(game_filter=name)
        assert [(row["name"], row["events_attended"]) for row in result] == [("p1", 2), ("p3", 1)]
    assert len(postgres.get_top_players_history(game_filter="Doubles")) == 1
    assert len(postgres.get_top_players_history(game_filter=SMASH)) == 2


def insights(dashboard):
    values = dashboard.callbacks["update_insights"](
        "tab-insights",
        1,
        ["event-a"],
        "all",
        [],
        15,
        None,
        None,
        None,
        None,
    )
    outputs = dashboard.definitions["update_insights"]["output"]
    return {
        (output.component_id, output.component_property): value
        for output, value in zip(outputs, values)
    }


def wire_reads(dashboard, monkeypatch):
    for name in (
        "get_game_aliases",
        "get_event_history_dashboard",
        "get_multi_game_count",
        "get_game_crossover_stats",
        "get_top_players_history",
    ):
        monkeypatch.setattr(dashboard.storage, name, getattr(postgres, name), raising=False)


def test_games_overview_and_alias_notes_use_canonical_names(history, dashboard, monkeypatch):
    before = facts(history)
    wire_reads(dashboard, monkeypatch)
    output = insights(dashboard)
    rows = {row["game"]: row for row in output[("insights-games-table", "data")]}
    assert set(rows) == {SF6, GG, SMASH, DOUBLES}
    assert rows[SF6]["entries"] == 2 and rows[SF6]["registered"] == 2
    assert rows[GG]["entries"] == 3
    assert "includes aliases:" in rows[SF6]["game_label"]
    assert "SF6 TOURNAMENT" in rows[SF6]["game_label"]
    assert all(
        isinstance(value, (str, int, float, bool))
        for row in rows.values()
        for value in row.values()
    )
    assert output[("insights-kpi-avggames", "children")] == "1.4"
    assert output[("insights-kpi-multigame", "children")] == "40.0%"
    assert output[("insights-top-game", "children")] == "Game highlight: " + GG
    columns = dashboard.callbacks["show_insights_game_aliases"](
        list(rows.values()),
        [{"name": "Game", "id": "game"}, {"name": "Check-ins", "id": "entries"}],
    )
    assert columns == [
        {"name": "Game", "id": "game_label", "presentation": "markdown"},
        {"name": "Check-ins", "id": "entries"},
    ]
    assert facts(history) == before


def test_alias_note_markdown_treats_unknown_labels_as_text(history, dashboard, monkeypatch):
    wire_reads(dashboard, monkeypatch)
    unsafe = "[bracket](https://example.invalid) <img> *game*"
    original = {
        "event_slug": "event-a",
        "total_participants": 1,
        "games_breakdown": {unsafe: 1},
        "event_date": "2026-01-01",
    }
    monkeypatch.setattr(dashboard.storage, "get_event_history_dashboard", lambda: [original])
    row = insights(dashboard)[("insights-games-table", "data")][0]
    assert row["game"] == unsafe
    assert "\\[" in row["game_label"] and "\\<" in row["game_label"] and "\\*" in row["game_label"]
    assert original["games_breakdown"] == {unsafe: 1}


def test_legacy_snapshot_only_history_merges_aliases_and_recomputes_winner(archive_db):
    archive_db.insert(
        "event_stats",
        {
            "event_slug": "legacy",
            "total_participants": 8,
            "games_breakdown": {
                "GUILTY GEAR STRIVE": 3,
                "GUILTY GEAR STRIVE TOURNAMENT": 3,
                "SF6": 4,
            },
            "most_popular_game": "SF6",
        },
    )
    row = postgres.get_event_history_dashboard()[0]
    assert row["games_breakdown"] == {GG: 6, SF6: 4}
    assert row["most_popular_game"] == GG


def test_alias_migration_is_idempotent_and_preflight_passes_without_fact_rewrites(
    pg_db, monkeypatch, capsys
):
    migrate.apply(pg_db.pool)
    with pg_db.pool.connection() as connection:
        connection.execute("DELETE FROM fgt_schema_migrations WHERE version = '009'")
        connection.execute("DELETE FROM game_aliases")
    seed_player(pg_db, "test-unchanged", game_counts=Jsonb({"SF6": 3}), favorite_game="SF6")
    seed_archive(pg_db, tournament_games_registered=["SF6", "Doubles"])
    before = facts(pg_db)
    source = MIGRATION.read_text(encoding="utf-8")
    migrate.lint(source, MIGRATION.name)
    with pg_db.pool.connection() as connection:
        connection.execute(source, prepare=False)
        first = connection.execute(
            "SELECT alias, canonical FROM game_aliases ORDER BY alias"
        ).fetchall()
        assert len(first) == 17
        connection.execute(source, prepare=False)
        assert (
            connection.execute(
                "SELECT alias, canonical FROM game_aliases ORDER BY alias"
            ).fetchall()
            == first
        )
    aliases = postgres.get_game_aliases()
    assert len(aliases) == 17
    assert aliases["guilty gear strive tournament"] == GG
    assert aliases["sf6 tournament"] == SF6
    assert aliases["doubles"] == DOUBLES and aliases["ssbu"] == SMASH
    assert migrate.apply(pg_db.pool) == [MIGRATION.name]
    assert migrate.apply(pg_db.pool) == []
    assert facts(pg_db) == before
    with pg_db.pool.connection() as connection:
        monkeypatch.setattr(
            migration_preflight.psycopg, "connect", lambda *args, **kwargs: nullcontext(connection)
        )
        assert migration_preflight.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready_to_deploy"] is True and report["pending_migrations"] == []

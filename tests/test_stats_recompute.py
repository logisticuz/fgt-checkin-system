"""T-015: archive facts, SELECT-only previews and atomic repairs in private real PG schemas."""

import json
from collections import Counter
from contextlib import nullcontext
from datetime import date
from decimal import Decimal

import pytest
from psycopg import sql

from scripts import recompute_stats as cli
from shared import migrate, postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard  # noqa: F401


def snapshot(db):
    with db.pool.connection() as conn:
        return {
            table: conn.execute(
                sql.SQL(
                    "SELECT count(*), md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY id)::text, '[]')) FROM {} t"
                ).format(sql.Identifier(table))
            ).fetchone()
            for table in ("event_archive", "event_stats", "players", "audit_log", "settings")
        }


@pytest.fixture
def history(pg_db):
    migrate.apply(pg_db.pool)
    for player in "abcd":
        pg_db.insert(
            "players",
            {
                "uuid": player,
                "name": "Synthetic Private " + player,
                "tag": "private-tag-" + player,
                "email": player + "@example.invalid",
                "total_events": 99,
                "total_paid": 999,
            },
        )
    for slug, day, stamp, members, snapshot_data in (
        ("first", "2026-01-01", "2026-11-01T00:00:00Z", ["a"], {"tournament_entrants_players": 2}),
        (
            "same-day",
            "2026-01-01",
            "2026-11-02T00:00:00Z",
            ["a", "b"],
            {"tournament_entrants": 100},
        ),
        (
            "later",
            "2026-01-02",
            "2026-10-01T00:00:00Z",
            ["a", "c"],
            {"tournament_entrants_players": 2},
        ),
        ("unknown", None, "2026-09-01T00:00:00Z", ["b", "d"], {}),
    ):
        pg_db.insert(
            "event_stats",
            {
                "event_slug": slug,
                "event_date": day,
                "archived_at": stamp,
                "new_players": 0,
                "returning_players": len(members),
                "retention_rate": 100,
                "startgg_snapshot": snapshot_data,
            },
        )
        for player in members:
            pg_db.insert(
                "event_archive",
                {
                    "event_slug": slug,
                    "event_date": day,
                    "archived_at": stamp,
                    "player_uuid": player,
                    "name": "Synthetic Private " + player,
                    "tag": "private-tag-" + player,
                    "payment_valid": player in {"a", "c"},
                    "payment_amount": (
                        10
                        if (slug, player) == ("same-day", "a")
                        else 35 if (slug, player) == ("later", "a") else 0
                    ),
                    "payment_expected": 25,
                    "tournament_games_registered": ["Test Game", "Test Second Game"],
                    "member": False,
                    "startgg": True,
                    "status": "Ready",
                },
            )
    return pg_db


def preview(db, **kwargs):
    with db.pool.connection() as conn:
        return cli.repair(_conn=conn, **kwargs)


def test_four_event_dates_cohorts_ties_unknown_and_retention(history):
    result = preview(history)
    events = {item["event_slug"]: item for item in result["events"]}
    assert events["first"]["after"]["new_players"] == 1
    assert events["same-day"]["after"]["new_players"] == 1
    assert events["later"]["after"]["new_players"] == 1
    assert (
        events["unknown"]["after"]["new_players"]
        == events["unknown"]["after"]["returning_players"]
        == 0
    )
    assert (
        max(
            Counter(
                player for event in events.values() for player in event["new_player_uuids"]
            ).values()
        )
        == 1
    )
    assert events["first"]["after"]["retention_rate"] == 0
    assert events["same-day"]["after"]["retention_rate"] == 50
    assert events["later"]["after"]["retention_rate"] == 50
    assert events["unknown"]["after"]["retention_rate"] is None
    assert events["first"]["after"]["no_show_count"] == 1
    assert events["same-day"]["after"]["no_show_count"] is None
    assert all(item["after"]["member_percentage"] is None for item in events.values())


def test_equal_date_and_archive_timestamp_uses_slug(history):
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_stats SET archived_at = '2026-11-01' WHERE event_slug = 'same-day'"
        )
    assert postgres.recompute_event_stats("first", dry_run=True)["new_player_uuids"] == ["a"]
    assert postgres.recompute_event_stats("same-day", dry_run=True)["new_player_uuids"] == ["b"]


def test_dry_run_is_database_read_only_and_checksums_unchanged(history, monkeypatch):
    before = snapshot(history)
    original = postgres._stats_context

    def asserted_read_only(conn, **kwargs):
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        return original(conn, **kwargs)

    monkeypatch.setattr(postgres, "_stats_context", asserted_read_only)
    monkeypatch.setattr(
        postgres, "_get_pool", lambda: pytest.fail("CLI must not invoke startup migrations")
    )
    result = preview(history, free_events=["first"], event_prices={"later": (Decimal(25), True)})
    assert result["applied"] is False
    assert snapshot(history) == before
    assert (
        next(item for item in result["events"] if item["event_slug"] == "later")["after"][
            "total_revenue"
        ]
        == 85
    )


def test_apply_is_idempotent_and_writes_audit(history):
    first = preview(history, apply=True)
    assert first["players"]
    with history.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_log WHERE action='stats_repair_applied'"
            ).fetchone()[0]
            == 1
        )
    second = preview(history)
    assert not second["players"]
    assert all(not item["changes"] for item in second["events"])
    before = snapshot(history)
    preview(history, apply=True)
    after = snapshot(history)
    for table in ("event_archive", "event_stats", "players", "settings"):
        assert before[table] == after[table]
    assert after["audit_log"][0] == before["audit_log"][0] + 1


def test_player_totals_use_all_archive_facts_and_known_dates(history):
    players = {item["player_uuid"]: item["after"] for item in preview(history)["players"]}
    assert players["a"]["total_events"] == 3
    assert players["a"]["total_paid"] == 45
    assert players["a"]["first_seen"].date() == date(2026, 1, 1)
    assert players["d"]["total_events"] == 1
    assert players["d"]["first_seen"] is None
    assert players["d"]["first_event"] is None


def test_deviations_are_not_invented_or_auto_corrected(history):
    history.insert(
        "players", {"uuid": "duplicate", "name": "synthetic private A", "tag": "PRIVATE-TAG-A"}
    )
    history.insert(
        "event_archive", {"event_slug": "later", "event_date": "2026-01-02", "player_uuid": None}
    )
    history.insert(
        "event_archive", {"event_slug": "later", "event_date": "2026-01-02", "player_uuid": "a"}
    )
    with history.pool.connection() as conn:
        facts_before = conn.execute(
            "SELECT to_jsonb(a) FROM event_archive a ORDER BY id"
        ).fetchall()
    result = preview(history, apply=True)
    kinds = {item["kind"] for item in result["deviations"]}
    assert {
        "missing_player_uuid",
        "duplicate_event_attendance",
        "suspected_duplicate",
        "paid_below_expected",
        "payment_valid_without_amount",
        "new_returning_participant_mismatch",
        "player_total_events_mismatch",
    } <= kinds
    with history.pool.connection() as conn:
        assert (
            conn.execute("SELECT to_jsonb(a) FROM event_archive a ORDER BY id").fetchall()
            == facts_before
        )
        assert conn.execute("SELECT count(*) FROM players").fetchone()[0] == 5


def test_verified_free_changes_only_expected_and_payment_requirement(history):
    result = preview(history, apply=True, free_events=["same-day"])
    with history.pool.connection() as conn:
        assert conn.execute(
            "SELECT sum(payment_amount), max(payment_expected) FROM event_archive WHERE event_slug='same-day'"
        ).fetchone() == (10, 0)
        snapshot_data, member_pct = conn.execute(
            "SELECT requirements_snapshot, member_percentage FROM event_stats WHERE event_slug='same-day'"
        ).fetchone()
    assert snapshot_data == {"require_payment": False}
    assert member_pct is None
    assert result["corrections"]


def test_confirmed_price_fills_paid_zero_only_and_preserves_nonzero(history):
    preview(
        history,
        apply=True,
        event_prices={"later": (Decimal(25), True), "same-day": (Decimal(25), False)},
    )
    with history.pool.connection() as conn:
        amounts = dict(
            conn.execute(
                "SELECT player_uuid,payment_amount FROM event_archive WHERE event_slug='later'"
            )
        )
        assert amounts == {"a": Decimal(35), "c": Decimal(50)}
        assert (
            conn.execute(
                "SELECT payment_amount FROM event_archive WHERE event_slug='same-day' AND player_uuid='b'"
            ).fetchone()[0]
            == 0
        )
    result = preview(history)
    assert any(
        item["kind"] == "paid_below_expected" and item["event_slug"] == "later"
        for item in result["deviations"]
    )


def test_price_comparison_is_dry_only_and_keeps_unknown_game_counts(history):
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET tournament_games_registered=NULL WHERE event_slug='later' AND player_uuid='c'"
        )
    before = snapshot(history)
    result = preview(history, compare_prices={"later": (Decimal(25), False)})
    assert snapshot(history) == before
    person, game = result["comparisons"]
    assert person["revenue"] == "60.00" and person["amounts_filled"] == 1
    assert game["revenue"] == "35.00" and game["unknown_game_count"] == 1
    with pytest.raises(ValueError, match="dry-run only"):
        preview(history, apply=True, compare_prices={"later": (Decimal(25), False)})


def test_apply_and_audit_are_one_transaction(history, monkeypatch):
    before = snapshot(history)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(postgres, "_stats_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        preview(
            history, apply=True, free_events=["first"], event_prices={"later": (Decimal(25), False)}
        )
    assert snapshot(history) == before


def test_unknown_or_conflicting_dates_do_not_guess(history):
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET event_date='2026-01-03' WHERE event_slug='later' AND player_uuid='a'"
        )
    result = postgres.recompute_event_stats("later", dry_run=True)
    assert result["after"]["event_date"] is None
    assert result["new_players"] == result["returning_players"] == 0
    assert "unknown_or_conflicting_event_date" in result["integrity_warnings"]


def test_archive_twice_preserves_stats_and_requirement_snapshot(pg_db):
    pg_db.seed_settings(
        active_event_slug="repeat",
        require_payment=False,
        require_membership=True,
        require_startgg=False,
    )
    pg_db.seed_checkin(event_slug="repeat")
    parameters = dict(
        event_slug="repeat",
        event_date="2026-01-01",
        event_display_name="Synthetic Repeat",
        swish_expected_per_game=25,
        startgg_snapshot={},
    )
    postgres.archive_event(**parameters)
    with pg_db.pool.connection() as conn:
        before = conn.execute(
            "SELECT to_jsonb(s) FROM event_stats s WHERE event_slug='repeat'"
        ).fetchone()[0]
        conn.execute("UPDATE settings SET require_membership=False, require_payment=True")
    postgres.archive_event(**parameters)
    with pg_db.pool.connection() as conn:
        after = conn.execute(
            "SELECT to_jsonb(s) FROM event_stats s WHERE event_slug='repeat'"
        ).fetchone()[0]
    assert before == after
    assert after["new_players"] == 1
    assert after["retention_rate"] == 0
    assert after["requirements_snapshot"] == {
        "require_payment": False,
        "require_membership": True,
        "require_startgg": False,
    }


def test_readonly_dashboard_and_funnel_share_canonical_cohorts(history):
    before = snapshot(history)
    events = {item["event_slug"]: item for item in postgres.get_event_history_dashboard()}
    assert events["first"]["new_players"] == 1
    assert events["first"]["retention_rate"] == 0
    funnel = postgres.get_player_funnel_stats()
    assert funnel["new_count"] == 3
    assert funnel["returning_count"] == 1
    one = postgres.get_player_funnel_stats(event_slugs=["same-day"])
    assert one["new_count"] == one["returning_count"] == 1
    assert snapshot(history) == before


def test_counts_export_contains_no_player_or_contact_data(history, capsys):
    result = preview(history)
    cli.print_result(result)
    output = capsys.readouterr().out
    assert (
        "Synthetic Private" not in output
        and "private-tag" not in output
        and "@example.invalid" not in output
    )
    exported = cli.counts_summary(result)
    assert "player_uuid" not in json.dumps(exported, default=str)
    assert exported["players_changed"] == 4
    cli.print_result(result, player_details=True)
    assert "First seen" in capsys.readouterr().out


@pytest.mark.parametrize(
    "values",
    [
        ["x=0"],
        ["x=-1"],
        ["x=NaN"],
        ["x=Infinity"],
        ["x=25.001"],
        ["x=1e999"],
        ["x=25", "x=25"],
        ["=25"],
        ["x=25/person"],
    ],
)
def test_invalid_prices_are_rejected(values):
    with pytest.raises(ValueError):
        cli.parse_prices(values)


def test_price_parser_requires_explicit_basis():
    assert cli.parse_prices(["x=25/game", "y=25"]) == {
        "x": (Decimal(25), True),
        "y": (Decimal(25), False),
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"event": "missing"},
        {"free_events": ["missing"]},
        {"free_events": ["first"], "event_prices": {"first": (Decimal(25), False)}},
        {"event": "first", "free_events": ["later"]},
    ],
)
def test_invalid_scope_does_not_write(history, arguments):
    before = snapshot(history)
    with pytest.raises(ValueError):
        preview(history, apply=True, **arguments)
    assert snapshot(history) == before


def test_cli_default_has_no_pool_startup_and_prints_counts_only(history, monkeypatch, capsys):
    with history.pool.connection() as conn:
        monkeypatch.setattr(cli.psycopg, "connect", lambda *args, **kwargs: nullcontext(conn))
        monkeypatch.setattr(postgres, "_get_pool", lambda: pytest.fail("No startup DDL"))
        assert cli.main(["--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False and len(result["events"]) == 4


def test_pre010_dry_run_works_and_apply_refuses(history):
    with history.pool.connection() as conn:
        # Warm a named SELECT-* plan before changing schema, as a long-lived pool would.
        for _ in range(8):
            conn.execute("SELECT * FROM event_stats ORDER BY event_slug").fetchall()
        conn.execute(
            "ALTER TABLE event_stats DROP COLUMN requirements_snapshot, DROP COLUMN same_series_return_rate, DROP COLUMN same_series_base_count, DROP COLUMN same_series_returned_players"
        )
        try:
            assert len(cli.repair(_conn=conn)["events"]) == 4
            with pytest.raises(ValueError, match="migration 010"):
                cli.repair(_conn=conn, apply=True)
        finally:
            conn.execute((cli.ROOT / "shared/migrations/010_stats_requirements.sql").read_text())


@pytest.mark.parametrize(
    "required,expected", [(None, "– (unknown)"), (False, "– (not required)"), (True, "0%")]
)
def test_dashboard_first_event_displays_availability(dashboard, monkeypatch, required, expected):
    from unittest.mock import Mock

    event = {
        "event_slug": "fightbox",
        "event_date": "2026-01-01",
        "archived_at": "2026-01-02",
        "total_participants": 3,
        "new_players": 3,
        "returning_players": 0,
        "retention_rate": None,
        "member_count": 0,
        "no_show_count": None,
        "no_show_rate": None,
        "requirements_snapshot": {} if required is None else {"require_membership": required},
    }
    monkeypatch.setattr(
        dashboard.storage, "get_event_history_dashboard", Mock(return_value=[event]), raising=False
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], "all", [], 10, "", "", None, None
    )
    outputs = dashboard.definitions["update_insights"]["output"]
    values = {item.component_id: value for item, value in zip(outputs, result)}
    assert values["insights-kpi-retention"] == "–"
    assert values["insights-kpi-memberrate"] == expected
    assert values["insights-kpi-noshow"] == "–"
    row = values["insights-events-table"][0]
    assert row["retention_rate"] == "–" and row["member_rate"] == expected
    assert row["event_quality"] == "–"


def test_dashboard_scan_accepts_two_sections_without_identity_exposure(dashboard, monkeypatch):
    from unittest.mock import Mock

    scan = {
        "data_errors": [{"kind": "event_stats_mismatch", "event_slug": "synthetic"}],
        "deviations": [{"kind": "suspected_duplicate", "value": "PRIVATE-IDENTITY"}],
    }
    monkeypatch.setattr(
        dashboard.storage, "scan_event_stats_integrity", Mock(return_value=scan), raising=False
    )
    _, rows = dashboard.callbacks["scan_archived_event_integrity"](1, {})
    assert len(rows) == 2
    assert "PRIVATE-IDENTITY" not in json.dumps(rows)


@pytest.mark.parametrize("required,disabled", [(["first"], []), ([], ["first"])])
def test_explicit_membership_policy_previews_without_writes_then_applies(
    history, required, disabled
):
    before = snapshot(history)
    result = preview(history, required_membership=required, no_membership=disabled)
    first = next(item for item in result["events"] if item["event_slug"] == "first")
    assert first["after"]["requirements_snapshot"] == {"require_membership": bool(required)}
    assert first["after"]["member_percentage"] == (Decimal(0) if required else None)
    assert snapshot(history) == before
    preview(history, apply=True, required_membership=required, no_membership=disabled)
    with history.pool.connection() as conn:
        policy = conn.execute(
            "SELECT requirements_snapshot FROM event_stats WHERE event_slug='first'"
        ).fetchone()[0]
        assert policy == {"require_membership": bool(required)}
        assert (
            conn.execute(
                "SELECT requirements_snapshot FROM event_stats WHERE event_slug='later'"
            ).fetchone()[0]
            is None
        )
        audit = json.loads(
            conn.execute(
                "SELECT details FROM audit_log WHERE action='stats_repair_applied'"
            ).fetchone()[0]
        )
        assert audit["verified_membership_requirements"] == {"first": bool(required)}


def test_membership_conflicts_or_unknown_scope_refuse_without_writes(history):
    before = snapshot(history)
    for arguments in (
        {"required_membership": ["first"], "no_membership": ["first"]},
        {"required_membership": ["missing"]},
        {"event": "first", "no_membership": ["later"]},
    ):
        with pytest.raises(ValueError):
            preview(history, apply=True, **arguments)
    assert snapshot(history) == before


def test_cli_membership_flags_are_explicit_and_counts_only(history, monkeypatch, capsys):
    with history.pool.connection() as conn:
        monkeypatch.setattr(cli.psycopg, "connect", lambda *args, **kwargs: nullcontext(conn))
        assert (
            cli.main(
                ["--required-membership", "first,later", "--no-membership", "same-day", "--json"]
            )
            == 0
        )
    result = json.loads(capsys.readouterr().out)
    rows = {item["event_slug"]: item for item in result["events"]}
    assert rows["first"]["after"]["member_percentage"] == "0.00"
    assert rows["same-day"]["after"]["member_percentage"] is None
    assert result["deviations"]["membership_requirement_unknown"] == 1


def test_dashboard_mixed_known_and_unknown_registration_counts_stay_unknown(dashboard, monkeypatch):
    from unittest.mock import Mock

    base = {
        "event_date": "2026-01-01",
        "archived_at": "2026-01-02",
        "total_participants": 20,
        "requirements_snapshot": {"require_membership": False},
    }
    rows = [
        {
            **base,
            "event_slug": "fightbox",
            "startgg_registered_players": 30,
            "no_show_count": 10,
            "no_show_rate": 100 / 3,
        },
        {**base, "event_slug": "fightbox-2", "no_show_count": None},
    ]
    monkeypatch.setattr(
        dashboard.storage, "get_event_history_dashboard", Mock(return_value=rows), raising=False
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], "all", [], 10, "", "", None, None
    )
    outputs = dashboard.definitions["update_insights"]["output"]
    values = {item.component_id: value for item, value in zip(outputs, result)}
    assert values["insights-kpi-noshow"] == "–"
    assert values["insights-empty-hint"] == ""


def test_duplicate_checkin_counts_one_visit_and_sums_all_recorded_payments(history):
    history.insert(
        "event_archive",
        {
            "event_slug": "later",
            "event_date": "2026-01-02",
            "player_uuid": "a",
            "payment_amount": 7,
            "payment_valid": True,
            "member": True,
            "status": "Pending",
            "tournament_games_registered": ["Test Game"],
        },
    )
    before = snapshot(history)
    result = preview(history, required_membership=["later"])
    event = next(item for item in result["events"] if item["event_slug"] == "later")
    assert event["archive_rows"] == 3 and event["after"]["total_participants"] == 2
    assert event["new_players"] + event["returning_players"] == 2
    assert event["after"]["total_revenue"] == 42
    assert event["after"]["avg_payment"] == 21
    assert event["after"]["member_percentage"] == 50
    assert event["after"]["status_breakdown"] == {"Ready": 2}
    assert event["after"]["no_show_count"] == 0
    player = next(item for item in result["players"] if item["player_uuid"] == "a")
    assert player["after"]["total_events"] == 3 and player["after"]["total_paid"] == 52
    assert snapshot(history) == before
    preview(history, apply=True, required_membership=["later"])
    with history.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*),sum(payment_amount) FROM event_archive WHERE event_slug='later'"
        ).fetchone() == (3, 42)
    assert not preview(history)["players"]


def test_duplicate_paid_attendance_does_not_create_a_second_payment(history):
    history.insert(
        "event_archive",
        {
            "event_slug": "later",
            "event_date": "2026-01-02",
            "player_uuid": "a",
            "payment_amount": 0,
            "payment_valid": True,
            "payment_expected": 50,
            "tournament_games_registered": ["Test Game", "Test Second Game"],
        },
    )
    result = preview(history, apply=True, event_prices={"later": (Decimal(25), True)})
    assert any(item["kind"] == "duplicate_payment_requires_review" for item in result["deviations"])
    with history.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT sum(payment_amount) FROM event_archive WHERE event_slug='later' AND player_uuid='a'"
            ).fetchone()[0]
            == 35
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM event_archive WHERE event_slug='later' AND player_uuid='a' AND payment_amount=0"
            ).fetchone()[0]
            == 1
        )


def test_archive_copy_count_preserves_rows_while_stats_count_unique_visits(pg_db):
    pg_db.seed_settings(active_event_slug="duplicate-archive")
    pg_db.seed_checkin(
        event_slug="duplicate-archive", tag="Synthetic first spelling", email="same@example.invalid"
    )
    pg_db.seed_checkin(
        event_slug="duplicate-archive",
        tag="Synthetic second spelling",
        email="same@example.invalid",
    )
    result = postgres.archive_event(event_slug="duplicate-archive", event_date="2026-01-01")
    assert result["archived"] == 2
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_archive WHERE event_slug='duplicate-archive'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute(
            "SELECT total_participants,new_players,returning_players FROM event_stats WHERE event_slug='duplicate-archive'"
        ).fetchone() == (1, 1, 0)
        assert conn.execute("SELECT total_events FROM players").fetchone()[0] == 1


def test_global_new_and_same_series_return_are_distinct(history):
    with history.pool.connection() as conn:
        for old, new in (
            ("first", "fightbox"),
            ("same-day", "fight-night-1"),
            ("later", "fightbox-2"),
        ):
            conn.execute("UPDATE event_archive SET event_slug=%s WHERE event_slug=%s", (new, old))
            conn.execute("UPDATE event_stats SET event_slug=%s WHERE event_slug=%s", (new, old))
    result = {item["event_slug"]: item for item in preview(history)["events"]}
    assert result["fightbox"]["after"]["retention_rate"] == 0
    assert result["fightbox"]["after"]["same_series_return_rate"] is None
    assert result["fight-night-1"]["after"]["same_series_return_rate"] is None
    assert result["fightbox-2"]["after"]["retention_rate"] == 50
    assert result["fightbox-2"]["after"]["same_series_return_rate"] == 100
    assert result["unknown"]["after"]["same_series_return_rate"] is None
    # Switching series does not make an existing player new again.
    scoped = postgres.get_player_funnel_stats(event_slugs=["fight-night-1"])
    assert scoped["new_count"] == scoped["returning_count"] == 1
    assert result["fight-night-1"]["new_player_uuids"] == ["b"]


def test_explicit_missing_membership_data_is_not_zero_percent(history):
    before = snapshot(history)
    result = preview(history, required_membership=["first"], member_not_recorded=["first"])
    first = next(item for item in result["events"] if item["event_slug"] == "first")
    assert first["after"]["member_percentage"] is None
    assert first["after"]["requirements_snapshot"] == {
        "require_membership": True,
        "member_data_recorded": False,
    }
    kinds = {item["kind"] for item in result["deviations"] if item.get("event_slug") == "first"}
    assert {"member_data_not_recorded", "required_membership_zero_members"} <= kinds
    assert snapshot(history) == before
    preview(history, apply=True, required_membership=["first"], member_not_recorded=["first"])
    assert postgres.get_event_history_dashboard()[-1]["event_slug"]


def test_required_zero_members_is_always_a_deviation(history):
    result = preview(history, required_membership=["first"])
    assert any(item["kind"] == "required_membership_zero_members" for item in result["deviations"])
    with pytest.raises(ValueError):
        preview(history, apply=True, member_not_recorded=["missing"])


def test_reconcile_all_profiles_and_explain_profiles_without_first_attendance(history):
    history.insert("players", {"uuid": "no-archive", "total_paid": 0})
    result = preview(history)
    summary = cli.counts_summary(result)["reconciliation"]
    assert summary["profiles"] == 5
    assert summary["never_new_profiles"] == {
        "count": 2,
        "reasons": {"zero_archive_rows": 1, "undated_only": 1},
    }
    assert (
        summary["players_total_paid_after"]
        == summary["archive_payments_after"]
        == summary["event_revenue_after"]
        == 45
    )
    assert summary["per_player_payment_mismatches"] == 4
    assert summary["balanced"] is True
    assert (
        sum(item["kind"] == "player_payment_aggregate_mismatch" for item in result["deviations"])
        == 4
    )


@pytest.mark.parametrize("player", [None, "orphan-profile"])
def test_apply_refuses_unassigned_money_instead_of_losing_it(history, player):
    history.insert(
        "event_archive",
        {
            "event_slug": "later",
            "event_date": "2026-01-02",
            "player_uuid": player,
            "payment_amount": 10,
        },
    )
    before = snapshot(history)
    result = preview(history)
    assert result["reconciliation"]["unassigned_payment_rows"] == 1
    assert result["reconciliation"]["balanced"] is False
    with pytest.raises(ValueError, match="do not reconcile"):
        preview(history, apply=True)
    assert snapshot(history) == before


@pytest.mark.parametrize("kind", ["settings", "rows"])
def test_apply_refuses_any_active_event_but_dry_run_remains_readonly(history, kind):
    if kind == "settings":
        history.seed_settings(active_event_slug="active-synthetic-event")
    else:
        history.seed_checkin(event_slug="active-synthetic-event")
    before = snapshot(history)
    assert preview(history)["applied"] is False
    with pytest.raises(ValueError, match="active"):
        preview(history, apply=True)
    assert snapshot(history) == before


@pytest.mark.parametrize("resource", ["fgt:player-merges", "fgt-event:first"])
def test_apply_refuses_archive_maintenance_locks_without_waiting(history, resource):
    with history.pool.connection() as busy, busy.transaction():
        busy.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (resource,))
        with pytest.raises(ValueError, match="progress|archived"):
            preview(history, apply=True)


def test_apply_refuses_concurrent_activation_or_checkin_writer(history):
    history.seed_settings(active_event_slug=None)
    with history.pool.connection() as busy, busy.transaction():
        busy.execute("UPDATE settings SET require_membership=true")
        with pytest.raises(ValueError, match="busy"):
            preview(history, apply=True)


def test_apply_audit_captures_every_changed_field_and_can_restore_core_rows(history):
    from psycopg.types.json import Jsonb

    with history.pool.connection() as conn:
        conn.execute("DELETE FROM event_stats WHERE event_slug='first'")
    before = snapshot(history)
    preview(history, apply=True, free_events=["first"], event_prices={"later": (Decimal(25), True)})
    json_fields = {
        "games_breakdown",
        "status_breakdown",
        "requirements_snapshot",
        "events_list",
        "game_counts",
    }
    with history.pool.connection() as conn, conn.transaction():
        audit = json.loads(
            conn.execute(
                "SELECT details FROM audit_log WHERE action='stats_repair_applied'"
            ).fetchone()[0]
        )
        assert audit["repair_snapshot_version"] == 1
        assert {entry["table"] for entry in audit["row_changes"]} == {
            "event_archive",
            "event_stats",
            "players",
        }
        assert any(not entry["before_exists"] for entry in audit["row_changes"])
        for entry in reversed(audit["row_changes"]):
            table, key = entry["table"], entry["key"]
            where = sql.SQL(" AND ").join(
                sql.SQL("{}=%s").format(sql.Identifier(field)) for field in key
            )
            if not entry["before_exists"]:
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE ").format(sql.Identifier(table)) + where,
                    list(key.values()),
                )
                continue
            fields = entry["changes"]
            assignments = sql.SQL(", ").join(
                sql.SQL("{}=%s").format(sql.Identifier(field)) for field in fields
            )
            values = [
                (
                    Jsonb(change["before"])
                    if field in json_fields and change["before"] is not None
                    else change["before"]
                )
                for field, change in fields.items()
            ]
            conn.execute(
                sql.SQL("UPDATE {} SET ").format(sql.Identifier(table))
                + assignments
                + sql.SQL(" WHERE ")
                + where,
                values + list(key.values()),
            )
    after = snapshot(history)
    assert all(before[table] == after[table] for table in before if table != "audit_log")
    assert after["audit_log"][0] == before["audit_log"][0] + 1


def test_dashboard_missing_membership_data_and_series_metric_labels(dashboard, monkeypatch):
    from unittest.mock import Mock

    event = {
        "event_slug": "fightbox",
        "event_date": "2026-01-01",
        "archived_at": "2026-01-02",
        "total_participants": 3,
        "retention_rate": 0,
        "same_series_return_rate": None,
        "member_count": 0,
        "requirements_snapshot": {"require_membership": True, "member_data_recorded": False},
    }
    monkeypatch.setattr(
        dashboard.storage, "get_event_history_dashboard", Mock(return_value=[event]), raising=False
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], "all", [], 10, "", "", None, None
    )
    values = {
        item.component_id: value
        for item, value in zip(dashboard.definitions["update_insights"]["output"], result)
    }
    assert values["insights-kpi-memberrate"] == "– (not recorded)"
    row = values["insights-events-table"][0]
    assert row["member_rate"] == "– (not recorded)"
    assert row["retention_rate"] == "0%" and row["same_series_return_rate"] == "–"

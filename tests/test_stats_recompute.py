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
from tests import test_lock_coverage as private_locks
from tests.test_dashboard_row_identity import dashboard  # noqa: F401


class _StatsLockCursor(private_locks._Cursor):
    """Extend T058's real-lock test namespace to the CLI's nonblocking variants."""

    def __iter__(self):
        return iter(self.cursor)

    def execute(self, query, *args, **kwargs):
        if isinstance(query, str) and "pg_try_advisory_xact_lock" in query:
            query = query.replace("hashtext('fgt", "hashtext(current_schema() || ':fgt")
        return super().execute(query, *args, **kwargs)


@pytest.fixture(autouse=True)
def private_stats_lock_namespace(request, monkeypatch):
    if "pg_db" not in request.fixturenames:
        return
    database = request.getfixturevalue("pg_db")
    connections = private_locks._LockConnections(database)
    monkeypatch.setattr(private_locks, "_Cursor", _StatsLockCursor)
    monkeypatch.setattr(database, "pool", connections)
    monkeypatch.setattr(postgres, "_get_pool", lambda: connections)


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
        == 35
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


def test_confirmed_price_changes_only_expectations_and_preserves_every_amount(history):
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
        assert amounts == {"a": Decimal(35), "c": Decimal(0)}
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
    assert person["revenue"] == game["revenue"] == "35.00"
    assert person["paid_amount_not_recorded"] == game["paid_amount_not_recorded"] == 1
    assert person["expected_payment_corrections"] == 0
    assert game["expected_payment_corrections"] == 1 and game["unknown_game_count"] == 1
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
        conn.execute("UPDATE event_stats SET event_date=NULL WHERE event_slug='later'")
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
        busy.execute(
            "SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':' || %s))", (resource,)
        )
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


@pytest.mark.parametrize("recorded", [None, 0, Decimal("12.50"), Decimal("-5.00")])
def test_tariff_never_creates_or_rewrites_recorded_money(history, recorded):
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET payment_amount=%s WHERE event_slug='later' AND player_uuid='c'",
            (recorded,),
        )
        before = conn.execute(
            "SELECT id,payment_valid,payment_amount FROM event_archive ORDER BY id"
        ).fetchall()
    dry = preview(history, event_prices={"later": (Decimal(25), True)})
    assert all(set(item["after"]) == {"payment_expected"} for item in dry["corrections"])
    result = preview(history, apply=True, event_prices={"later": (Decimal(25), True)})
    with history.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT id,payment_valid,payment_amount FROM event_archive ORDER BY id"
            ).fetchall()
            == before
        )
        assert (
            conn.execute(
                "SELECT payment_expected FROM event_archive WHERE event_slug='later' AND player_uuid='c'"
            ).fetchone()[0]
            == 50
        )
        policy = conn.execute(
            "SELECT requirements_snapshot FROM event_stats WHERE event_slug='later'"
        ).fetchone()[0]
        assert policy["event_price"] == {"amount": "25", "basis": "game"}
    if recorded is None or recorded == 0:
        assert any(
            item["kind"] == "payment_valid_without_amount"
            and item["message"] == "Paid, amount not recorded"
            for item in result["deviations"]
            if item.get("event_slug") == "later"
        )


def test_verified_price_basis_survives_ordinary_recompute_and_free_assertion_clears_it(history):
    preview(history, apply=True, event_prices={"first": (Decimal("25.00"), False)})
    result = postgres.recompute_event_stats("first", dry_run=True)
    assert result["after"]["requirements_snapshot"]["event_price"] == {
        "amount": "25.00",
        "basis": "person",
    }
    preview(history, apply=True, free_events=["first"])
    with history.pool.connection() as conn:
        policy = conn.execute(
            "SELECT requirements_snapshot FROM event_stats WHERE event_slug='first'"
        ).fetchone()[0]
    assert policy == {"require_payment": False}


def test_real_insights_unknown_noshow_output_survives_trend_and_ops(dashboard, monkeypatch):
    from unittest.mock import Mock

    event = {
        "event_slug": "fightbox",
        "event_date": "2026-01-01",
        "archived_at": "2026-01-02",
        "total_participants": 3,
        "new_players": 3,
        "returning_players": 0,
        "no_show_count": None,
        "no_show_rate": None,
        "retention_rate": 0,
        "requirements_snapshot": {"require_membership": False},
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
    unknown = values["insights-events-table"][0]
    assert unknown["no_show_rate"] == unknown["no_show_count"] == "–"
    rows = [
        unknown,
        {**unknown, "event_date": "2026-01-02", "no_show_rate": 0, "no_show_count": 0},
        {**unknown, "event_date": "2026-01-03", "no_show_rate": 25, "no_show_count": 1},
    ]
    figure = dashboard.callbacks["render_events_noshow_trend"](rows)
    assert figure["data"][0]["y"] == [None, 0.0, 25.0]
    assert figure["data"][0]["connectgaps"] is False
    assert figure["data"][1]["y"] == [3, 3, 3]
    assert "n/a" in figure["data"][1]["hovertext"][0]
    ops = dashboard.callbacks["build_events_ops_table"](rows)
    assert ops[0]["no_show_rate"] == ops[0]["no_show_band"] == ops[0]["event_quality"] == "n/a"
    assert ops[0]["no_show_count"] == "n/a" and "n/a" in ops[0]["ops_flag"]
    assert ops[1]["no_show_rate"] == 0.0 and ops[1]["no_show_band"] == "Low"
    assert ops[1]["ops_flag"] == "OK"


@pytest.mark.parametrize("guest", [False, True])
def test_dashboard_projects_unique_ready_startgg_and_guest_numerators(
    pg_db, dashboard, monkeypatch, guest
):
    from unittest.mock import Mock

    pg_db.insert("players", {"uuid": "unique-player"})
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "fightbox",
            "event_date": "2026-01-01",
            "archived_at": "2026-01-02",
            "total_participants": 2,
            "member_count": 2,
            "guest_count": 0,
            "startgg_count": 2,
            "status_breakdown": {"Ready": 2},
            "games_breakdown": {"Stored game summary": 99},
            "startgg_snapshot": {"tournament_entrants_players": 1},
            "requirements_snapshot": {"require_membership": True},
        },
    )
    for _ in range(2):
        pg_db.insert(
            "event_archive",
            {
                "event_slug": "fightbox",
                "event_date": "2026-01-01",
                "archived_at": "2026-01-02",
                "player_uuid": "unique-player",
                "member": True,
                "startgg": True,
                "is_guest": guest,
                "status": "Ready",
            },
        )
    before = snapshot(pg_db)
    events = postgres.get_event_history_dashboard()
    assert events[0]["total_participants"] == events[0]["startgg_count"] == 1
    assert events[0]["guest_count"] == int(guest)
    assert events[0]["status_breakdown"] == {"Ready": 1}
    assert (
        events[0]["games_breakdown"]
        == postgres.get_event_game_stats(["fightbox"])["fightbox"]["games_breakdown"]
    )
    monkeypatch.setattr(
        dashboard.storage, "get_event_history_dashboard", Mock(return_value=events), raising=False
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], "all", [], 10, "", "", None, None
    )
    values = {
        item.component_id: value
        for item, value in zip(dashboard.definitions["update_insights"]["output"], result)
    }
    assert values["insights-kpi-readyrate"] == "100%"
    assert values["insights-kpi-startggrate"] == ("0%" if guest else "100%")
    assert values["insights-kpi-guestrate"] == ("100%" if guest else "0%")
    assert snapshot(pg_db) == before


def test_t065_aliases_drive_expected_game_count_and_derived_player_games(history):
    history.insert("game_aliases", {"alias": "synthetic alias", "canonical": "Synthetic Canonical"})
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET tournament_games_registered=ARRAY['synthetic alias','Synthetic Canonical'], payment_expected=99 WHERE event_slug='later'"
        )
    result = preview(history, event_prices={"later": (Decimal(25), True)})
    corrections = [item for item in result["corrections"] if item["event_slug"] == "later"]
    assert len(corrections) == 2
    assert all(item["after"]["payment_expected"] == 25 for item in corrections)
    player = next(item for item in result["players"] if item["player_uuid"] == "c")
    assert player["after"]["game_counts"] == {"Synthetic Canonical": 1}
    assert player["after"]["games_played"] == ["Synthetic Canonical"]
    with history.pool.connection() as conn:
        assert conn.execute(
            "SELECT tournament_games_registered FROM event_archive WHERE event_slug='later' LIMIT 1"
        ).fetchone()[0] == ['synthetic alias', 'Synthetic Canonical']


@pytest.mark.parametrize(
    "linked,game_rows,participants,slots,multi,entries,unknown",
    [
        (True, [["SF6", "GG"], ["SF6", "GG"]], 1, 2, 1, {"SF6": 1, "GG": 1}, 0),
        (False, [["SF6", "GG"], ["SF6", "GG"]], 2, 4, 2, {"SF6": 2, "GG": 2}, 0),
        (True, [["SF6"], ["GG"]], 1, 2, 1, {"SF6": 1, "GG": 1}, 0),
        (False, [["SF6"], ["GG"]], 2, 2, 0, {"SF6": 1, "GG": 1}, 0),
        (True, [["SF6", "GG"], None], 1, 2, 1, {"SF6": 1, "GG": 1}, 0),
        (False, [["SF6", "GG"], None], 2, 2, 1, {"SF6": 1, "GG": 1}, 1),
    ],
)
def test_games_use_same_attendance_population_through_real_insights(
    pg_db, dashboard, monkeypatch, linked, game_rows, participants, slots, multi, entries, unknown
):
    from unittest.mock import Mock

    for alias, canonical in (("sf6", "SF6"), ("gg", "GG")):
        pg_db.insert("game_aliases", {"alias": alias, "canonical": canonical})
    if linked:
        pg_db.insert("players", {"uuid": "one-attendee"})
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "fightbox",
            "event_date": "2026-01-01",
            "archived_at": "2026-01-02",
            "total_participants": 2,
            "status_breakdown": {"Ready": 2},
            "startgg_count": 2,
            "games_breakdown": {"SF6": 2, "GG": 2},
            "requirements_snapshot": {"require_membership": False},
        },
    )
    for games in game_rows:
        pg_db.insert(
            "event_archive",
            {
                "event_slug": "fightbox",
                "event_date": "2026-01-01",
                "archived_at": "2026-01-02",
                "player_uuid": "one-attendee" if linked else None,
                "tournament_games_registered": games,
                "status": "Ready",
                "startgg": True,
                "payment_amount": 25,
            },
        )
    before = snapshot(pg_db)
    game_stats = postgres.get_event_game_stats(["fightbox"])["fightbox"]
    assert game_stats["total_count"] == participants
    assert game_stats["total_game_slots"] == slots
    assert game_stats["multi_game_count"] == multi
    assert game_stats["games_breakdown"] == entries
    assert game_stats["unknown_game_count"] == unknown
    assert postgres.get_multi_game_count(["fightbox"]) == {
        "multi_game_count": multi,
        "total_count": participants,
    }
    event = postgres.recompute_event_stats("fightbox", dry_run=True)
    assert event["after"]["total_participants"] == participants
    assert event["after"]["games_breakdown"] == entries
    assert event["after"]["total_revenue"] == 50
    history = postgres.get_event_history_dashboard()
    assert history[0]["unknown_game_count"] == unknown
    monkeypatch.setattr(
        dashboard.storage, "get_event_history_dashboard", Mock(return_value=history), raising=False
    )
    monkeypatch.setattr(
        dashboard.storage, "get_game_aliases", postgres.get_game_aliases, raising=False
    )
    monkeypatch.setattr(
        dashboard.storage, "get_event_game_stats", postgres.get_event_game_stats, raising=False
    )
    monkeypatch.setattr(
        dashboard.storage, "get_multi_game_count", postgres.get_multi_game_count, raising=False
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], "all", [], 10, "", "", None, None
    )
    values = {
        item.component_id: value
        for item, value in zip(dashboard.definitions["update_insights"]["output"], result)
    }
    assert values["insights-kpi-total"] == str(participants)
    assert values["insights-kpi-avggames"] == f"{slots / (participants - unknown):.1f}"
    assert values["insights-kpi-multigame"] == f"{multi / (participants - unknown) * 100:.1f}%"
    assert {row["game"]: row["entries"] for row in values["insights-games-table"]} == entries
    assert all(float(row["share"].rstrip("%")) <= 100 for row in values["insights-games-table"])
    if linked:
        player = postgres.recompute_player_aggregates(dry_run=True)[0]
        assert player["after"]["game_counts"] == entries
        assert player["after"]["total_events"] == 1 and player["after"]["total_paid"] == 50
    assert snapshot(pg_db) == before


def test_same_player_games_union_per_event_and_aliases_do_not_duplicate_slots(history):
    with history.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET tournament_games_registered=ARRAY['SF6','STREET FIGHTER 6 TOURNAMENT']"
        )
    stats = postgres.get_event_game_stats()
    assert stats["first"]["games_breakdown"] == {"Street Fighter 6": 1}
    assert stats["same-day"]["games_breakdown"] == {"Street Fighter 6": 2}
    player = next(
        item
        for item in postgres.recompute_player_aggregates(dry_run=True)
        if item["player_uuid"] == "a"
    )
    assert player["after"]["game_counts"] == {"Street Fighter 6": 3}
    assert player["after"]["total_paid"] == 45


@pytest.mark.parametrize(
    "row_dates", [("2026-01-01", None), ("2026-01-01", "2026-01-02"), (None, None)]
)
@pytest.mark.parametrize("linked", [True, False])
def test_event_date_range_keeps_all_game_rows_in_actual_insights(
    pg_db, dashboard, monkeypatch, row_dates, linked
):
    pg_db.insert("game_aliases", {"alias": "sf6", "canonical": "Street Fighter 6"})
    pg_db.insert("event_stats", {"event_slug": "fightbox", "event_date": "2026-01-01"})
    if linked:
        pg_db.insert("players", {"uuid": "date-attendee"})
    for day, games in zip(row_dates, (["SF6"], ["GG"])):
        pg_db.insert(
            "event_archive",
            {
                "event_slug": "fightbox",
                "event_date": day,
                "player_uuid": "date-attendee" if linked else None,
                "tournament_games_registered": games,
                "payment_amount": 25,
            },
        )
    before = snapshot(pg_db)
    monkeypatch.setattr(
        dashboard.storage,
        "get_event_history_dashboard",
        postgres.get_event_history_dashboard,
        raising=False,
    )
    for name in ("get_game_aliases", "get_event_game_stats", "get_multi_game_count"):
        monkeypatch.setattr(dashboard.storage, name, getattr(postgres, name), raising=False)
    outputs = []
    for period in ("all", "custom"):
        result = dashboard.callbacks["update_insights"](
            "tab-insights", 1, [], period, [], 10, "", "", "2026-01-01", "2026-01-01"
        )
        values = {
            item.component_id: value
            for item, value in zip(dashboard.definitions["update_insights"]["output"], result)
        }
        assert values["insights-kpi-total"] == ("1" if linked else "2")
        assert values["insights-kpi-avggames"] == ("2.0" if linked else "1.0")
        assert values["insights-kpi-multigame"] == ("100.0%" if linked else "0.0%")
        assert {row["game"]: row["entries"] for row in values["insights-games-table"]} == {
            "Street Fighter 6": 1,
            "GG": 1,
        }
        outputs.append(values["insights-games-table"])
    assert outputs[0] == outputs[1]
    assert snapshot(pg_db) == before


@pytest.mark.parametrize("period", ["all", "custom"])
def test_unknown_date_events_have_visible_count_including_empty_range(
    pg_db, dashboard, monkeypatch, period
):
    pg_db.insert("event_stats", {"event_slug": "undated-event"})
    pg_db.insert(
        "event_archive", {"event_slug": "undated-event", "tournament_games_registered": ["SF6"]}
    )
    before = snapshot(pg_db)
    monkeypatch.setattr(
        dashboard.storage,
        "get_event_history_dashboard",
        postgres.get_event_history_dashboard,
        raising=False,
    )
    result = dashboard.callbacks["update_insights"](
        "tab-insights", 1, [], period, [], 10, "", "", "2026-01-01", "2026-01-01"
    )
    values = {
        item.component_id: value
        for item, value in zip(dashboard.definitions["update_insights"]["output"], result)
    }
    assert "1 events without a date" in values["insights-empty-hint"]
    assert (
        "included in all time" if period == "all" else "excluded from this date range"
    ) in values["insights-empty-hint"]
    assert values["insights-kpi-total"] == ("1" if period == "all" else "0")
    assert postgres.get_unique_attendee_count(start_date="2026-01-01") == 0
    assert postgres.get_event_game_stats(start_date="2026-01-01") == {}
    assert len(postgres.get_event_game_stats()) == 1
    assert snapshot(pg_db) == before


def test_conflicting_archive_dates_without_event_date_remain_unknown_in_all_scopes(pg_db):
    pg_db.insert("event_stats", {"event_slug": "ambiguous"})
    pg_db.insert("players", {"uuid": "ambiguous-player"})
    for day in ("2026-01-01", "2026-01-02"):
        pg_db.insert(
            "event_archive",
            {
                "event_slug": "ambiguous",
                "event_date": day,
                "player_uuid": "ambiguous-player",
                "tournament_games_registered": ["SF6", "GG"],
            },
        )
    before = snapshot(pg_db)
    for bounds in (
        {"start_date": "2026-01-01"},
        {"end_date": "2026-01-02"},
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
    ):
        assert postgres.get_event_game_stats(**bounds) == {}
        assert postgres.get_unique_attendee_count(**bounds) == 0
        assert postgres.get_top_players_history(**bounds) == []
    assert postgres.get_event_game_stats()["ambiguous"]["total_count"] == 1
    assert postgres.get_unique_attendee_count() == 1
    assert postgres.get_player_funnel_stats()["new_count"] == 0
    assert postgres.get_player_funnel_stats()["returning_count"] == 0
    assert postgres.recompute_event_stats("ambiguous", dry_run=True)["after"]["event_date"] is None
    assert snapshot(pg_db) == before


@pytest.mark.parametrize("metadata", ["2026-01-01", "2026-01-03", None, "absent"])
def test_all_insights_archive_date_scopes_select_complete_events(pg_db, metadata):
    for player in ("dated", "undated"):
        pg_db.insert(
            "players",
            {"uuid": player, "name": player, "total_events": 3, "last_seen": "2025-01-01"},
        )
    for index in range(3):
        slug = f"scope-{index}"
        if metadata != "absent":
            pg_db.insert("event_stats", {"event_slug": slug, "event_date": metadata})
        for player, day, games, source in (
            ("dated", "2026-01-01", ["SF6"], "public_form"),
            ("undated", None, ["SF6", "GG"], "manual_dashboard"),
        ):
            pg_db.insert(
                "event_archive",
                {
                    "event_slug": slug,
                    "event_date": day,
                    "player_uuid": player,
                    "name": player,
                    "tournament_games_registered": games,
                    "added_via": source,
                    "acquisition_source": "friend" if day is None else "venue",
                },
            )
    before = snapshot(pg_db)
    selected_day = metadata if metadata and metadata != "absent" else "2026-01-01"
    selected = dict(start_date=selected_day, end_date=selected_day)
    excluded = dict(start_date="2026-02-01", end_date="2026-02-01")
    for name in (
        "get_event_game_stats",
        "get_multi_game_count",
        "get_event_manual_add_stats",
        "get_added_via_breakdown",
        "get_acquisition_source_breakdown",
        "get_game_crossover_stats",
        "get_unique_attendee_count",
        "get_top_players_history",
    ):
        query = getattr(postgres, name)
        assert query(**selected) == query(), name
        outside = query(**excluded)
        assert not any(outside.values()) if isinstance(outside, dict) else not outside
    for name in ("get_community_health_v2_stats", "get_player_funnel_stats"):
        query = getattr(postgres, name)
        result = query(**selected, anchor_date=selected_day)
        assert result["core_players" if name.endswith("v2_stats") else "core_count"] == 2
    assert (
        postgres.get_player_churn_stats(**selected, anchor_date="2026-01-01")["scoped_players"] == 2
    )
    assert postgres.get_top_players_history(**selected, game_filter="GG")[0]["name"] == "undated"
    assert snapshot(pg_db) == before

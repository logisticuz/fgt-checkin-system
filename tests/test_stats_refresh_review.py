"""C423: regressions for all seven independently reproduced T067 integration failures."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
from psycopg.types.json import Json

from shared import postgres_api as postgres
from tests.test_auto_stats_refresh import ACTOR, event, seed_history
from tests.test_dashboard_dialogs import games, storage  # noqa: F401
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_manual_games_options import manual  # noqa: F401
from tests.test_stats_recompute import private_stats_lock_namespace  # noqa: F401


@pytest.mark.parametrize("edit_price", [False, True])
@pytest.mark.parametrize("activation", ["reopen", "settings_switch"])
def test_switching_paid_free_paid_restores_each_drafts_own_policy(pg_db, edit_price, activation):
    seed_history(pg_db)
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "free-event",
            "event_date": "2026-01-02",
            "requirements_snapshot": {"require_payment": False, "require_membership": False},
        },
    )
    pg_db.insert(
        "event_archive",
        {"event_slug": "free-event", "event_date": "2026-01-02", "player_uuid": "a"},
    )
    postgres.reopen_event("fightbox", user=ACTOR)
    if edit_price:
        settings = postgres.get_active_settings_with_id()
        postgres.update_settings(settings["record_id"], {"swish_expected_per_game": 40})
    postgres.reopen_event("free-event", user=ACTOR)
    assert postgres.get_active_settings()["require_payment"] is False
    if activation == "reopen":
        postgres.reopen_event("fightbox", user=ACTOR)
    else:
        postgres.update_settings(
            postgres.get_active_settings_with_id()["record_id"], {"active_event_slug": "fightbox"}
        )
    settings = postgres.get_active_settings()
    assert settings["require_payment"] is True
    assert settings["swish_expected_per_game"] == (40 if edit_price else 25)
    assert str(settings["event_date"]) == "2026-01-01"
    postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    policy = event(pg_db, "fightbox")["requirements_snapshot"]
    assert policy["require_payment"] is True and policy["event_price"]["basis"] == "game"
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT payment_amount,payment_expected FROM event_archive WHERE event_slug='fightbox'"
        ).fetchone() == (Decimal("25"), Decimal(40 if edit_price else 25))


@pytest.mark.parametrize("clear", [False, True])
def test_retained_archive_tracks_partial_and_final_deletion_at_every_end(pg_db, clear):
    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(
        settings["record_id"], {"active_event_slug": "fightbox-2", "event_date": "2026-01-02"}
    )
    for player, amount in (("a", 42), ("b", 25)):
        pg_db.seed_checkin(
            record_id="retained-" + player,
            event_slug="fightbox-2",
            tag="synthetic-" + player,
            email=player + "@example.invalid",
            player_uuid=player,
            payment_amount=amount,
        )
    postgres.archive_event("fightbox-2", clear_active=False, user=ACTOR)
    postgres.reopen_event("fightbox-2", user=ACTOR)
    postgres.delete_checkin("retained-a")
    postgres.archive_event("fightbox-2", clear_active=clear, user=ACTOR)
    assert event(pg_db, "fightbox-3")["new_players"] == 1
    assert event(pg_db, "fightbox-2")["total_revenue"] == 25
    if clear:
        postgres.reopen_event("fightbox-2", user=ACTOR)
    postgres.delete_checkin("retained-b")
    ended = postgres.archive_event("fightbox-2", clear_active=clear, user=ACTOR)
    assert ended["archived"] == 0 and ended["stats_stale"] is False
    assert event(pg_db, "fightbox-2")["total_participants"] == 0
    assert event(pg_db, "fightbox-2")["attendance_recorded"] is True
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_archive WHERE event_slug='fightbox-2'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("amount", [Decimal("25"), Decimal("25.50")])
def test_person_tariff_in_registered_payment_dialog_never_requests_extra_game_money(
    pg_db, dashboard, games, manual, monkeypatch, amount
):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute(
            "UPDATE event_stats SET requirements_snapshot=jsonb_set(requirements_snapshot,'{event_price,basis}','\"person\"') WHERE event_slug='fightbox'"
        )
        conn.execute(
            "UPDATE event_stats SET requirements_snapshot=jsonb_set(requirements_snapshot,'{event_price,amount}',%s::jsonb) WHERE event_slug='fightbox'",
            (Json(str(amount)),),
        )
        conn.execute(
            "UPDATE event_archive SET payment_amount=%s,payment_expected=%s WHERE event_slug='fightbox'",
            (amount, amount),
        )
    postgres.reopen_event("fightbox", user=ACTOR)
    if amount != amount.to_integral_value():
        # Saving an unrelated setting must not replace the exact historical tariff
        # with the legacy integer control's unchanged projection.
        settings = postgres.get_active_settings_with_id()
        postgres.update_settings(
            settings["record_id"],
            {
                "swish_expected_per_game": settings["fields"]["swish_expected_per_game"],
                "swish_number": "1230000000",
            },
        )
        assert (
            Decimal(postgres.get_event_payment_policy("fightbox")["event_price"]["amount"])
            == amount
        )
    dashboard.storage.get_active_settings.return_value = postgres.get_active_settings()
    dashboard.storage.get_active_settings.return_value["events_json"] = [
        {"name": name} for name in ("Street Fighter 6", "Tekken 8")
    ]
    dashboard.storage.get_checkin_by_record_id.return_value["fields"]["event_slug"] = "fightbox"
    dashboard.storage.get_checkin_by_record_id.return_value["fields"]["payment_amount"] = float(
        amount
    )
    monkeypatch.setattr(
        dashboard.storage,
        "get_event_payment_policy",
        postgres.get_event_payment_policy,
        raising=False,
    )
    result = dashboard.callbacks["add_manual_checkin"](
        1, "Player One", "p1", ["Street Fighter 6", "Tekken 8"], "fightbox", {}
    )
    from dash import no_update

    assert result[4] is no_update
    fields = dashboard.storage.update_checkin.call_args.args[1]
    assert Decimal(str(fields["payment_expected"])) == amount
    assert "payment_amount" not in fields and "payment_valid" not in fields


def test_context_failure_after_correcting_payment_commits_facts_and_stale(pg_db, monkeypatch):
    seed_history(pg_db)
    postgres.reopen_event("fightbox", user=ACTOR)
    postgres.update_checkin(
        postgres.get_checkins("fightbox")[0]["record_id"], {"payment_amount": 77}
    )

    def fail(conn, **kwargs):
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(postgres, "_stats_context", fail)
    ended = postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert ended["stats_stale"] is True
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT payment_amount FROM event_archive WHERE event_slug='fightbox'"
            ).fetchone()[0]
            == 77
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM active_event_data WHERE event_slug='fightbox'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT last_error_code FROM event_stats_refresh").fetchone()[0]
            == "DivisionByZero"
        )


def test_unrelated_summary_only_seven_players_175_money_is_never_zeroed(pg_db):
    seed_history(pg_db)
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "legacy-summary",
            "event_date": "2025-01-01",
            "total_participants": 7,
            "total_revenue": 175,
            "games_breakdown": {"Legacy Game": 7},
            "new_players": 2,
            "returning_players": 5,
        },
    )
    before = event(pg_db, "legacy-summary")
    postgres.refresh_archive_stats(user=ACTOR)
    assert event(pg_db, "legacy-summary") == before
    row = next(
        row
        for row in postgres.get_event_history_dashboard()
        if row["event_slug"] == "legacy-summary"
    )
    assert row["summary_only"] is True and row["coverage_note"] == "Summary only (no rows)"
    assert any(
        item["label"] == "Summary only (no rows)"
        for item in postgres.get_stats_attention()["issues"]
    )


def test_reversing_not_recorded_while_reopened_does_not_reapply_draft_policy(pg_db):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE event_archive SET member=false WHERE event_slug='fightbox'")
    postgres.reopen_event("fightbox", user=ACTOR)
    issue = next(
        item
        for item in postgres.get_stats_attention()["issues"]
        if item["kind"] == "required_membership_zero_members"
    )
    postgres.resolve_stats_attention(
        issue["key"], issue["fingerprint"], action="not_recorded", user=ACTOR
    )
    with pg_db.pool.connection() as conn:
        rows = conn.execute(
            "SELECT action,details FROM audit_log WHERE action IN ('membership_marked_not_recorded','draft_membership_marked_not_recorded') ORDER BY id DESC"
        ).fetchall()
        assert len(rows) == 2
        for action, raw in rows:
            details = json.loads(raw)
            if action.startswith("draft_"):
                assert details["after"]["policy"]["member_data_recorded"] is False
                conn.execute(
                    "UPDATE event_edit_context SET policy=%s WHERE event_slug='fightbox'",
                    (Json(details["before"]["policy"]),),
                )
            else:
                conn.execute(
                    "UPDATE event_stats SET requirements_snapshot=%s WHERE event_slug='fightbox'",
                    (Json(details["before"]),),
                )
        conn.execute("DELETE FROM event_stats_issue_ack")
    postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert (
        event(pg_db, "fightbox")["requirements_snapshot"].get("member_data_recorded") is not False
    )


def test_live_refresh_and_merge_defer_until_active_event_archive(pg_db, monkeypatch):
    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(settings["record_id"], {"active_event_slug": "live-event"})
    original = postgres._stats_context
    monkeypatch.setattr(
        postgres,
        "_stats_context",
        lambda *a, **k: pytest.fail("No inline stats during live check-in"),
    )
    assert (
        postgres.resolve_stats_attention(
            "deferred-owner-retry", "not-scanned", action="retry", user=ACTOR
        )["deferred"]
        is True
    )
    with ThreadPoolExecutor() as executor:
        with pg_db.pool.connection() as held:
            held.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
            work = executor.submit(postgres.refresh_archive_stats, user=ACTOR)
            try:
                assert work.result(timeout=2)["deferred"] is True
            finally:
                held.rollback()
    postgres.merge_players("a", "b", user=ACTOR)
    assert event(pg_db, "fightbox-3")["new_players"] == 1
    monkeypatch.setattr(postgres, "_stats_context", original)
    pg_db.seed_checkin(event_slug="live-event", name="Synthetic Live", tag="synthetic-live")
    postgres.archive_event("live-event", event_date="2026-01-05", clear_active=True, user=ACTOR)
    assert event(pg_db, "fightbox-3")["new_players"] == 0
    with pg_db.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM event_stats_refresh").fetchone()[0] == 0


def test_plain_attention_ack_never_blocks_public_checkin(pg_db, monkeypatch):
    seed_history(pg_db)
    pg_db.insert(
        "event_archive",
        {"event_slug": "fightbox", "event_date": "2026-01-01", "player_uuid": "b", "member": True},
    )
    issue = next(
        item
        for item in postgres.get_stats_attention()["issues"]
        if item["kind"] == "duplicate_event_attendance"
    )
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(settings["record_id"], {"active_event_slug": "live-event"})
    entered, release = Event(), Event()
    audit = postgres._stats_audit

    def paused(conn, action, *args, **kwargs):
        if action == "stats_issue_acknowledged":
            entered.set()
            assert release.wait(5)
        return audit(conn, action, *args, **kwargs)

    monkeypatch.setattr(postgres, "_stats_audit", paused)
    with ThreadPoolExecutor() as executor:
        owner = executor.submit(
            postgres.resolve_stats_attention, issue["key"], issue["fingerprint"], user=ACTOR
        )
        try:
            assert entered.wait(3)
            public = executor.submit(
                postgres.begin_checkin,
                "live-event",
                {"name": "Synthetic Public", "tag": "synthetic-public"},
            )
            assert public.result(timeout=2)["created"] is True
        finally:
            release.set()
        assert owner.result(timeout=3)["resolved"] is True


def test_live_manual_recompute_is_disabled_with_tooltip_and_server_retry_defers(
    pg_db, dashboard, monkeypatch
):
    from unittest.mock import Mock

    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(settings["record_id"], {"active_event_slug": "live-event"})
    monkeypatch.setattr(
        dashboard.storage,
        "get_stats_refresh_admission",
        postgres.get_stats_refresh_admission,
        raising=False,
    )
    assert dashboard.callbacks["update_recompute_admission"]("tab-settings") == (
        True,
        "Runs after the event is archived",
    )
    monkeypatch.setattr(
        dashboard.storage, "refresh_archive_stats", postgres.refresh_archive_stats, raising=False
    )
    monkeypatch.setattr(dashboard.storage, "recompute_event_stats", Mock(), raising=False)
    callback = dashboard.callbacks["recompute_selected_event_stats"]
    monkeypatch.setitem(callback.__globals__, "current_user", lambda: ACTOR)
    assert "Runs after the event is archived" in str(callback(1, "fightbox", {}))
    dashboard.storage.recompute_event_stats.assert_not_called()


def test_partial_legacy_mirror_deletes_last_mapped_row_but_preserves_unmapped_facts(pg_db):
    seed_history(pg_db)
    # Explicitly start an additive draft before adding rows to unknown legacy facts.
    postgres.reopen_event("fightbox", restore_active=False, user=ACTOR)
    pg_db.seed_checkin(
        record_id="mapped-new",
        event_slug="fightbox",
        tag="synthetic-a",
        email="a@example.invalid",
        payment_amount=42,
    )
    postgres.archive_event("fightbox", clear_active=False, user=ACTOR)
    postgres.delete_checkin("mapped-new")
    postgres.archive_event("fightbox", clear_active=False, user=ACTOR)
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*),sum(payment_amount) FROM event_archive WHERE event_slug='fightbox'"
        ).fetchone() == (1, Decimal("25"))


def test_payment_correction_toggle_uses_person_price_for_two_games(dashboard, games, monkeypatch):
    from unittest.mock import Mock

    fields = dashboard.storage.get_checkin_by_record_id.return_value["fields"]
    fields.update(
        payment_valid=False,
        payment_amount=0,
        tournament_games_registered=["Street Fighter 6", "Tekken 8"],
    )
    monkeypatch.setattr(
        dashboard.storage,
        "get_event_payment_policy",
        Mock(
            return_value={
                "historical": True,
                "require_payment": True,
                "event_price": {"amount": "25", "basis": "person"},
            }
        ),
        raising=False,
    )
    row = {"id": "rec-1", "record_id": "rec-1", "name": "Synthetic Player", "payment_valid": "✗"}
    dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": "rec-1", "column_id": "payment_valid"}, [row], "fightbox-4", {}
    )
    saved = dashboard.storage.update_checkin.call_args.args[1]
    assert saved["payment_amount"] == saved["payment_expected"] == 25


def test_opening_event_cannot_race_past_an_idle_refresh_admission(pg_db, monkeypatch):
    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    entered, release = Event(), Event()
    context = postgres._stats_context

    def paused(conn, **kwargs):
        entered.set()
        assert release.wait(5)
        return context(conn, **kwargs)

    monkeypatch.setattr(postgres, "_stats_context", paused)
    with ThreadPoolExecutor() as executor:
        refresh = executor.submit(postgres.refresh_archive_stats, user=ACTOR)
        activation = None
        try:
            assert entered.wait(3)
            activation = executor.submit(
                postgres.update_settings,
                settings["record_id"],
                {"active_event_slug": "new-live-event"},
            )
            deadline = time.monotonic() + 2
            with pg_db.pool.connection() as probe:
                while not probe.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND mode='ShareLock' AND NOT granted "
                    "AND objid=((hashtext(current_schema() || ':fgt:player-merges')::bigint & 4294967295)::oid))"
                ).fetchone()[0]:
                    assert (
                        not activation.done()
                    ), "Event opened while refresh still held its global lock"
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            assert postgres.get_active_settings()["active_event_slug"] is None
        finally:
            release.set()
        assert refresh.result(timeout=3)["stale"] is False
        assert activation.result(timeout=3)["fields"]["active_event_slug"] == "new-live-event"
    assert (
        postgres.begin_checkin(
            "new-live-event", {"name": "Synthetic Public", "tag": "synthetic-new"}
        )["created"]
        is True
    )

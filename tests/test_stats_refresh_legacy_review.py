"""C441: pre-migration mirrors, authoritative summaries and activation-first admission."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest

from shared import migrate, postgres_api as postgres
from tests.test_auto_stats_refresh import ACTOR, event, seed_history
from tests.test_stats_recompute import private_stats_lock_namespace, snapshot  # noqa: F401


def legacy_retained(db):
    seed_history(db)
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(
        settings["record_id"], {"active_event_slug": "fightbox-2", "event_date": "2026-01-02"}
    )
    for player, amount in (("a", 42), ("b", 25)):
        db.seed_checkin(
            record_id="legacy-" + player,
            event_slug="fightbox-2",
            player_uuid=player,
            tag="synthetic-" + player,
            email=player + "@example.invalid",
            payment_amount=amount,
        )
    postgres.archive_event("fightbox-2", clear_active=False, user=ACTOR)
    with db.pool.connection() as conn:
        conn.execute("DELETE FROM event_edit_context WHERE event_slug='fightbox-2'")
        conn.execute(
            "UPDATE event_stats SET attendance_recorded=false WHERE event_slug='fightbox-2'"
        )


@pytest.mark.parametrize("recover", ["reopen", "end"])
def test_pre_migration_retained_mirror_recovers_verified_links_then_tracks_deletions(
    pg_db, recover
):
    legacy_retained(pg_db)
    if recover == "reopen":
        postgres.reopen_event("fightbox-2", user=ACTOR)
    else:
        postgres.archive_event("fightbox-2", clear_active=False, user=ACTOR)
    postgres.delete_checkin("legacy-a")
    postgres.archive_event("fightbox-2", clear_active=False, user=ACTOR)
    assert event(pg_db, "fightbox-2")["total_revenue"] == 25
    assert event(pg_db, "fightbox-3")["new_players"] == 1
    postgres.delete_checkin("legacy-b")
    postgres.archive_event("fightbox-2", clear_active=True, user=ACTOR)
    assert event(pg_db, "fightbox-2")["total_participants"] == 0
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_archive WHERE event_slug='fightbox-2'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("action", ["end", "reopen"])
def test_ambiguous_pre_migration_state_refuses_without_changing_any_facts(pg_db, action):
    legacy_retained(pg_db)
    postgres.delete_checkin("legacy-a")
    before = snapshot(pg_db)
    with pytest.raises(postgres.ArchiveError, match="reopened before the update.*re-open"):
        if action == "end":
            postgres.archive_event("fightbox-2", clear_active=True, user=ACTOR)
        else:
            postgres.reopen_event("fightbox-2", user=ACTOR)
    assert snapshot(pg_db) == before


def test_null_source_legacy_copies_cannot_be_matched_by_identity_guessing(pg_db):
    legacy_retained(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE event_archive SET source_record_id=NULL WHERE event_slug='fightbox-2'")
    before = snapshot(pg_db)
    with pytest.raises(postgres.ArchiveError, match="reopened before the update"):
        postgres.archive_event("fightbox-2", clear_active=True, user=ACTOR)
    assert snapshot(pg_db) == before


def test_recovered_legacy_mirror_keeps_genuinely_unmapped_null_source_facts(pg_db):
    legacy_retained(pg_db)
    pg_db.insert("event_archive", {"event_slug": "fightbox-2", "payment_amount": 11})
    postgres.reopen_event("fightbox-2", user=ACTOR)
    postgres.delete_checkin("legacy-a")
    postgres.archive_event("fightbox-2", clear_active=True, user=ACTOR)
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*),sum(payment_amount) FROM event_archive WHERE event_slug='fightbox-2'"
        ).fetchone() == (2, Decimal("36"))


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("added", [0, 1, 2])
def test_summary_only_roundtrip_keeps_authoritative_summary_even_when_rows_are_added(
    pg_db, clear, added
):
    seed_history(pg_db)
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "legacy-summary",
            "event_date": "2025-01-01",
            "total_participants": 7,
            "total_revenue": 175,
            "avg_payment": 25,
            "games_breakdown": {"Legacy Game": 7},
            "most_popular_game": "Legacy Game",
            "new_players": 2,
            "returning_players": 5,
        },
    )
    before = event(pg_db, "legacy-summary")
    postgres.reopen_event("legacy-summary", user=ACTOR)
    for index in range(added):
        pg_db.seed_checkin(
            record_id=f"summary-{index}",
            event_slug="legacy-summary",
            tag=f"synthetic-summary-{index}",
            payment_amount=77,
        )
    postgres.archive_event("legacy-summary", clear_active=clear, user=ACTOR)
    after = event(pg_db, "legacy-summary")
    assert after["attendance_recorded"] is False
    assert {key: after[key] for key in before if key != "summary_only"} == {
        key: value for key, value in before.items() if key != "summary_only"
    }
    if not added:
        assert after == before
    else:
        assert after["summary_only"] is True
        label = f"summary-only event got {added} rows: summary kept"
        assert any(item["label"] == label for item in postgres.get_stats_attention()["issues"])
        row = next(
            item
            for item in postgres.get_event_history_dashboard()
            if item["event_slug"] == "legacy-summary"
        )
        assert row["summary_only"] is True and row["coverage_note"] == label
        assert (row["total_participants"], row["total_revenue"], row["games_breakdown"]) == (
            7,
            175,
            {"Legacy Game": 7},
        )
        with pg_db.pool.connection() as conn:
            preview = postgres.recompute_event_stats("legacy-summary", dry_run=True, _conn=conn)
        assert preview["changes"] == {} and preview["after"]["total_revenue"] == 175
    if clear:
        postgres.refresh_archive_stats(user=ACTOR)
        assert event(pg_db, "legacy-summary") == after
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*),coalesce(sum(payment_amount),0) FROM event_archive WHERE event_slug='legacy-summary'"
        ).fetchone() == (added, Decimal(77 * added))
    if added:
        # A later complete removal is still a partial-ledger correction, never permission
        # to replace the authoritative summary with zero.
        postgres.reopen_event("legacy-summary", user=ACTOR)
        for record in postgres.get_checkins("legacy-summary"):
            postgres.delete_checkin(record["record_id"])
        postgres.archive_event("legacy-summary", clear_active=True, user=ACTOR)
        postgres.refresh_archive_stats(user=ACTOR)
        assert event(pg_db, "legacy-summary") == after


def test_activation_first_holds_admission_lock_from_before_update_until_commit(pg_db, monkeypatch):
    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    activation_paused, resume_activation = Event(), Event()
    refresh_entered, resume_refresh = Event(), Event()
    original_context = postgres._stats_context

    def before(query):
        if query.startswith("UPDATE settings SET active_event_slug ="):
            activation_paused.set()
            assert resume_activation.wait(5)

    def paused_refresh(conn, **kwargs):
        refresh_entered.set()
        assert resume_refresh.wait(5)
        return original_context(conn, **kwargs)

    pg_db.pool.before = before
    monkeypatch.setattr(postgres, "_stats_context", paused_refresh)
    with ThreadPoolExecutor() as workers:
        activation = workers.submit(
            postgres.update_settings, settings["record_id"], {"active_event_slug": "new-live-event"}
        )
        try:
            assert activation_paused.wait(3)
            refresh = workers.submit(postgres.refresh_archive_stats, user=ACTOR)
            deferred = refresh.result(timeout=3)
            assert deferred["deferred"] is True and deferred["error_code"] == "statistics_busy"
            assert not refresh_entered.is_set()
        finally:
            resume_activation.set()
            resume_refresh.set()
        assert activation.result(timeout=3)["fields"]["active_event_slug"] == "new-live-event"
    assert (
        postgres.begin_checkin(
            "new-live-event", {"name": "Synthetic Public", "tag": "synthetic-new"}, public=True
        )["created"]
        is True
    )


def test_repeating_011_keeps_summary_authority_and_acknowledgement(pg_db):
    seed_history(pg_db)
    pg_db.insert(
        "event_stats",
        {
            "event_slug": "summary-repeat",
            "summary_only": True,
            "total_participants": 7,
            "total_revenue": 175,
        },
    )
    pg_db.insert(
        "event_stats_issue_ack",
        {
            "issue_key": "synthetic-key",
            "fingerprint": "synthetic-facts",
            "acknowledged_by": ACTOR["user_id"],
            "resolution": "acknowledge",
        },
    )
    before = snapshot(pg_db)
    source = (migrate.MIGRATIONS_DIR / "011_stats_refresh_and_attention.sql").read_text(
        encoding="utf-8"
    )
    with pg_db.pool.connection() as conn:
        acknowledgement = conn.execute(
            "SELECT to_jsonb(a) FROM event_stats_issue_ack a"
        ).fetchone()[0]
        conn.execute(source)
        conn.execute(source)
        assert (
            conn.execute("SELECT to_jsonb(a) FROM event_stats_issue_ack a").fetchone()[0]
            == acknowledgement
        )
    assert snapshot(pg_db) == before

"""T067: real transactions, reopened drafts, deterministic refresh and degraded recovery."""

import json
from decimal import Decimal

import pytest

from shared import postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_stats_recompute import private_stats_lock_namespace  # noqa: F401

ACTOR = {"user_id": "synthetic-owner", "user_name": "Synthetic Owner", "is_dev": True}


def seed_history(db):
    db.seed_settings(active_event_slug=None, require_payment=False, require_membership=False)
    for player in ("a", "b"):
        db.insert(
            "players",
            {
                "uuid": player,
                "name": "Synthetic " + player,
                "tag": "synthetic-" + player,
                "email": player + "@example.invalid",
            },
        )
    for slug, day, player in (("fightbox", "2026-01-01", "b"), ("fightbox-3", "2026-01-03", "a")):
        db.insert(
            "event_stats",
            {
                "event_slug": slug,
                "event_date": day,
                "requirements_snapshot": {
                    "require_payment": True,
                    "require_membership": True,
                    "require_startgg": False,
                    "event_price": {"amount": "25", "basis": "game"},
                },
            },
        )
        db.insert(
            "event_archive",
            {
                "event_slug": slug,
                "event_date": day,
                "player_uuid": player,
                "name": "Synthetic " + player,
                "tag": "synthetic-" + player,
                "email": player + "@example.invalid",
                "payment_amount": 25,
                "payment_expected": 25,
                "payment_valid": True,
                "member": True,
                "tournament_games_registered": ["Synthetic Game"],
            },
        )
    assert postgres.refresh_archive_stats(user=ACTOR)["stale"] is False


def event(db, slug):
    with db.pool.connection() as conn:
        return conn.execute(
            "SELECT to_jsonb(s) FROM event_stats s WHERE event_slug=%s", (slug,)
        ).fetchone()[0]


def test_reopen_correct_rearchive_updates_later_new_then_removing_backfill_restores_new(pg_db):
    seed_history(pg_db)
    before_stats = event(pg_db, "fightbox")
    with pg_db.pool.connection() as conn:
        before_facts = postgres._archive_fact_rows(conn, "fightbox")
    result = postgres.reopen_event("fightbox", user=ACTOR)
    assert result["restored_rows"] == 1
    assert event(pg_db, "fightbox") == before_stats
    with pg_db.pool.connection() as conn:
        assert postgres._archive_fact_rows(conn, "fightbox") == before_facts
    settings = postgres.get_active_settings()
    assert settings["require_payment"] is True
    assert settings["require_membership"] is True
    assert settings["swish_expected_per_game"] == 25
    pg_db.seed_checkin(
        record_id="backfill-a",
        event_slug="fightbox",
        tag="synthetic-a",
        name="Synthetic a",
        email="a@example.invalid",
        player_uuid="a",
        payment_amount=42,
        payment_valid=True,
        member=True,
        tournament_games_registered=["Synthetic Game", "Second Game"],
    )
    # Active corrections are drafts until the operator explicitly re-archives.
    assert event(pg_db, "fightbox-3")["new_players"] == 1
    result = postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert result["stats_stale"] is False
    assert result["archived"] == 2
    assert event(pg_db, "fightbox-3")["new_players"] == 0
    assert event(pg_db, "fightbox-3")["returning_players"] == 1
    assert (
        event(pg_db, "fightbox")["requirements_snapshot"] == before_stats["requirements_snapshot"]
    )
    with pg_db.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, player_uuid, payment_amount, payment_expected FROM event_archive "
            "WHERE event_slug='fightbox' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert (
            rows[0][0] == before_facts[0]["id"]
        )  # Legacy NULL-source restore keeps the original fact ID.
        assert next(row[2:] for row in rows if row[1] == "a") == (Decimal("42"), Decimal("50"))
    postgres.reopen_event("fightbox", user=ACTOR)
    assert postgres.delete_checkin("backfill-a") is True
    assert event(pg_db, "fightbox-3")["new_players"] == 0
    postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert event(pg_db, "fightbox-3")["new_players"] == 1
    assert event(pg_db, "fightbox")["total_participants"] == 1
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT total_events, total_paid FROM players WHERE uuid='a'"
        ).fetchone() == (1, Decimal("25"))
        audits = conn.execute(
            "SELECT details FROM audit_log WHERE action='archive_facts_committed'"
        ).fetchall()
        assert any(
            any(
                row.get("source_record_id") == "backfill-a" for row in json.loads(item[0])["before"]
            )
            for item in audits
        )


@pytest.mark.parametrize("fail_audit", [False, True])
def test_refresh_failure_rolls_back_derived_savepoint_but_commits_facts_and_stale_marker(
    pg_db, monkeypatch, caplog, fail_audit
):
    seed_history(pg_db)
    postgres.reopen_event("fightbox", user=ACTOR)
    pg_db.seed_checkin(
        record_id="recover-a",
        event_slug="fightbox",
        tag="synthetic-a",
        email="a@example.invalid",
        name="Synthetic a",
        player_uuid="a",
    )
    original = postgres.recompute_player_aggregates
    original_audit = postgres._stats_audit
    if fail_audit:

        def fail(conn, action, *args, **kwargs):
            if action in {"automatic_stats_recomputed", "stats_refresh_degraded"}:
                raise RuntimeError("private@example.invalid must not enter logs")
            return original_audit(conn, action, *args, **kwargs)

        monkeypatch.setattr(postgres, "_stats_audit", fail)
    else:

        def fail(*args, **kwargs):
            kwargs["_conn"].execute("SELECT 1 / 0")

        monkeypatch.setattr(postgres, "recompute_player_aggregates", fail)
    before = event(pg_db, "fightbox-3")
    result = postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert result["stats_stale"] is True
    assert event(pg_db, "fightbox-3") == before
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM event_archive WHERE event_slug='fightbox'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM active_event_data").fetchone()[0] == 0
        assert conn.execute("SELECT last_error_code FROM event_stats_refresh").fetchone()[0] in {
            "DivisionByZero",
            "RuntimeError",
        }
    assert "private@example.invalid" not in caplog.text
    monkeypatch.setattr(postgres, "recompute_player_aggregates", original)
    monkeypatch.setattr(postgres, "_stats_audit", original_audit)
    assert postgres.refresh_archive_stats(user=ACTOR)["stale"] is False
    assert event(pg_db, "fightbox-3")["new_players"] == 0
    with pg_db.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM event_stats_refresh").fetchone()[0] == 0


@pytest.mark.parametrize("degrade_first", [False, True])
def test_global_merge_refresh_and_undo_survive_later_stats_only_healing(
    pg_db, monkeypatch, degrade_first
):
    seed_history(pg_db)
    baseline = {slug: event(pg_db, slug) for slug in ("fightbox", "fightbox-3")}
    original = postgres.recompute_player_aggregates
    if degrade_first:

        def fail(*args, **kwargs):
            raise RuntimeError("Synthetic refresh failure")

        monkeypatch.setattr(postgres, "recompute_player_aggregates", fail)
    merged = postgres.merge_players("a", "b", user=ACTOR)
    monkeypatch.setattr(postgres, "recompute_player_aggregates", original)
    assert postgres.refresh_archive_stats(user=ACTOR)["stale"] is False
    assert event(pg_db, "fightbox")["new_players"] == 1
    assert event(pg_db, "fightbox-3")["new_players"] == 0
    assert postgres.undo_merge(merged["merge_id"], user=ACTOR)["undone"] is True
    assert {slug: event(pg_db, slug) for slug in baseline} == baseline


def test_stats_healing_never_resets_undo_guard_after_human_identity_change(pg_db):
    seed_history(pg_db)
    merged = postgres.merge_players("a", "b", user=ACTOR)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE players SET name='Changed by human', total_events=999 WHERE uuid='a'")
    assert postgres.refresh_archive_stats(user=ACTOR)["stale"] is False
    with pytest.raises(ValueError, match="Players changed"):
        postgres.undo_merge(merged["merge_id"], user=ACTOR)


def test_attention_ack_is_audited_persists_across_technical_rearchive_but_changes_reopen_it(pg_db):
    seed_history(pg_db)
    pg_db.insert(
        "event_archive",
        {
            "event_slug": "fightbox",
            "event_date": "2026-01-01",
            "player_uuid": "b",
            "member": True,
            "tournament_games_registered": ["Synthetic Game"],
        },
    )
    issue = next(
        item
        for item in postgres.get_stats_attention()["issues"]
        if item["kind"] == "duplicate_event_attendance"
    )
    with pytest.raises(PermissionError):
        postgres.resolve_stats_attention(issue["key"], issue["fingerprint"], user={"user_id": "to"})
    assert postgres.resolve_stats_attention(issue["key"], issue["fingerprint"], user=ACTOR)[
        "resolved"
    ]
    assert not any(
        item["kind"] == "duplicate_event_attendance"
        for item in postgres.get_stats_attention()["issues"]
    )
    with pg_db.pool.connection() as conn:
        conn.execute(
            "UPDATE event_archive SET archived_at=now(), source_record_id='technical-' || id WHERE event_slug='fightbox'"
        )
        assert (
            conn.execute("SELECT acknowledged_by FROM event_stats_issue_ack").fetchone()[0]
            == ACTOR["user_id"]
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_log WHERE action='stats_issue_acknowledged'"
            ).fetchone()[0]
            == 1
        )
    assert not any(
        item["kind"] == "duplicate_event_attendance"
        for item in postgres.get_stats_attention()["issues"]
    )
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE event_archive SET payment_amount=13 WHERE event_slug='fightbox'")
    changed = next(
        item for item in postgres.get_stats_attention()["issues"] if item["key"] == issue["key"]
    )
    assert changed["fingerprint"] != issue["fingerprint"]
    with pytest.raises(ValueError, match="changed"):
        postgres.resolve_stats_attention(issue["key"], issue["fingerprint"], user=ACTOR)


def test_attention_not_recorded_changes_only_policy_and_hides_acknowledged_membership_items(pg_db):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE event_archive SET member=false WHERE event_slug='fightbox'")
    issue = next(
        item
        for item in postgres.get_stats_attention()["issues"]
        if item["kind"] == "required_membership_zero_members"
    )
    postgres.resolve_stats_attention(
        issue["key"], issue["fingerprint"], action="not_recorded", user=ACTOR
    )
    assert event(pg_db, "fightbox")["requirements_snapshot"]["member_data_recorded"] is False
    assert event(pg_db, "fightbox")["member_percentage"] is None
    assert not any(
        item["event_slug"] == "fightbox" for item in postgres.get_stats_attention()["issues"]
    )
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT member,payment_amount FROM event_archive WHERE event_slug='fightbox'"
        ).fetchone() == (False, Decimal("25"))
    # The core integrity scan continues to show the deviation, independently of the box acknowledgement.
    assert any(
        item["kind"] == "required_membership_zero_members"
        for item in postgres.scan_event_stats_integrity()["deviations"]
    )


def test_attention_stale_visible_even_when_scan_itself_fails_and_retry_heals(pg_db, monkeypatch):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute(
            "INSERT INTO event_stats_refresh(event_slug,reason,last_error_code) VALUES ('fightbox','archive','RuntimeError')"
        )
    original = postgres._stats_context
    monkeypatch.setattr(
        postgres,
        "_stats_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Synthetic scan failure")),
    )
    issue = postgres.get_stats_attention()["issues"][0]
    assert issue["kind"] == "stats_stale"
    monkeypatch.setattr(postgres, "_stats_context", original)
    issue = postgres.get_stats_attention()["issues"][0]
    assert (
        postgres.resolve_stats_attention(
            issue["key"], issue["fingerprint"], action="retry", user=ACTOR
        )["stale"]
        is False
    )
    assert not any(
        item["kind"] == "stats_stale" for item in postgres.get_stats_attention()["issues"]
    )


def test_not_same_updates_global_queue_without_recompute(pg_db, monkeypatch):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE players SET name='Same synthetic name'")
    assert any(
        item["kind"] == "suspected_duplicate" for item in postgres.get_stats_attention()["issues"]
    )
    monkeypatch.setattr(
        postgres,
        "_refresh_stats_locked",
        lambda *a, **k: pytest.fail("Dismissal must not recompute"),
    )
    assert postgres.dismiss_duplicate_pair("a", "b", dismissed_by="synthetic-owner")
    assert not any(
        item["kind"] == "suspected_duplicate" for item in postgres.get_stats_attention()["issues"]
    )


def test_repeated_refresh_writes_only_real_derived_differences(pg_db):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert postgres.refresh_archive_stats(user=ACTOR) == {
        "stale": False,
        "events_changed": 0,
        "players_changed": 0,
    }
    with pg_db.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before


def test_attention_callbacks_use_server_owner_and_refresh_existing_insights(dashboard, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    handler = dashboard.callbacks["resolve_stats_attention"]
    current_user = Mock(return_value={"user_id": "synthetic-to", "is_dev": False})
    monkeypatch.setitem(handler.__globals__, "current_user", current_user)
    write = Mock(return_value={"resolved": True})
    monkeypatch.setattr(dashboard.storage, "resolve_stats_attention", write, raising=False)
    forged = {"user_id": "synthetic-owner", "is_dev": True}
    assert "Only the owner" in handler([1], forged)[0]
    write.assert_not_called()
    current_user.return_value = ACTOR
    item = {
        "type": "stats-attention-action",
        "key": "opaque-key",
        "fingerprint": "opaque-fingerprint",
        "action": "acknowledge",
    }
    monkeypatch.setitem(handler.__globals__, "ctx", SimpleNamespace(triggered_id=item))
    result = handler([1], {"user_id": "forged-store"}, 7)
    assert result[0] == "Saved."
    assert result[2] == 8
    write.assert_called_once_with(
        "opaque-key", "opaque-fingerprint", action="acknowledge", user=ACTOR
    )
    assert dashboard.callbacks["open_attention_duplicates"]([1]) == "duplicates"


def test_attention_box_is_compact_readable_by_to_with_owner_actions_only(dashboard, monkeypatch):
    from unittest.mock import Mock

    handler = dashboard.callbacks["load_stats_attention"]
    actor = Mock(return_value={"user_id": "synthetic-to", "is_dev": False})
    monkeypatch.setitem(handler.__globals__, "current_user", actor)
    queue = {
        "count": 8,
        "issues": [
            {
                "key": str(index),
                "fingerprint": "opaque",
                "label": "Review item",
                "event_slug": "synthetic-event",
                "action": "duplicates" if index == 0 else "acknowledge",
            }
            for index in range(8)
        ],
    }
    monkeypatch.setattr(dashboard.storage, "get_stats_attention", lambda: queue, raising=False)
    title, content = handler("tab-insights", 1, 0)
    assert title == "Things to check (8)"
    text = str(content)
    assert text.count("Review item") == 5
    assert "Open Duplicates" in text
    assert "Acknowledge" not in text
    actor.return_value = ACTOR
    assert "Acknowledge" in str(handler("tab-insights", 1, 0)[1])


@pytest.mark.parametrize(
    "correction,expected", [("unchanged_person_price", 25), ("new_game_price", 40), ("free", 0)]
)
def test_reopened_verified_tariff_and_explicit_settings_changes_only_change_expected_money(
    pg_db, correction, expected
):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute(
            "UPDATE event_stats SET requirements_snapshot=jsonb_set(requirements_snapshot, '{event_price,basis}', '\"person\"') WHERE event_slug='fightbox'"
        )
    postgres.reopen_event("fightbox", user=ACTOR)
    row = postgres.get_checkins("fightbox")[0]
    postgres.update_checkin(
        row["record_id"],
        {"tournament_games_registered": ["Synthetic Game", "Second Game"], "member": False},
    )
    settings = postgres.get_active_settings_with_id()
    if correction == "new_game_price":
        postgres.update_settings(
            settings["record_id"], {"swish_expected_per_game": 40, "require_membership": False}
        )
    elif correction == "free":
        postgres.update_settings(settings["record_id"], {"require_payment": False})
    # All factual edits are still drafts.
    with pg_db.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT payment_expected FROM event_archive WHERE event_slug='fightbox'"
            ).fetchone()[0]
            == 25
        )
    postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT payment_amount,payment_expected,member FROM event_archive WHERE event_slug='fightbox'"
        ).fetchone() == (Decimal("25"), Decimal(expected), False)
    policy = event(pg_db, "fightbox")["requirements_snapshot"]
    if correction == "unchanged_person_price":
        assert policy["event_price"] == {"amount": "25", "basis": "person"}
    elif correction == "new_game_price":
        assert policy["event_price"] == {"amount": "40", "basis": "person"}
        assert policy["require_membership"] is False
    else:
        assert "event_price" not in policy
        assert policy["require_payment"] is False


def test_removing_every_reopened_row_commits_empty_event_and_zero_archive_profile(pg_db):
    seed_history(pg_db)
    postgres.reopen_event("fightbox", user=ACTOR)
    postgres.delete_checkin(postgres.get_checkins("fightbox")[0]["record_id"])
    result = postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    assert result["archived"] == 0 and result["stats_stale"] is False
    assert event(pg_db, "fightbox")["total_participants"] == 0
    assert event(pg_db, "fightbox")["requirements_snapshot"]["event_price"]["amount"] == "25"
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT total_events,first_seen,total_paid FROM players WHERE uuid='b'"
        ).fetchone() == (0, None, Decimal("0"))


def test_reopen_without_restore_then_restore_tracks_legacy_mirror_without_duplicate_cash(pg_db):
    seed_history(pg_db)
    postgres.reopen_event("fightbox", restore_active=False, user=ACTOR)
    postgres.reopen_event("fightbox", restore_active=True, user=ACTOR)
    postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*),sum(payment_amount) FROM event_archive WHERE event_slug='fightbox'"
        ).fetchone() == (1, Decimal("25"))


def test_history_delete_refreshes_later_first_attendance_and_zeroes_removed_profiles(pg_db):
    seed_history(pg_db)
    result = postgres.delete_archived_event("fightbox", user=ACTOR)
    assert result["stats_stale"] is False
    with pg_db.pool.connection() as conn:
        assert conn.execute(
            "SELECT total_events,first_seen FROM players WHERE uuid='b'"
        ).fetchone() == (0, None)
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_log WHERE action='archive_history_deleted'"
            ).fetchone()[0]
            == 1
        )


def test_row_audits_restore_facts_profiles_stats_and_cleared_active_state(pg_db):
    from psycopg import sql
    from psycopg.types.json import Json

    seed_history(pg_db)
    pg_db.seed_checkin(
        record_id="audit-backfill",
        event_slug="audit-earlier",
        tag="synthetic-a",
        email="a@example.invalid",
        payment_amount=17,
    )
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE settings SET active_event_slug='audit-earlier'")

    def checksums():
        with pg_db.pool.connection() as conn:
            return {
                table: conn.execute(
                    f"SELECT md5(jsonb_agg(to_jsonb(t) ORDER BY {key})::text) FROM {table} t"
                ).fetchone()[0]
                for table, key in (
                    ("players", "uuid"),
                    ("event_archive", "id"),
                    ("event_stats", "event_slug"),
                    ("active_event_data", "record_id"),
                    ("settings", "id"),
                )
            }

    before = checksums()
    with pg_db.pool.connection() as conn:
        start = conn.execute("SELECT coalesce(max(id),0) FROM audit_log").fetchone()[0]
    postgres.archive_event("audit-earlier", event_date="2025-12-01", clear_active=True, user=ACTOR)
    assert checksums() != before
    with pg_db.pool.connection() as conn:
        audits = conn.execute(
            "SELECT action,details FROM audit_log WHERE id>%s ORDER BY id DESC", (start,)
        ).fetchall()

        def restore(table, key, identity, previous):
            identifiers = (sql.Identifier(table), sql.Identifier(key))
            if previous is None:
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE {}=%s").format(*identifiers), (identity,)
                )
                return
            fields = list(previous)
            exists = conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE {}=%s").format(*identifiers), (identity,)
            ).fetchone()
            if exists:
                conn.execute(
                    sql.SQL(
                        "UPDATE {} SET ({})=(SELECT {} FROM jsonb_populate_record(NULL::{},%s::jsonb)) WHERE {}=%s"
                    ).format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, fields)),
                        sql.SQL(",").join(map(sql.Identifier, fields)),
                        sql.Identifier(table),
                        sql.Identifier(key),
                    ),
                    (Json(previous), identity),
                )
            else:
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {} SELECT * FROM jsonb_populate_record(NULL::{},%s::jsonb)"
                    ).format(sql.Identifier(table), sql.Identifier(table)),
                    (Json(previous),),
                )

        for action, raw in audits:
            details = json.loads(raw)
            if action == "archive_active_finalized":
                for table, key, part in (
                    ("active_event_data", "record_id", "active"),
                    ("settings", "id", "settings"),
                ):
                    for row in details[part]["before"]:
                        restore(table, key, row[key], row)
            elif action == "automatic_stats_recomputed":
                for row in details["events"]:
                    restore(
                        "event_stats",
                        "event_slug",
                        row["event_slug"],
                        row["before"] if row["before_exists"] else None,
                    )
                for row in details["players"]:
                    restore("players", "uuid", row["player_uuid"], row["before"])
            elif action == "archive_facts_committed":
                for row in details["after"]:
                    restore("event_archive", "id", row["id"], None)
                for row in details["before"]:
                    restore("event_archive", "id", row["id"], row)
                for table, rows in details["tables"].items():
                    key = "uuid" if table == "players" else "event_slug"
                    for row in rows:
                        restore(table, key, row["key"], row["before"])
    assert checksums() == before


def test_empty_rearchived_event_remains_the_previous_series_cohort(pg_db):
    seed_history(pg_db)
    pg_db.insert("event_stats", {"event_slug": "fightbox-2", "event_date": "2026-01-02"})
    pg_db.insert(
        "event_archive",
        {
            "event_slug": "fightbox-2",
            "event_date": "2026-01-02",
            "player_uuid": "a",
            "tag": "synthetic-a",
            "email": "a@example.invalid",
        },
    )
    postgres.refresh_archive_stats(user=ACTOR)
    assert event(pg_db, "fightbox-3")["same_series_return_rate"] == 100
    postgres.reopen_event("fightbox-2", user=ACTOR)
    postgres.delete_checkin(postgres.get_checkins("fightbox-2")[0]["record_id"])
    postgres.archive_event("fightbox-2", clear_active=True, user=ACTOR)
    assert event(pg_db, "fightbox-2")["same_series_base_count"] == 1
    assert event(pg_db, "fightbox-2")["same_series_return_rate"] == 0
    assert event(pg_db, "fightbox-3")["same_series_return_rate"] is None
    read = {item["event_slug"]: item for item in postgres.get_event_history_dashboard()}
    assert read["fightbox-3"]["same_series_return_rate"] is None


@pytest.mark.parametrize("archived_day", [None, "2026-01-01"])
def test_reopen_preserves_event_date_fallback_and_requires_explicit_unknown_date(
    pg_db, archived_day
):
    seed_history(pg_db)
    with pg_db.pool.connection() as conn:
        conn.execute("UPDATE event_stats SET event_date=NULL WHERE event_slug='fightbox'")
        conn.execute(
            "UPDATE event_archive SET event_date=%s WHERE event_slug='fightbox'", (archived_day,)
        )
        conn.execute("UPDATE settings SET event_date='2099-01-01'")
    historical = event(pg_db, "fightbox")
    postgres.reopen_event("fightbox", user=ACTOR)
    assert event(pg_db, "fightbox") == historical
    settings_day = postgres.get_active_settings()["event_date"]
    assert (str(settings_day) if settings_day else None) == archived_day
    if archived_day is None:
        with pytest.raises(postgres.ArchiveError, match="date is unknown"):
            postgres.archive_event("fightbox", clear_active=True, user=ACTOR)
        assert len(postgres.get_checkins("fightbox")) == 1
    postgres.archive_event(
        "fightbox", event_date=archived_day or "2026-01-01", clear_active=True, user=ACTOR
    )
    assert str(event(pg_db, "fightbox")["event_date"]) == "2026-01-01"

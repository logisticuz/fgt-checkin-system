"""C443: registered live duplicate dialog -> shared merge hook -> deferred END healing."""

import json

import pytest

from shared import postgres_api as postgres
from tests.test_auto_stats_refresh import ACTOR, event, seed_history
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_live_duplicate_ui import click, open_id, text_of
from tests.test_stats_recompute import private_stats_lock_namespace  # noqa: F401


@pytest.mark.parametrize("action", ["keep_candidate", "keep_checkin"])
@pytest.mark.parametrize("undo_while_live", [False, True])
def test_real_live_dialog_merge_and_undo_defer_then_end_heals(
    pg_db, dashboard, monkeypatch, action, undo_while_live
):
    seed_history(pg_db)
    settings = postgres.get_active_settings_with_id()
    postgres.update_settings(
        settings["record_id"], {"active_event_slug": "live-event", "event_date": "2026-01-05"}
    )
    record_id = postgres.begin_checkin(
        "live-event", {"name": "Synthetic b", "tag": "synthetic-b", "email": "b@example.invalid"}
    )["checkin_id"]
    before = {slug: event(pg_db, slug) for slug in ("fightbox", "fightbox-3")}
    context = postgres._stats_context

    def no_live_context(*args, **kwargs):
        pytest.fail("A live-dialog merge/undo must defer before the statistics context scan")

    monkeypatch.setattr(postgres, "_stats_context", no_live_context)
    for name in ("get_live_duplicate_pair", "resolve_live_duplicate", "get_merge_history"):
        monkeypatch.setattr(dashboard.storage, name, getattr(postgres, name), raising=False)
    opener = dashboard.callbacks["open_live_duplicate"]
    monkeypatch.setitem(opener.__globals__, "current_user", lambda: {**ACTOR, "is_dev": False})
    click(open_id(record_id, "a"))
    style, _, _, _, target, _ = opener([1])
    assert style == {"display": "block"} and target["record_id"] == record_id
    button = "btn-live-dup-keep-b" if action == "keep_candidate" else "btn-live-dup-keep-a"
    click(button + ".n_clicks")
    result = dashboard.callbacks["resolve_live_duplicate_choice"](1, 1, 0, 0, target)
    assert "Merged: kept" in text_of(result[2])
    survivor = "a" if action == "keep_candidate" else "b"
    with pg_db.pool.connection() as conn:
        merge_id = conn.execute("SELECT id FROM merge_log").fetchone()[0]
        assert (
            conn.execute(
                "SELECT player_uuid FROM active_event_data WHERE record_id=%s", (record_id,)
            ).fetchone()[0]
            == survivor
        )
        assert conn.execute(
            "SELECT reason,last_error_code FROM event_stats_refresh"
        ).fetchone() == ("merge", "active_event")
        assert (
            conn.execute(
                "SELECT before_totals->'after'->>'player' FROM merge_log WHERE id=%s", (merge_id,)
            ).fetchone()[0]
            == conn.execute(
                "SELECT md5(to_jsonb(p)::text) FROM players p WHERE uuid=%s", (survivor,)
            ).fetchone()[0]
        )
    assert {slug: event(pg_db, slug) for slug in before} == before
    if undo_while_live:
        postgres.undo_merge(merge_id, user=ACTOR)
        assert {slug: event(pg_db, slug) for slug in before} == before
        with pg_db.pool.connection() as conn:
            assert conn.execute(
                "SELECT reason,last_error_code,generation FROM event_stats_refresh"
            ).fetchone() == ("undo_merge", "active_event", 2)
    monkeypatch.setattr(postgres, "_stats_context", context)
    ended = postgres.archive_event("live-event", clear_active=True, user=ACTOR)
    assert ended["stats_stale"] is False
    assert event(pg_db, "fightbox-3")["new_players"] == int(undo_while_live)
    with pg_db.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM event_stats_refresh").fetchone()[0] == 0
        actions = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT details FROM audit_log WHERE action='player_merged'"
            ).fetchall()
        ]
        assert any(item.get("source") == "live_duplicate" for item in actions)

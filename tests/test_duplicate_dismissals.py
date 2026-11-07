"""T-056: "Not same" in the duplicate tool is persisted, filtered out of scans and undoable."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest
from dash import no_update
from dash._callback_context import context_value
from dash._utils import AttributeDict
from dash._validate import validate_multi_return
from dash.exceptions import PreventUpdate
from psycopg.errors import CheckViolation

from scripts import migration_preflight
from shared import migrate, postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard  # noqa: F401  (pytest fixture)
from tests.test_lock_coverage import (
    _observe_commands,
    _private_lock_connections,
    _submit,
    _wait_for_lock,
)
from tests.test_safe_archiving import (  # noqa: F401  (pytest fixture)
    archive_db,
    seed_player,
    table_rows,
)

# --- storage (real test Postgres) -----------------------------------------------------------


def pair_keys(candidates):
    return {tuple(sorted((c["player_a"]["uuid"], c["player_b"]["uuid"]))) for c in candidates}


@pytest.fixture
def twins(archive_db):  # noqa: F811
    """Two synthetic players with the same phone and a near-identical tag (a HIGH pair)."""
    a = seed_player(archive_db, "test-twin-one", name="Synthetic Twin")
    b = seed_player(archive_db, "test-twin-onf", name="Synthetic Twin")
    return a, b


@pytest.mark.db
def test_dismissed_pair_stays_out_of_the_scan_until_undone(twins):
    a, b = twins
    key = tuple(sorted((a["uuid"], b["uuid"])))
    assert key in pair_keys(postgres.find_duplicate_candidates())

    assert postgres.dismiss_duplicate_pair(b["uuid"], a["uuid"], dismissed_by="Synthetic TO")
    assert key not in pair_keys(postgres.find_duplicate_candidates())
    assert postgres.is_duplicate_pair_dismissed(a["uuid"], b["uuid"])

    assert postgres.undo_duplicate_dismissal(a["uuid"], b["uuid"]) is True
    assert key in pair_keys(postgres.find_duplicate_candidates())
    assert postgres.undo_duplicate_dismissal(a["uuid"], b["uuid"]) is False


@pytest.mark.db
def test_pair_is_stored_once_in_order_and_dismiss_is_idempotent(twins, archive_db):  # noqa: F811
    a, b = twins
    assert postgres.dismiss_duplicate_pair(a["uuid"], b["uuid"], dismissed_by="TO one")
    assert postgres.dismiss_duplicate_pair(b["uuid"], a["uuid"], dismissed_by="TO two")
    with archive_db.pool.connection() as connection:
        rows = connection.execute(
            "SELECT uuid_a, uuid_b, dismissed_by FROM player_duplicate_dismissals"
        ).fetchall()
    assert rows == [(*sorted((a["uuid"], b["uuid"])), "TO one")]

    listed = postgres.get_duplicate_dismissals()
    assert len(listed) == 1 and listed[0]["dismissed_by"] == "TO one"
    assert {listed[0]["player_a"]["tag"], listed[0]["player_b"]["tag"]} == {a["tag"], b["tag"]}
    assert "telephone" not in listed[0]["player_a"] and "email" not in listed[0]["player_a"]


@pytest.mark.db
def test_table_holds_no_contact_data_and_rejects_unordered_pairs(archive_db):  # noqa: F811
    with archive_db.pool.connection() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'player_duplicate_dismissals' AND table_schema = %s",
                (archive_db.schema,),
            ).fetchall()
        }
        # T-068 adds confirmed_rows: record_id -> {opaque identity hash, profile uuid} only.
        assert columns == {
            "id",
            "uuid_a",
            "uuid_b",
            "dismissed_at",
            "dismissed_by",
            "confirmed_rows",
        }
        with pytest.raises(CheckViolation):
            connection.execute(
                "INSERT INTO player_duplicate_dismissals (uuid_a, uuid_b) VALUES ('b', 'a')"
            )


@pytest.mark.parametrize("pair", [("", "x"), ("x", "x"), (None, "x")])
def test_a_pair_needs_two_different_uuids(pair):
    with pytest.raises(ValueError):
        postgres.ordered_duplicate_pair(*pair)


@pytest.mark.db
def test_dismissed_pairs_do_not_use_up_the_candidate_limit(archive_db):  # noqa: F811
    first = [seed_player(archive_db, f"test-limit-a{i}", name="Synthetic A") for i in "12"]
    second = [
        seed_player(archive_db, f"test-other-b{i}", name="Synthetic B", telephone="0000000001")
        for i in "12"
    ]
    one = postgres.find_duplicate_candidates(limit=1)
    assert len(one) == 1
    shown = one[0]
    postgres.dismiss_duplicate_pair(shown["player_a"]["uuid"], shown["player_b"]["uuid"])
    after = postgres.find_duplicate_candidates(limit=1)
    assert len(after) == 1 and pair_keys(after) != pair_keys(one)
    assert {p["uuid"] for p in first + second} >= {
        after[0]["player_a"]["uuid"],
        after[0]["player_b"]["uuid"],
    }


@pytest.mark.db
def test_dismissing_a_merged_away_player_saves_nothing(twins, archive_db):  # noqa: F811
    a, _ = twins
    assert postgres.dismiss_duplicate_pair(a["uuid"], "missing-player-uuid") is False
    assert postgres.get_duplicate_dismissals() == []


@pytest.mark.db
def test_merge_cleans_removed_profile_dismissals_and_undo_still_works(twins, archive_db):
    a, b = twins
    keep = seed_player(archive_db, "test-keep")
    low = seed_player(archive_db, "test-low", uuid="00-control")
    high = seed_player(archive_db, "test-high", uuid="zz-control")
    for left, right in ((b, low), (b, high), (a, b), (a, low), (keep, high)):
        assert postgres.dismiss_duplicate_pair(left["uuid"], right["uuid"])
    retained = {
        tuple(sorted((a["uuid"], low["uuid"]))),
        tuple(sorted((keep["uuid"], high["uuid"]))),
    }
    before_players = table_rows(archive_db, "players")

    merge = postgres.merge_players(keep["uuid"], b["uuid"], reason="synthetic test merge")
    rows = table_rows(archive_db, "player_duplicate_dismissals")
    assert {(row["uuid_a"], row["uuid_b"]) for row in rows} == retained
    assert all(b["uuid"] not in (row["uuid_a"], row["uuid_b"]) for row in rows)

    result = postgres.undo_merge(merge["merge_id"])
    assert result["undone"] is True and result["restored_uuid"] == b["uuid"]
    assert table_rows(archive_db, "players") == before_players
    # Cleanup deliberately does not restore decisions about the removed profile.
    assert table_rows(archive_db, "player_duplicate_dismissals") == rows


@pytest.mark.db
def test_merge_failure_rolls_back_dismissal_cleanup(twins, archive_db, _private_lock_connections):
    a, b = twins
    assert postgres.dismiss_duplicate_pair(a["uuid"], b["uuid"])
    before_players = table_rows(archive_db, "players")
    before_dismissals = table_rows(archive_db, "player_duplicate_dismissals")

    def fail_merge_log(query):
        if "INSERT INTO merge_log" in query:
            raise RuntimeError("synthetic merge log failure")

    _observe_commands(archive_db, before=fail_merge_log)
    with pytest.raises(RuntimeError, match="synthetic merge log failure"):
        postgres.merge_players(a["uuid"], b["uuid"])
    assert table_rows(archive_db, "players") == before_players
    assert table_rows(archive_db, "player_duplicate_dismissals") == before_dismissals
    assert table_rows(archive_db, "merge_log") == []


@pytest.mark.db
def test_undo_audit_failure_rolls_back_the_dismissal_removal(
    twins, archive_db, _private_lock_connections
):
    a, b = twins
    assert postgres.dismiss_duplicate_pair(a["uuid"], b["uuid"])
    before = table_rows(archive_db, "player_duplicate_dismissals")

    def fail_audit(query):
        if "INSERT INTO audit_log" in query:
            raise RuntimeError("synthetic audit failure")

    _observe_commands(archive_db, before=fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        postgres.undo_duplicate_dismissal(a["uuid"], b["uuid"], user=USER)
    assert table_rows(archive_db, "player_duplicate_dismissals") == before
    assert table_rows(archive_db, "audit_log") == []


@pytest.mark.db
def test_cleanup_migration_is_idempotent_and_runner_preflight_pass(pg_db, monkeypatch, capsys):
    first = seed_player(pg_db, "test-live-a", uuid="live-a")
    second = seed_player(pg_db, "test-live-b", uuid="live-b")
    migration = (
        Path(__file__).resolve().parents[1]
        / "shared/migrations/008_duplicate_dismissal_cleanup.sql"
    )
    source = migration.read_text(encoding="utf-8")
    migrate.lint(source, migration.name)
    pairs = [
        (first["uuid"], second["uuid"]),
        ("gone-a", first["uuid"]),
        (second["uuid"], "missing-z"),
        ("missing-a", "missing-b"),
    ]
    with pg_db.pool.connection() as connection:
        for pair in pairs:
            connection.execute(
                "INSERT INTO player_duplicate_dismissals (uuid_a, uuid_b, dismissed_by) "
                "VALUES (%s, %s, 'Synthetic TO')",
                tuple(sorted(pair)),
            )
        kept = connection.execute(
            "SELECT * FROM player_duplicate_dismissals WHERE uuid_a = 'live-a' AND uuid_b = 'live-b'"
        ).fetchall()
        assert connection.execute(source, prepare=False).rowcount == 3
        assert connection.execute(source, prepare=False).rowcount == 0
        assert connection.execute("SELECT * FROM player_duplicate_dismissals").fetchall() == kept

    assert migration.name in migrate.apply(pg_db.pool)
    assert migrate.apply(pg_db.pool) == []
    with pg_db.pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fgt_schema_migrations WHERE version = '008'"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.setattr(
            migration_preflight.psycopg, "connect", lambda *args, **kwargs: nullcontext(connection)
        )
        assert migration_preflight.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True and report["ready_to_deploy"] is True
    assert report["pending_migrations"] == report["required_unapplied"] == []


@pytest.mark.db
def test_dismiss_waits_for_a_running_merge(
    twins, archive_db, _private_lock_connections
):  # noqa: F811
    a, b = twins
    with ThreadPoolExecutor(max_workers=1) as workers:
        with archive_db.pool.connection() as holder:
            with holder.transaction():  # stands in for merge_players' exclusive lock
                holder.execute("SELECT pg_advisory_xact_lock(hashtext('fgt:player-merges'))")
                writing = _submit(workers, postgres.dismiss_duplicate_pair, a["uuid"], b["uuid"])
                _wait_for_lock(archive_db, "fgt:player-merges", writing, "ShareLock")
                assert not writing.done()
        # The holder's transaction has ended (lock released) before the executor joins.
        assert writing.result(timeout=10) is True


@pytest.mark.db
def test_count_and_paging_reach_every_live_dismissal(archive_db):  # noqa: F811
    players = [
        seed_player(archive_db, f"test-page-{i:03d}", name=f"Synthetic Page {i}") for i in range(12)
    ]
    for i in range(0, 12, 2):
        postgres.dismiss_duplicate_pair(players[i]["uuid"], players[i + 1]["uuid"])
    assert postgres.count_duplicate_dismissals() == 6
    first = postgres.get_duplicate_dismissals(limit=4)
    rest = postgres.get_duplicate_dismissals(limit=4, offset=4)
    assert len(first) == 4 and len(rest) == 2
    keys = {(d["uuid_a"], d["uuid_b"]) for d in first + rest}
    assert keys == postgres.get_dismissed_pair_keys()


@pytest.mark.db
def test_scan_still_works_without_the_table(twins, monkeypatch):
    monkeypatch.setattr(postgres, "DISMISSALS_TABLE", "missing_dismissals_table")
    a, b = twins
    assert tuple(sorted((a["uuid"], b["uuid"]))) in pair_keys(postgres.find_duplicate_candidates())
    assert postgres.get_duplicate_dismissals() == []
    assert postgres.count_duplicate_dismissals() == 0
    assert postgres.is_duplicate_pair_dismissed(a["uuid"], b["uuid"]) is False


# --- dashboard callbacks (storage mocked) ----------------------------------------------------

PLAYER_A = {
    "uuid": "uuid-a",
    "name": "Synthetic A",
    "tag": "syn_a",
    "telephone": "",
    "total_events": 1,
}
PLAYER_B = {
    "uuid": "uuid-b",
    "name": "Synthetic B",
    "tag": "syn_b",
    "telephone": "",
    "total_events": 2,
}
PLAYER_C = {
    "uuid": "uuid-c",
    "name": "Synthetic C",
    "tag": "syn_c",
    "telephone": "",
    "total_events": 3,
}
CANDIDATES = [
    {"player_a": PLAYER_A, "player_b": PLAYER_B, "reasons": ["similar_tag"], "confidence": "high"},
    {"player_a": PLAYER_A, "player_b": PLAYER_C, "reasons": ["similar_name"], "confidence": "low"},
]
CARD_AB = {"type": "dup-card", "a": "uuid-a", "b": "uuid-b"}
CARD_AC = {"type": "dup-card", "a": "uuid-a", "b": "uuid-c"}
USER = {
    "user_id": "42",
    "user_name": "Synthetic TO",
    "email": "to@example.invalid",
    "is_dev": False,
}


def click(button, outputs=None, value=1):
    prop = json.dumps(button, sort_keys=True, separators=(",", ":")) + ".n_clicks"
    context_value.set(
        AttributeDict(
            triggered_inputs=[{"prop_id": prop, "value": value}],
            outputs_list=outputs or [],
        )
    )


def card_outputs():
    return [
        {"id": "duplicates-feedback", "property": "children"},
        [{"id": CARD_AB, "property": "style"}, {"id": CARD_AC, "property": "style"}],
        {"id": "duplicates-dismissed", "property": "children"},
    ]


def assert_dash_accepts(result):
    """Run a return value through Dash's own multi-output validator (wildcards need lists)."""
    validate_multi_return(card_outputs(), list(result), "dismiss_not_same")


def dismissal(a, b, number=0):
    return {
        "uuid_a": a,
        "uuid_b": b,
        "dismissed_at": f"2026-11-07T12:{number % 60:02d}:00+00:00",
        "dismissed_by": "Synthetic TO",
        "player_a": {"uuid": a, "name": f"Synthetic {a}", "tag": f"tag-{a}"},
        "player_b": {"uuid": b, "name": f"Synthetic {b}", "tag": f"tag-{b}"},
    }


@pytest.fixture
def dup(dashboard, monkeypatch):  # noqa: F811
    store = dashboard.storage
    store.find_duplicate_candidates = Mock(return_value=list(CANDIDATES))
    store.dismiss_duplicate_pair = Mock(return_value=True)
    store.undo_duplicate_dismissal = Mock(return_value=True)
    store.is_duplicate_pair_dismissed = Mock(return_value=False)
    store.get_duplicate_dismissals = Mock(return_value=[])
    store.count_duplicate_dismissals = Mock(return_value=0)
    store.get_dismissed_pair_keys = Mock(return_value=set())
    store.merge_players = Mock(return_value={"keep_player_tag": "syn_a"})
    store.get_merge_history = Mock(return_value=[])
    callbacks_module = dashboard.callbacks["dismiss_not_same"].__globals__
    monkeypatch.setitem(callbacks_module, "current_user", lambda: dict(USER))
    return dashboard


def text_of(component):
    if component is None or isinstance(component, (str, int, float)):
        return "" if component is None else str(component)
    if isinstance(component, (list, tuple)):
        return " ".join(text_of(c) for c in component)
    return text_of(getattr(component, "children", None))


def ids_in(component, found=None):
    found = [] if found is None else found
    if isinstance(component, (list, tuple)):
        for child in component:
            ids_in(child, found)
    elif hasattr(component, "to_plotly_json"):
        if getattr(component, "id", None) is not None:
            found.append(component.id)
        ids_in(getattr(component, "children", None), found)
    return found


def test_not_same_saves_hides_only_that_card_and_names_the_real_user(dup):
    dup.storage.get_duplicate_dismissals.return_value = [dismissal("uuid-a", "uuid-b")]
    dup.storage.count_duplicate_dismissals.return_value = 1
    dup.storage.get_dismissed_pair_keys.return_value = {("uuid-a", "uuid-b")}
    click({"type": "btn-not-same", "a": "uuid-a", "b": "uuid-b"}, card_outputs())
    result = dup.callbacks["dismiss_not_same"]([1, None], [{"padding": "1px"}, {}])
    assert_dash_accepts(result)
    feedback, styles, dismissed = result

    dup.storage.dismiss_duplicate_pair.assert_called_once_with(
        "uuid-a", "uuid-b", dismissed_by="Synthetic TO"
    )
    dup.storage.find_duplicate_candidates.assert_not_called()  # no full re-scan
    assert styles == [{"padding": "1px", "display": "none"}, no_update]
    assert text_of(feedback) == "Marked as different players."
    assert "Dismissed pairs (1)" in text_of(dismissed)
    assert {"type": "btn-undo-dismiss", "a": "uuid-a", "b": "uuid-b"} in ids_in(dismissed)

    audit_user = dup.storage.log_action.call_args.args[0]
    assert audit_user["user_name"] == "Synthetic TO" and audit_user["user_id"] == "42"
    assert dup.storage.log_action.call_args.args[1] == "duplicate_dismissed"


def test_overlapping_clicks_never_reshow_a_dismissed_card(dup):
    """Review P2: a late response must not restore another card from stale State."""
    # The second click's request still believes card A-B is visible (stale State), but
    # A-B was saved by the first, overlapping click.
    dup.storage.get_dismissed_pair_keys.return_value = {("uuid-a", "uuid-b"), ("uuid-a", "uuid-c")}
    click({"type": "btn-not-same", "a": "uuid-a", "b": "uuid-c"}, card_outputs())
    _, styles, _ = dup.callbacks["dismiss_not_same"]([1, 1], [{}, {}])
    assert styles == [{"display": "none"}, {"display": "none"}]

    # Without a stored decision for the other card, it is left alone - never set visible.
    dup.storage.get_dismissed_pair_keys.return_value = {("uuid-a", "uuid-c")}
    _, styles, _ = dup.callbacks["dismiss_not_same"]([1, 1], [{"display": "none"}, {}])
    assert styles[0] is no_update and styles[1] == {"display": "none"}


def test_the_title_is_not_a_server_output_of_not_same(dup):
    assert "duplicates-title" not in str(dup.definitions["dismiss_not_same"]["output"])


def test_an_email_is_never_stored_as_the_dismisser(dup, monkeypatch):
    module = dup.callbacks["dismiss_not_same"].__globals__
    monkeypatch.setitem(module, "current_user", lambda: {**USER, "user_name": "to@example.invalid"})
    click({"type": "btn-not-same", "a": "uuid-a", "b": "uuid-b"}, card_outputs())
    dup.callbacks["dismiss_not_same"]([1, None], [{}, {}])
    assert dup.storage.dismiss_duplicate_pair.call_args.kwargs["dismissed_by"] == "user-42"


def test_new_buttons_appearing_do_not_count_as_a_click(dup):
    """Review P2: no scalar no_update for the wildcard output; nothing to do = PreventUpdate."""
    click({"type": "btn-not-same", "a": "uuid-a", "b": "uuid-b"}, card_outputs(), value=None)
    with pytest.raises(PreventUpdate):
        dup.callbacks["dismiss_not_same"]([None, None], [{}, {}])
    dup.storage.dismiss_duplicate_pair.assert_not_called()


def test_a_failed_save_keeps_the_cards_and_says_so_in_a_valid_shape(dup):
    """Review P2: the error feedback must survive Dash's validator (was HTTP 500)."""
    dup.storage.dismiss_duplicate_pair.side_effect = RuntimeError("db down")
    click({"type": "btn-not-same", "a": "uuid-a", "b": "uuid-b"}, card_outputs())
    result = dup.callbacks["dismiss_not_same"]([1, None], [{}, {}])
    assert_dash_accepts(result)
    feedback, styles, dismissed = result
    assert "nothing changed" in text_of(feedback)
    assert styles == [no_update, no_update] and dismissed is no_update
    dup.storage.log_action.assert_not_called()


def test_dismissed_list_shows_the_true_total_and_pages_through_all(dup):
    """Review P2: 101 stored pairs -> "(101)", a page of rows, and Show more until the end."""
    every = [dismissal(f"a{i:03d}", f"b{i:03d}", i) for i in range(101)]
    dup.storage.count_duplicate_dismissals.return_value = 101
    dup.storage.get_duplicate_dismissals.side_effect = lambda limit=100, offset=0: every[
        offset : offset + limit
    ]
    module = dup.callbacks["dismiss_not_same"].__globals__
    page = module["DISMISSED_PAGE"]

    cards, _, _ = dup.callbacks["scan_duplicates"](1)
    assert "Dismissed pairs (101)" in text_of(cards)
    undo_ids = [i for i in ids_in(cards) if isinstance(i, dict) and i["type"] == "btn-undo-dismiss"]
    assert len(undo_ids) == page
    assert {"type": "btn-dismissed-more", "shown": page} in ids_in(cards)
    assert f"Show more ({101 - page} more)" in text_of(cards)

    shown = page
    while True:
        click({"type": "btn-dismissed-more", "shown": shown})
        section = dup.callbacks["show_more_dismissed"]([1])
        undo_ids = [
            i for i in ids_in(section) if isinstance(i, dict) and i["type"] == "btn-undo-dismiss"
        ]
        assert section[0].open is True  # the list stays open while paging
        more = [
            i for i in ids_in(section) if isinstance(i, dict) and i["type"] == "btn-dismissed-more"
        ]
        if not more:
            break
        shown = more[0]["shown"]
    assert len(undo_ids) == 101
    assert {"type": "btn-undo-dismiss", "a": "a100", "b": "b100"} in undo_ids  # the oldest


def test_scan_cards_are_addressable_and_end_with_the_dismissed_section(dup):
    cards, _, title = dup.callbacks["scan_duplicates"](1)
    assert title == "Potential duplicates (2)"
    found = ids_in(cards)
    assert CARD_AB in found and CARD_AC in found
    assert found.count("duplicates-dismissed") == 1

    dup.storage.find_duplicate_candidates.return_value = []
    empty, _, _ = dup.callbacks["scan_duplicates"](1)
    assert "duplicates-dismissed" in ids_in(empty)  # the Undo list is reachable with 0 cards


def test_undo_removes_the_dismissal_and_rebuilds_the_list(dup):
    click({"type": "btn-undo-dismiss", "a": "uuid-a", "b": "uuid-b"})
    feedback, cards, title = dup.callbacks["undo_not_same"]([1])
    dup.storage.undo_duplicate_dismissal.assert_called_once_with(
        "uuid-a", "uuid-b", user={"user_id": "42", "user_name": "Synthetic TO"}
    )
    assert CARD_AB in ids_in(cards) and title == "Potential duplicates (2)"
    dup.storage.log_action.assert_not_called()  # storage writes the single transactional audit
    assert "Undone" in text_of(feedback)


@pytest.mark.db
@pytest.mark.parametrize(
    "actor_name, expected_name",
    [("Synthetic TO", "Synthetic TO"), ("to@example.invalid", "user-42")],
)
def test_undo_callback_writes_one_real_audit_without_contacts(
    dup, archive_db, monkeypatch, actor_name, expected_name
):
    a = seed_player(archive_db, "test-undo-audit-a")
    b = seed_player(archive_db, "test-undo-audit-b")
    assert postgres.dismiss_duplicate_pair(a["uuid"], b["uuid"])
    monkeypatch.setattr(dup.storage, "undo_duplicate_dismissal", postgres.undo_duplicate_dismissal)
    module = dup.callbacks["undo_not_same"].__globals__
    monkeypatch.setitem(module, "current_user", lambda: {**USER, "user_name": actor_name})
    click({"type": "btn-undo-dismiss", "a": b["uuid"], "b": a["uuid"]})
    feedback, _, _ = dup.callbacks["undo_not_same"]([1])
    assert "Undone" in text_of(feedback)
    rows = table_rows(archive_db, "audit_log")
    assert len(rows) == 1
    audit = rows[0]
    assert (audit["action"], audit["user_id"], audit["user_name"]) == (
        "duplicate_dismissal_undone",
        "42",
        expected_name,
    )
    assert audit["user_email"] is None
    assert audit["target_table"] == "players"
    assert audit["target_player"] == min(a["uuid"], b["uuid"])
    assert json.loads(audit["details"]) == {"pair": sorted((a["uuid"], b["uuid"]))}
    assert USER["email"] not in json.dumps(audit, default=str)
    assert table_rows(archive_db, "player_duplicate_dismissals") == []
    dup.storage.log_action.assert_not_called()
    assert postgres.undo_duplicate_dismissal(a["uuid"], b["uuid"], user=USER) is False
    assert table_rows(archive_db, "audit_log") == rows


def test_merge_of_a_dismissed_pair_from_a_stale_list_is_refused(dup):
    dup.storage.is_duplicate_pair_dismissed.return_value = True
    feedback, history, cards, title = dup.callbacks["execute_merge"](1, "uuid-a", "uuid-b")
    dup.storage.merge_players.assert_not_called()
    assert "marked as different players" in text_of(feedback)
    assert history is no_update


def test_merge_of_an_ordinary_pair_is_unchanged(dup):
    dup.callbacks["execute_merge"](1, "uuid-a", "uuid-b")
    dup.storage.merge_players.assert_called_once_with(
        "uuid-a", "uuid-b", reason="Manual merge via dashboard"
    )

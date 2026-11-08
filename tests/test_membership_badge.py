"""T-075: the TO sees "eBas svarade inte" separately from "Inte medlem" (synthetic data)."""

import pytest

from fgt_dashboard import membership_badge as badge
from shared import postgres_api as postgres
from tests.test_dashboard_row_identity import dashboard  # noqa: F401  (pytest fixture)

REQ = {"require_membership": True}


# --- the mapping -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member,state,reported,expected",
    [
        (True, "confirmed_member", True, badge.MEMBER),
        (True, "unknown", True, badge.MEMBER),  # TO verified and marked as member
        ("✓", "unknown", True, badge.MEMBER),
        (False, "unknown", True, badge.EBAS_UNKNOWN),  # eBas timeout / 5xx / non-JSON
        (None, "unknown", True, badge.EBAS_UNKNOWN),
        (False, "confirmed_nonmember", True, badge.NONMEMBER),
        (False, "skipped", True, badge.MISSING),
        (False, "unknown", False, badge.MISSING),  # NULL state: eBas never asked
        (False, "unknown", None, badge.MISSING),  # adapter without the flag
    ],
)
def test_membership_state(member, state, reported, expected):
    assert badge.membership_state(member, state, reported) == expected


def test_member_cells_differ_and_only_eBas_unknown_gets_the_badge():
    cells = {
        s: badge.member_cell(s)
        for s in (badge.MEMBER, badge.EBAS_UNKNOWN, badge.NONMEMBER, badge.MISSING)
    }
    assert cells == {
        badge.MEMBER: "✓",
        badge.EBAS_UNKNOWN: "⚠ eBas svarade inte",
        badge.NONMEMBER: "✗",
        badge.MISSING: "✗",
    }


# --- TO table ----------------------------------------------------------------------------


def raw_row(record_id, member, state, reported):
    return {
        "record_id": record_id,
        "name": f"Synthetic {record_id}",
        "tag": f"pw-{record_id}",
        "telephone": "0700000000",
        "member": member,
        "member_lookup_state": state,
        "member_lookup_reported": reported,
        "startgg": True,
        "payment_valid": True,
        "status": "Ready" if member else "Pending",
    }


ROWS = [
    raw_row("member", True, "confirmed_member", True),
    raw_row("ebas-down", False, "unknown", True),
    raw_row("nonmember", False, "confirmed_nonmember", True),
    raw_row("skipped", False, "skipped", True),
    raw_row("never-asked", False, "unknown", False),
]


def table(dashboard, requirements, columns=("name", "tag", "member", "status")):  # noqa: F811
    dashboard.storage.get_checkins.return_value = [dict(r) for r in ROWS]
    result = dashboard.callbacks["update_table"](
        "test-event", 0, 0, None, list(columns), "all", None, None, 0, requirements
    )
    return {row["record_id"]: row for row in result[0]}


def test_table_shows_the_eBas_badge_only_for_a_failed_lookup(dashboard):  # noqa: F811
    rows = table(dashboard, REQ)
    assert {rid: row["member"] for rid, row in rows.items()} == {
        "member": "✓",
        "ebas-down": badge.EBAS_UNKNOWN_CELL,
        "nonmember": "✗",
        "skipped": "✗",
        "never-asked": "✗",
    }
    # Every row carries its lookup context (hidden keys), whatever its member flag.
    assert {
        rid: (r["member_lookup_state"], r["member_lookup_reported"]) for rid, r in rows.items()
    } == {
        "member": ("confirmed_member", True),
        "ebas-down": ("unknown", True),
        "nonmember": ("confirmed_nonmember", True),
        "skipped": ("skipped", True),
        "never-asked": ("unknown", False),
    }


def test_needs_attention_knows_even_when_the_member_column_is_hidden(dashboard):  # noqa: F811
    rows = table(dashboard, REQ, columns=("name", "tag", "status"))
    assert "member" not in rows["ebas-down"]
    assert (
        rows["ebas-down"]["member_lookup_state"],
        rows["ebas-down"]["member_lookup_reported"],
    ) == (
        "unknown",
        True,
    )


def test_no_badge_when_membership_is_not_required(dashboard):  # noqa: F811
    rows = table(dashboard, {"require_membership": False})
    assert all("member" not in row for row in rows.values())


def test_the_badge_cell_has_amber_style_and_hover_help():
    from fgt_dashboard import layout

    source = open(layout.__file__, encoding="utf-8").read()
    assert source.count("membership_badge.EBAS_UNKNOWN_CELL") == 2  # style + tooltip
    assert "membership_badge.EBAS_UNKNOWN_HELP" in source


# --- Needs attention ---------------------------------------------------------------------


def spans(component):
    found = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        if type(node).__name__ == "Span":
            found.append(node)
        walk(getattr(node, "children", None) or [])

    walk(component)
    return found


def test_needs_attention_shows_the_reason_and_help(dashboard):  # noqa: F811
    rows = list(table(dashboard, REQ).values())
    items, count = dashboard.callbacks["update_needs_attention"](rows, REQ, {})
    assert count == "4"  # everyone but the member; the eBas row is still pending
    texts = [s.children for s in spans(items) if isinstance(s.children, str)]
    assert texts.count("⚠ eBas svarade inte") == 1
    assert texts.count(badge.EBAS_UNKNOWN_HELP) == 1
    assert texts.count("🎫 MEMBERSHIP") == 3  # nonmember, skipped, never asked
    ebas = [s for s in spans(items) if s.children == "⚠ eBas svarade inte"][0]
    assert ebas.title == badge.EBAS_UNKNOWN_HELP
    assert ebas.style["color"] == "#f59e0b"  # amber, not the red of "not a member"
    cards = {item.children[0].children[1].children: item.style["borderLeft"] for item in items}
    assert cards["Synthetic ebas-down (pw-ebas-down)"] == "3px solid #f59e0b"
    assert cards["Synthetic nonmember (pw-nonmember)"] == "3px solid #ef4444"


# --- local edits keep the badge ----------------------------------------------------------


def test_marking_and_unmarking_member_keeps_the_eBas_context(dashboard):  # noqa: F811
    rows = list(table(dashboard, REQ).values())
    target = [r for r in rows if r["record_id"] == "ebas-down"][0]
    dashboard.storage.update_checkin.return_value = {"fields": {"member": True}}
    cell = {"row": 0, "row_id": target["id"], "column_id": "member"}
    _, updated, _ = dashboard.callbacks["toggle_field"](cell, rows, "test-event", {})
    marked = [r for r in updated if r["record_id"] == "ebas-down"][0]
    assert marked["member"] == "✓"
    assert dashboard.storage.update_checkin.call_args.args[1] == {"member": True}

    dashboard.storage.update_checkin.return_value = {"fields": {"member": False}}
    _, updated, _ = dashboard.callbacks["toggle_field"](cell, updated, "test-event", {})
    unmarked = [r for r in updated if r["record_id"] == "ebas-down"][0]
    assert unmarked["member"] == badge.EBAS_UNKNOWN_CELL
    other = [r for r in updated if r["record_id"] == "nonmember"][0]
    assert other["member"] == "✗"


def test_an_edit_takes_the_lookup_context_storage_returns(dashboard):  # noqa: F811
    """Review P2 (mocked): fresh lookup fields from the edit response win over the loaded row."""
    rows = list(table(dashboard, REQ).values())
    target = [r for r in rows if r["record_id"] == "ebas-down"][0]
    dashboard.storage.update_checkin.return_value = {
        "fields": {
            "member": False,
            "startgg": False,
            "member_lookup_state": "confirmed_nonmember",
            "member_lookup_reported": True,
        }
    }
    cell = {"row": 0, "row_id": target["id"], "column_id": "startgg"}
    _, updated, _ = dashboard.callbacks["toggle_field"](cell, rows, "test-event", {})
    edited = [r for r in updated if r["record_id"] == "ebas-down"][0]
    assert edited["member"] == "✗"
    assert edited["member_lookup_state"] == "confirmed_nonmember"


def test_a_partial_read_never_invents_lookup_context(dashboard):  # noqa: F811
    """Responses without member_lookup_reported (e.g. a read that skipped the column, whose
    member_lookup_state is just the 'unknown' default) leave the row's own context alone."""
    rows = list(table(dashboard, REQ).values())
    target = [r for r in rows if r["record_id"] == "nonmember"][0]
    dashboard.storage.update_checkin.return_value = {
        "fields": {"member": False, "startgg": False, "member_lookup_state": "unknown"}
    }
    cell = {"row": 0, "row_id": target["id"], "column_id": "startgg"}
    _, updated, _ = dashboard.callbacks["toggle_field"](cell, rows, "test-event", {})
    edited = [r for r in updated if r["record_id"] == "nonmember"][0]
    assert (edited["member"], edited["member_lookup_state"]) == ("✗", "confirmed_nonmember")


# --- real PostgreSQL + the registered callbacks (review-1 P2 sequences) -------------------


@pytest.fixture
def real_ui(pg_db, monkeypatch):
    from types import SimpleNamespace

    from dash import Dash

    import fgt_dashboard.callbacks as callbacks

    monkeypatch.setattr(  # SSE notify to the backend: stubbed, nothing leaves the test
        callbacks.requests, "post", lambda *a, **k: SimpleNamespace(status_code=200)
    )
    app = Dash(__name__)
    callbacks.register_callbacks(app)
    handlers = {
        d["callback"].__wrapped__.__name__: d["callback"].__wrapped__
        for d in app.callback_map.values()
        if "callback" in d
    }
    slug = "pw-t075-" + __import__("uuid").uuid4().hex[:8]
    pg_db.seed_settings(
        active_event_slug=slug,
        require_membership=True,
        require_startgg=False,
        require_payment=False,
    )
    checkin = pg_db.seed_checkin(
        event_slug=slug,
        record_id="rec-t075",
        name="Synthetic T075",
        tag="pw-t075",
        member=False,
        member_source="ebas_check",
        status="Pending",
    )
    postgres.apply_integration_result(  # eBas timeout -> member false + 'unknown'
        checkin["record_id"],
        "ebas",
        False,
        {},
        state="unavailable",
        error={"code": "provider_timeout"},
    )
    return SimpleNamespace(callbacks=handlers, slug=slug, record_id=checkin["record_id"])


def load(ui):
    result = ui.callbacks["update_table"](
        ui.slug,
        0,
        0,
        None,
        ["name", "tag", "member", "startgg", "status"],
        "all",
        None,
        None,
        0,
        REQ,
    )
    return result[0]


def toggle(ui, rows, column):
    target = [r for r in rows if r["record_id"] == ui.record_id][0]
    cell = {"row": 0, "row_id": target["id"], "column_id": column}
    _, updated, _ = ui.callbacks["toggle_field"](cell, rows, ui.slug, {})
    return updated, [r for r in updated if r["record_id"] == ui.record_id][0]


def attention_texts(ui, rows):
    items, _ = ui.callbacks["update_needs_attention"](rows, REQ, {})
    return [s.children for s in spans(items) if isinstance(s.children, str)]


@pytest.mark.db
def test_mark_reload_unmark_brings_the_eBas_badge_back(real_ui):
    rows = load(real_ui)
    assert [r["member"] for r in rows] == [badge.EBAS_UNKNOWN_CELL]
    _, marked = toggle(real_ui, rows, "member")
    assert marked["member"] == "✓"
    rows = load(real_ui)  # reload while member is true
    assert [r["member"] for r in rows] == ["✓"]
    rows, unmarked = toggle(real_ui, rows, "member")
    assert unmarked["member"] == badge.EBAS_UNKNOWN_CELL
    texts = attention_texts(real_ui, rows)
    assert "⚠ eBas svarade inte" in texts and "🎫 MEMBERSHIP" not in texts


@pytest.mark.db
def test_a_new_nonmember_result_shows_up_with_an_unrelated_edit(real_ui):
    rows = load(real_ui)
    assert [r["member"] for r in rows] == [badge.EBAS_UNKNOWN_CELL]
    postgres.apply_integration_result(  # eBas answers later: confirmed non-member
        real_ui.record_id, "ebas", True, {"member": False}, state="verified_false"
    )
    rows, edited = toggle(real_ui, rows, "startgg")  # the TO edits Start.gg on the old table
    assert edited["startgg"] == "✓"
    assert edited["member"] == "✗"
    texts = attention_texts(real_ui, rows)
    assert "🎫 MEMBERSHIP" in texts and "⚠ eBas svarade inte" not in texts


# --- storage: the real eBas failure path -------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, badge.MISSING),  # never asked (no personnummer)
        ("confirmed_nonmember", badge.NONMEMBER),
        ("skipped", badge.MISSING),
    ],
)
def test_get_checkins_tells_never_asked_from_a_failed_lookup(pg_db, stored, expected):
    checkin = pg_db.seed_checkin(member=False, member_lookup_state=stored)
    row = postgres.get_checkins(checkin["event_slug"])[0]
    assert row["member_lookup_reported"] is (stored is not None)
    state = badge.membership_state(
        row["member"], row["member_lookup_state"], row["member_lookup_reported"]
    )
    assert state == expected


@pytest.mark.db
def test_an_eBas_timeout_becomes_the_badge(pg_db):
    pg_db.seed_settings(require_startgg=False)
    checkin = pg_db.seed_checkin(member=False, member_source="ebas_check")
    postgres.apply_integration_result(
        checkin["record_id"],
        "ebas",
        False,
        {},
        state="unavailable",
        error={"code": "provider_timeout"},
    )
    row = postgres.get_checkins(checkin["event_slug"])[0]
    assert (row["member"], row["member_lookup_state"], row["member_lookup_reported"]) == (
        False,
        "unknown",
        True,
    )
    state = badge.membership_state(
        row["member"], row["member_lookup_state"], row["member_lookup_reported"]
    )
    assert state == badge.EBAS_UNKNOWN

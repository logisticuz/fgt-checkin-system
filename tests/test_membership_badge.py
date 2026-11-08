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
    assert [rid for rid, row in rows.items() if row.get("member_lookup")] == ["ebas-down"]


def test_needs_attention_knows_even_when_the_member_column_is_hidden(dashboard):  # noqa: F811
    rows = table(dashboard, REQ, columns=("name", "tag", "status"))
    assert "member" not in rows["ebas-down"]
    assert rows["ebas-down"]["member_lookup"] == badge.EBAS_UNKNOWN


def test_no_badge_when_membership_is_not_required(dashboard):  # noqa: F811
    rows = table(dashboard, {"require_membership": False})
    assert all("member_lookup" not in row and "member" not in row for row in rows.values())


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

"""T-068 dashboard callbacks for live duplicate hints (storage mocked, synthetic data)."""

import json
from unittest.mock import Mock

import pytest
from dash import no_update
from dash._callback_context import context_value
from dash._utils import AttributeDict
from dash.exceptions import PreventUpdate

from tests.test_dashboard_row_identity import dashboard  # noqa: F401  (pytest fixture)

HINT = {
    "candidate_uuid": "uuid-kenji",
    "candidate_tag": "pw-kenji",
    "candidate_name": "Synthetic Kenji",
    "reasons": ["similar_tag", "exact_name"],
    "confidence": "medium",
}
ROW = {
    "record_id": "rec-1",
    "name": "Synthetic Kenji",
    "tag": "pw-kenjj",
    "status": "Ready",
    "member": "✓",
    "startgg": "✓",
}
PAIR = {
    "record_id": "rec-1",
    "event_slug": "live-event",
    "version": "v1",
    "checkin": {
        "name": "Synthetic Kenji",
        "tag": "pw-kenjj",
        "telephone": "0700000399",
        "total_events": 0,
        "has_profile": False,
        "today": True,
    },
    "candidate": {
        "uuid": "uuid-kenji",
        "name": "Synthetic Kenji",
        "tag": "pw-kenji",
        "telephone": "0700000301",
        "total_events": 7,
        "today": False,
    },
    "reasons": ["similar_tag", "exact_name"],
    "confidence": "medium",
}
REQ = {"require_membership": True, "require_startgg": True, "require_payment": False}


def click(prop_id, value=1, outputs=None):
    context_value.set(
        AttributeDict(
            triggered_inputs=[{"prop_id": prop_id, "value": value}], outputs_list=outputs or []
        )
    )


def open_id(record="rec-1", cand="uuid-kenji"):
    key = {"type": "live-dup-open", "record": record, "cand": cand}
    return json.dumps(key, sort_keys=True, separators=(",", ":")) + ".n_clicks"


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


@pytest.fixture
def ui(dashboard, monkeypatch):  # noqa: F811
    store = dashboard.storage
    store.get_live_duplicate_pair = Mock(return_value=PAIR)
    store.resolve_live_duplicate = Mock(
        return_value={
            "merge_id": 9,
            "keep_player_tag": "pw-kenji",
            "keep_uuid": "uuid-kenji",
            "event_slug": "live-event",
        }
    )
    store.get_live_duplicate_hints = Mock(return_value={"rec-1": [HINT]})
    store.detect_live_duplicate_hints = Mock(return_value=1)
    store.get_merge_history = Mock(return_value=[])
    store.stale_duplicate_pairs = Mock(return_value=[])
    module = dashboard.callbacks["open_live_duplicate"].__globals__
    monkeypatch.setitem(
        module,
        "current_user",
        lambda: {"user_id": "42", "user_name": "Synthetic TO", "email": "", "is_dev": False},
    )
    return dashboard


def test_needs_attention_lists_the_duplicate_as_a_soft_item(ui):
    items, count = ui.callbacks["update_needs_attention"]([ROW], REQ, {"rec-1": [HINT]})
    assert count == "1"  # the player is Ready: only the duplicate item is listed
    assert "Possible duplicate: pw-kenjj ↔ pw-kenji" in text_of(items)
    assert "similar tag, same name" in text_of(items)
    assert {"type": "live-dup-open", "record": "rec-1", "cand": "uuid-kenji"} in ids_in(items)
    assert "Mark paid" not in text_of(items)  # T-001 rule stays


def test_duplicate_items_come_after_missing_requirements(ui):
    pending = {**ROW, "startgg": "✗", "status": "Pending"}
    items, count = ui.callbacks["update_needs_attention"]([pending], REQ, {"rec-1": [HINT]})
    assert count == "2"
    assert "START.GG" in text_of(items[0]) and "Possible duplicate" in text_of(items[-1])


def test_section_shows_for_a_hint_but_the_counters_ignore_it(ui):
    total, ready, pending, attention, style = ui.callbacks["update_stats"](
        [ROW], REQ, {"rec-1": [HINT]}
    )
    assert style["display"] == "block"
    assert (ready, attention) == ("1", "0")  # never a requirement, never affects Ready
    *_, hidden = ui.callbacks["update_stats"]([ROW], REQ, {})
    assert hidden == {"display": "none"}


def test_dialog_shows_both_profiles_with_masked_phones(ui):
    click(open_id())
    style, body, keep_a, keep_b, target, feedback = ui.callbacks["open_live_duplicate"]([1])
    assert style == {"display": "block"}
    assert (keep_a, keep_b) == ("Keep pw-kenjj", "Keep pw-kenji")
    assert target == {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"}
    text = text_of(body)
    for expected in (
        "today's check-in",
        "existing profile",
        "Events: 7",
        "Checked in today: yes",
        "Checked in today: no",
        "similar tag, same name",
    ):
        assert expected in text, expected
    assert "0700000399" not in text and "0700000301" not in text
    assert "07••••••99" in text
    ui.storage.resolve_live_duplicate.assert_not_called()  # opening changes nothing


def test_dialog_for_a_stale_hint_says_so(ui):
    ui.storage.get_live_duplicate_pair.return_value = None
    click(open_id())
    style, *_, target, feedback = ui.callbacks["open_live_duplicate"]([1])
    assert style == {"display": "none"} and target is None
    assert "no longer current" in text_of(feedback)


def test_rendering_new_items_is_not_a_click(ui):
    click(open_id(), value=0)
    with pytest.raises(PreventUpdate):
        ui.callbacks["open_live_duplicate"]([0])


@pytest.mark.parametrize(
    "button, action",
    [
        ("btn-live-dup-keep-a", "keep_checkin"),
        ("btn-live-dup-keep-b", "keep_candidate"),
        ("btn-live-dup-not-same", "not_same"),
    ],
)
def test_dialog_buttons_use_the_existing_tools(ui, button, action):
    click(f"{button}.n_clicks")
    target = {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"}
    style, stored, feedback, trigger, body, _, _, history = ui.callbacks[
        "resolve_live_duplicate_choice"
    ](1, 1, 1, 0, target)
    args = ui.storage.resolve_live_duplicate.call_args
    assert args.args == ("rec-1", "uuid-kenji", action)
    assert args.kwargs["user"]["user_name"] == "Synthetic TO"
    assert args.kwargs["version"] == "v1"  # bound to what the dialog showed
    assert style == {"display": "none"} and stored is None and body is no_update
    assert isinstance(trigger, float)  # refreshes the live view (and so the item disappears)
    if action == "not_same":
        assert text_of(feedback) == "Marked as different players."
        assert history is no_update
    else:
        assert "Undo in Insights" in text_of(feedback)
        assert "No merges yet." in text_of(history)  # the merge history is reloaded
    # Every connected dashboard refreshes: a broadcast without candidate or contact data.
    payload = ui.post.call_args.kwargs["json"]
    assert payload == {"type": "duplicates_changed", "event_slug": "live-event"}


def test_cancel_changes_nothing(ui):
    click("btn-live-dup-cancel.n_clicks")
    style, stored, feedback, trigger, *_ = ui.callbacks["resolve_live_duplicate_choice"](
        0, 0, 0, 1, {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"}
    )
    ui.storage.resolve_live_duplicate.assert_not_called()
    assert style == {"display": "none"} and trigger is no_update
    ui.post.assert_not_called()


def test_a_gone_pair_reports_nothing_changed(ui):
    ui.storage.resolve_live_duplicate.side_effect = ValueError("no longer current")
    ui.storage.get_live_duplicate_pair.return_value = None
    click("btn-live-dup-keep-b.n_clicks")
    style, target, feedback, *_ = ui.callbacks["resolve_live_duplicate_choice"](
        0, 1, 0, 0, {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"}
    )
    assert style == {"display": "none"} and target is None
    assert "Nothing changed: no longer current" in text_of(feedback)
    ui.post.assert_not_called()


def test_a_changed_pair_reopens_the_dialog_with_what_it_is_now(ui):
    """Review P1#1: the server refused (nothing written); the TO sees the fresh pair."""
    ui.storage.resolve_live_duplicate.side_effect = ValueError("Something changed")
    fresh = {**PAIR, "version": "v2", "candidate": {**PAIR["candidate"], "tag": "pw-kenji2"}}
    ui.storage.get_live_duplicate_pair.return_value = fresh
    click("btn-live-dup-keep-b.n_clicks")
    style, target, _, _, body, keep_a, keep_b, history = ui.callbacks[
        "resolve_live_duplicate_choice"
    ](0, 1, 0, 0, {"record_id": "rec-1", "candidate_uuid": "uuid-kenji", "version": "v1"})
    assert style == {"display": "block"}
    assert target["version"] == "v2" and keep_b == "Keep pw-kenji2"
    assert "Nothing changed: Something changed" in text_of(body)
    assert "live-dup-changed" in ids_in(body)
    assert history is no_update
    ui.post.assert_not_called()


def test_live_table_marks_rows_only_for_the_active_event(ui):
    ui.storage.get_checkins.return_value = [ROW, {**ROW, "record_id": "rec-2", "tag": "pw-other"}]
    ui.storage.get_active_settings.return_value = {"active_event_slug": "live-event"}
    out = ui.callbacks["update_table"](
        "live-event", 0, 0, 0, ["name", "tag", "status"], "all", "", None, 0, REQ
    )
    rows, cols, store = out[0], out[1], out[-1]
    assert store == {"rec-1": [HINT]}
    assert [r.get("dup_hint") for r in rows] == ["⚠️ pw-kenji", None]
    assert cols[-1]["id"] == "dup_hint"

    ui.storage.detect_live_duplicate_hints.assert_not_called()  # never on the table path

    ui.storage.get_live_duplicate_hints.reset_mock()
    out = ui.callbacks["update_table"](
        "archived-event", 0, 0, 0, ["name", "tag", "status"], "all", "", None, 0, REQ
    )
    ui.storage.get_live_duplicate_hints.assert_not_called()
    assert out[-1] == {} and all("dup_hint" not in r for r in out[0])


def test_a_hint_failure_never_breaks_the_live_table(ui):
    ui.storage.get_checkins.return_value = [ROW]
    ui.storage.get_active_settings.return_value = {"active_event_slug": "live-event"}
    ui.storage.get_live_duplicate_hints.side_effect = RuntimeError("db hiccup")
    out = ui.callbacks["update_table"](
        "live-event", 0, 0, 0, ["name", "tag", "status"], "all", "", None, 0, REQ
    )
    assert [r["record_id"] for r in out[0]] == ["rec-1"] and out[-1] == {}


def test_detection_runs_in_its_own_callback_and_only_reports_changes(ui):
    ui.storage.get_active_settings.return_value = {"active_event_slug": "live-event"}
    detect = ui.callbacks["detect_live_duplicates"]
    assert isinstance(detect("live-event", 1, 0, 0), float)  # changed → table re-renders
    ui.storage.detect_live_duplicate_hints.assert_called_once_with("live-event")
    ui.storage.detect_live_duplicate_hints.return_value = 0
    with pytest.raises(PreventUpdate):  # nothing changed → no extra table render
        detect("live-event", 2, 0, 0)
    ui.storage.detect_live_duplicate_hints.reset_mock()
    with pytest.raises(PreventUpdate):  # browsing an archived event: nothing to detect
        detect("archived-event", 3, 0, 0)
    ui.storage.detect_live_duplicate_hints.assert_not_called()
    ui.storage.detect_live_duplicate_hints.side_effect = RuntimeError("db hiccup")
    with pytest.raises(PreventUpdate):  # a failure is swallowed
        detect("live-event", 4, 0, 0)


def test_merge_history_loads_on_any_refresh_with_undo(ui):
    ui.storage.get_merge_history.return_value = [
        {
            "id": 9,
            "merged_at": "2026-11-07T20:00:00",
            "keep_uuid": "uuid-kenji",
            "removed_tag": "pw-kenjj",
            "removed_name": "Synthetic Kenji",
            "archive_rows_updated": 0,
            "undone": False,
        }
    ]
    history = ui.callbacks["load_merge_history"](1, "tab-insights")
    assert "Removed pw-kenjj" in text_of(history)
    assert {"type": "btn-undo-merge", "merge_id": 9} in ids_in(history)


def test_cards_resolved_elsewhere_are_hidden_and_others_untouched(ui):
    cards = [
        {"id": {"type": "dup-card", "a": "u1", "b": "u2"}, "property": "style"},
        {"id": {"type": "dup-card", "a": "u3", "b": "u4"}, "property": "style"},
    ]
    ui.storage.stale_duplicate_pairs.return_value = [["u3", "u4"]]
    click("live-sync.n_clicks", outputs=cards)
    styles = ui.callbacks["prune_resolved_duplicate_cards"](1)
    assert styles == [no_update, {"display": "none"}]
    ui.storage.stale_duplicate_pairs.assert_called_once_with([["u1", "u2"], ["u3", "u4"]])
    ui.storage.stale_duplicate_pairs.return_value = []
    with pytest.raises(PreventUpdate):
        ui.callbacks["prune_resolved_duplicate_cards"](2)


def test_insights_merge_and_undo_broadcast_too(ui):
    ui.storage.merge_players = Mock(return_value={"keep_player_tag": "pw-a"})
    ui.storage.undo_merge = Mock(return_value={"restored_tag": "pw-b"})
    ui.storage.is_duplicate_pair_dismissed = Mock(return_value=False)
    ui.storage.find_duplicate_candidates = Mock(return_value=[])
    ui.storage.count_duplicate_dismissals = Mock(return_value=0)
    ui.storage.get_duplicate_dismissals = Mock(return_value=[])
    ui.callbacks["execute_merge"](1, "uuid-a", "uuid-b")
    assert ui.post.call_args.kwargs["json"]["type"] == "duplicates_changed"
    ui.post.reset_mock()
    undo = json.dumps({"merge_id": 9, "type": "btn-undo-merge"}, separators=(",", ":"))
    click(undo + ".n_clicks")
    ui.callbacks["undo_merge_callback"]([1])
    assert ui.post.call_args.kwargs["json"]["type"] == "duplicates_changed"

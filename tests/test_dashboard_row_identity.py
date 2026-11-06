"""Exercise the registered dashboard callbacks with synthetic rows and no external I/O."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from dash import Dash, no_update


def make_row(number):
    return {
        "id": f"record-{number}",
        "record_id": f"record-{number}",
        "name": f"Player {number}",
        "tag": f"tag-{number}",
        "member": "✗",
        "payment_valid": "✗",
        "startgg": "✗",
        "status": "Pending",
    }


@pytest.fixture
def dashboard(monkeypatch):
    # Replace storage before importing callbacks so no real database is loaded or queried.
    import shared

    storage = ModuleType("shared.storage")
    for name in (
        "get_checkins",
        "get_active_settings",
        "get_active_slug",
        "get_active_settings_with_id",
        "update_settings",
        "update_checkin",
        "delete_checkin",
        "get_audit_log",
        "log_action",
    ):
        setattr(storage, name, Mock())
    storage.get_active_settings.return_value = {}
    storage.update_checkin.return_value = True
    storage.delete_checkin.return_value = True
    monkeypatch.setitem(sys.modules, "shared.storage", storage)
    monkeypatch.setattr(shared, "storage", storage, raising=False)
    monkeypatch.setenv("INTEGRATION_ENGINE", "none")
    monkeypatch.setenv("STARTGG_API_KEY", "")
    monkeypatch.setenv("STARTGG_TOKEN", "")

    path = Path(__file__).resolve().parents[1] / "fgt_dashboard" / "callbacks.py"
    spec = importlib.util.spec_from_file_location("row_identity_callbacks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    post = Mock(
        return_value=SimpleNamespace(
            status_code=200,
            json=lambda: {"startgg": True, "events": ["Test Game"], "status": "Ready"},
        )
    )
    monkeypatch.setattr(module.requests, "post", post)

    app = Dash(__name__)
    module.register_callbacks(app)
    handlers = {}
    definitions = {}
    for definition in app.callback_map.values():
        if "callback" in definition:
            callback = definition["callback"].__wrapped__
            handlers[callback.__name__] = callback
            definitions[callback.__name__] = definition
    return SimpleNamespace(callbacks=handlers, definitions=definitions, storage=storage, post=post)


@pytest.mark.parametrize("column", ["payment_valid", "startgg", "member"])
@pytest.mark.parametrize("target_index", [2, 20], ids=["sorted", "page-two"])
def test_toggle_uses_clicked_identity_instead_of_displayed_position(
    dashboard, column, target_index
):
    rows = [make_row(number) for number in range(25)]
    target_id = rows[target_index]["id"]
    active_cell = {"row": 0, "row_id": target_id, "column_id": column}

    _, updated, active = dashboard.callbacks["toggle_field"](active_cell, rows, "test-event", {})

    dashboard.storage.update_checkin.assert_called_once()
    assert dashboard.storage.update_checkin.call_args.args[0] == target_id
    assert updated[target_index][column] == "✓"
    assert updated[0][column] == "✗"
    assert active is None


@pytest.mark.parametrize("row_id", [None, "vanished-record"])
def test_toggle_never_falls_back_to_position_for_missing_identity(dashboard, row_id):
    feedback, updated, active = dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": row_id, "column_id": "member"},
        [make_row(0)],
        "test-event",
        {},
    )
    assert "no longer" in feedback.children
    assert updated is no_update
    assert active is None
    dashboard.storage.update_checkin.assert_not_called()
    dashboard.post.assert_not_called()


def test_reset_prevents_refresh_retrigger_and_allows_same_cell_twice(dashboard):
    toggle = dashboard.callbacks["toggle_field"]
    rows = [make_row(0)]
    cell = {"row": 0, "row_id": "record-0", "column_id": "member"}

    _, updated, active = toggle(cell, rows, "test-event", {})
    assert active is None
    assert toggle(active, updated, "test-event", {}) == (no_update, no_update, no_update)
    assert dashboard.storage.update_checkin.call_count == 1

    _, updated, active = toggle(cell, updated, "test-event", {})
    assert active is None
    assert updated[0]["member"] == "✗"
    assert dashboard.storage.update_checkin.call_count == 2


def test_failed_toggle_also_resets_active_cell(dashboard):
    dashboard.storage.update_checkin.return_value = False
    _, updated, active = dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": "record-0", "column_id": "member"},
        [make_row(0)],
        "test-event",
        {},
    )
    assert updated is no_update
    assert active is None


def test_non_toggle_cell_keeps_editing_selection(dashboard):
    assert dashboard.callbacks["toggle_field"](
        {"row": 0, "row_id": "record-0", "column_id": "name"},
        [make_row(0)],
        "test-event",
        {},
    ) == (no_update, no_update, no_update)
    dashboard.storage.update_checkin.assert_not_called()


def test_delete_keeps_confirmed_ids_when_new_checkin_changes_positions(dashboard):
    rows = [make_row(number) for number in range(4)]
    selected = ["record-3", "record-1"]
    displayed, message, _, captured = dashboard.callbacks["show_delete_confirmation"](
        1, selected, rows
    )
    assert displayed is True
    assert "Player 3" in message and "Player 1" in message

    # A refresh inserts a row and the UI selection changes while the dialog is open.
    refreshed = [make_row(99), *reversed(rows)]
    selected[:] = ["record-99"]
    _, remaining, pending = dashboard.callbacks["delete_selected_player"](
        1, captured, refreshed, "test-event", {}
    )

    assert [call.args[0] for call in dashboard.storage.delete_checkin.call_args_list] == [
        "record-3",
        "record-1",
    ]
    assert [row["id"] for row in remaining] == ["record-99", "record-2", "record-0"]
    assert pending == []


def test_recheck_keeps_confirmed_id_after_refresh(dashboard):
    rows = [make_row(0), make_row(1)]
    selected = ["record-1"]
    displayed, message, captured, _ = dashboard.callbacks["show_recheck_confirmation"](
        1, selected, rows
    )
    assert displayed is True
    assert "Player 1" in message
    selected[:] = ["record-99"]

    _, pending = dashboard.callbacks["recheck_startgg"](
        1, captured, [make_row(99), *reversed(rows)], "test-event", {}
    )

    dashboard.post.assert_called_once_with(
        "http://backend:8000/api/admin/recheck-startgg",
        json={"record_id": "record-1"},
        timeout=20,
    )
    assert pending == []


@pytest.mark.parametrize("action", ["delete", "recheck"])
def test_unknown_selected_id_does_not_open_dialog(dashboard, action):
    result = dashboard.callbacks[f"show_{action}_confirmation"](
        1, ["vanished-record"], [make_row(0)]
    )
    assert result[0] is False
    assert result[3 if action == "delete" else 2] == []
    dashboard.storage.delete_checkin.assert_not_called()
    dashboard.post.assert_not_called()


@pytest.mark.parametrize("action", ["delete", "recheck"])
@pytest.mark.parametrize("refreshed", [[], [make_row(99)]], ids=["empty", "replaced"])
def test_vanished_id_at_confirmation_is_a_noop(dashboard, action, refreshed):
    opened = dashboard.callbacks[f"show_{action}_confirmation"](1, ["record-0"], [make_row(0)])
    captured = opened[3 if action == "delete" else 2]
    handler = "delete_selected_player" if action == "delete" else "recheck_startgg"
    result = dashboard.callbacks[handler](1, captured, refreshed, "test-event", {})

    assert "no longer" in result[0].children
    assert result[-1] == []
    dashboard.storage.delete_checkin.assert_not_called()
    dashboard.post.assert_not_called()


def test_delete_skips_vanished_id_and_deletes_only_remaining_confirmed_id(dashboard):
    _, _, _, captured = dashboard.callbacks["show_delete_confirmation"](
        1, ["record-0", "record-1"], [make_row(0), make_row(1)]
    )
    feedback, remaining, _ = dashboard.callbacks["delete_selected_player"](
        1, captured, [make_row(99), make_row(1)], "test-event", {}
    )
    dashboard.storage.delete_checkin.assert_called_once_with("record-1")
    assert [row["id"] for row in remaining] == ["record-99"]
    assert "Failed: 1" in feedback.children


@pytest.mark.parametrize("action", ["delete", "recheck"])
def test_callbacks_wire_selection_to_capture_and_confirmation_to_store(dashboard, action):
    opening = dashboard.definitions[f"show_{action}_confirmation"]
    assert {"id": "checkins-table", "property": "selected_row_ids"} in opening["state"]
    handler = "delete_selected_player" if action == "delete" else "recheck_startgg"
    confirming = dashboard.definitions[handler]
    assert confirming["inputs"] == [
        {"id": f"confirm-{action}-dialog", "property": "submit_n_clicks"}
    ]
    assert {"id": f"pending-{action}-row-ids", "property": "data"} in confirming["state"]
    assert all(state["property"] != "selected_rows" for state in confirming["state"])
    assert all(state["property"] != "selected_row_ids" for state in confirming["state"])


def test_table_refresh_assigns_record_identity_after_filtering(dashboard):
    dashboard.storage.get_checkins.return_value = [
        {key: value for key, value in make_row(number).items() if key != "id"}
        for number in range(3)
    ]
    result = dashboard.callbacks["update_table"](
        "test-event", 0, 0, None, ["name", "member"], "all", "Player 2", None, {}
    )
    rows, columns = result[:2]
    assert [row["id"] for row in rows] == ["record-2"]
    assert rows[0]["id"] == rows[0]["record_id"]
    assert "id" not in [column["id"] for column in columns]

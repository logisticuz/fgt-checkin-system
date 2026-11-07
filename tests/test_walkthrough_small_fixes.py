"""T-063 walkthrough regressions; synthetic storage and offline app clients only."""

import ast
import asyncio
import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from shared import postgres_api as postgres
from tests.test_auth_callback_and_docs import load_application  # noqa: F401
from tests.test_dashboard_row_identity import dashboard  # noqa: F401
from tests.test_safe_archiving import seed_archive
from tests.test_status_pages_by_id import render
from tests.test_status_templates import app_templates  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
OLD = "test-series-4"
NEW = "test-series-5"
OLD_NAME = "Synthetic Series #4"


@pytest.mark.parametrize(
    "capability", [{}, {"checkin_id": "test-id", "status_token": "test-token"}]
)
def test_ready_never_requests_or_warns_about_a_missing_sound(app_templates, capability):
    page = render(app_templates, "status_ready.html", **capability)
    assert "clear.mp3" not in page
    assert "<audio" not in page and "audioEl" not in page
    assert "Could not play confirmation sound" not in page
    assert "Good luck!" in page


@pytest.mark.parametrize(
    ("patch", "display", "tournament"),
    [
        ({"active_event_slug": NEW}, None, None),
        ({"active_event_slug": None}, None, None),
        ({"active_event_slug": OLD}, OLD_NAME, OLD_NAME),
        ({"require_payment": True}, OLD_NAME, OLD_NAME),
        ({"active_event_slug": NEW, "event_display_name": "New Series"}, "New Series", None),
        (
            {
                "active_event_slug": NEW,
                "event_display_name": "New Series",
                "tournament_name": "New Series",
            },
            "New Series",
            "New Series",
        ),
    ],
)
def test_settings_change_names_with_the_slug_in_one_write(pg_db, patch, display, tournament):
    row = pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name=OLD_NAME
    )
    original_patch = patch.copy()
    result = postgres.update_settings(str(row["id"]), patch)
    assert result["fields"]["event_display_name"] == display
    assert result["fields"]["tournament_name"] == tournament
    assert patch == original_patch


def _dashboard_event_dropdown(settings, monkeypatch):
    storage = sys.modules["shared.storage"]
    for name, replacement in {
        "get_active_settings": lambda: settings,
        "get_active_slug": lambda: settings["active_event_slug"],
        "get_all_event_slugs": lambda: [settings["active_event_slug"]],
        "get_checkins": lambda *args, **kwargs: [],
        "get_event_history": lambda: [],
        "get_session": lambda *args: None,
    }.items():
        monkeypatch.setattr(storage, name, replacement, raising=False)
    spec = importlib.util.spec_from_file_location(
        "t063_layout", ROOT / "fgt_dashboard" / "layout.py"
    )
    layout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layout)
    monkeypatch.setattr(layout, "_get_auth_state", lambda: {})

    pending = [layout.create_layout()]
    while pending:
        component = pending.pop()
        if getattr(component, "id", None) == "event-dropdown":
            return component
        children = getattr(component, "children", [])
        pending.extend(children if isinstance(children, (list, tuple)) else [children])
    raise AssertionError("Current Event dropdown was not rendered")


@pytest.mark.parametrize(
    "selected_slug", [NEW, OLD], ids=["new-event", "repair-existing-selection"]
)
def test_auth_selection_clears_stale_name_for_ready_page_and_dashboard(
    pg_db, load_application, monkeypatch, selected_slug
):
    pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name=OLD_NAME
    )
    api, auth = load_application("dashboard")
    monkeypatch.setattr(api, "get_all_event_slugs", lambda: [selected_slug])
    monkeypatch.setattr(api, "get_active_settings_with_id", postgres.get_active_settings_with_id)
    monkeypatch.setattr(api, "update_settings", postgres.update_settings)
    monkeypatch.setattr(api, "reopen_event", Mock(side_effect=ValueError("No archived data")))
    auth.check_event_admin.return_value = (True, "synthetic allowed event")
    client = TestClient(api.app, follow_redirects=False)
    client.cookies.set(api.SESSION_COOKIE_NAME, "synthetic-session")
    response = client.post("/auth/select-event/save", data={"event_slug": selected_slug})
    assert response.status_code == 302
    settings = postgres.get_active_settings()
    assert settings["active_event_slug"] == selected_slug
    assert settings["event_display_name"] == settings["tournament_name"] == ""

    dropdown = _dashboard_event_dropdown(settings, monkeypatch)
    expected_name = selected_slug.replace("-", " ").title()
    assert dropdown.value == selected_slug
    selected_option = next(
        option for option in dropdown.options if option["value"] == selected_slug
    )
    assert selected_option["label"] == expected_name
    assert OLD_NAME not in [option["label"] for option in dropdown.options]

    monkeypatch.chdir(ROOT / "backend")
    from backend.routers import pages

    monkeypatch.setattr(pages, "status_context", lambda request: {})
    monkeypatch.setattr(pages, "get_active_settings", lambda: settings)
    monkeypatch.setattr(pages, "check_participant_status", lambda name: {})
    monkeypatch.setattr(pages, "get_participant_details", lambda name: {"games": []})
    monkeypatch.setattr(pages, "compute_ready_and_missing", lambda *args: (True, []))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/status/Synthetic",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    ready = asyncio.run(pages.status_view(request, "Synthetic"))
    assert ready.context["event_slug"] == selected_slug
    assert ready.context["event_name"] == pages.slug_to_display_name(selected_slug)
    assert OLD_NAME not in ready.body.decode()


@pytest.mark.parametrize("archive_name", [None, "", "Synthetic Series #5"])
def test_reopen_replaces_or_clears_the_previous_event_name(pg_db, archive_name):
    pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name=OLD_NAME
    )
    pg_db.insert(
        "event_stats",
        {"event_slug": NEW, "event_date": "2026-11-07", "event_display_name": archive_name},
    )
    postgres.reopen_event(NEW, restore_active=False)
    settings = postgres.get_active_settings()
    assert settings["active_event_slug"] == NEW
    assert settings["event_display_name"] == settings["tournament_name"] == (archive_name or None)


def test_reopen_without_stats_clears_names_instead_of_inheriting_them(pg_db):
    pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name=OLD_NAME
    )
    seed_archive(pg_db, event_slug=NEW)
    postgres.reopen_event(NEW, restore_active=False)
    settings = postgres.get_active_settings()
    assert settings["active_event_slug"] == NEW
    assert settings["event_display_name"] is settings["tournament_name"] is None


def test_archive_clear_clears_the_name_with_the_active_slug(pg_db):
    pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name=OLD_NAME
    )
    pg_db.seed_checkin(event_slug=OLD)
    postgres.archive_event(
        OLD,
        event_date="2026-11-07",
        event_display_name=OLD_NAME,
        startgg_snapshot={},
        clear_active=True,
    )
    settings = postgres.get_active_settings()
    assert settings["active_event_slug"] is None
    assert settings["event_display_name"] is settings["tournament_name"] is None


@pytest.mark.parametrize("failed_run", [False, True], ids=["successful-smoke", "failed-smoke"])
def test_acceptance_helper_restores_its_complete_settings_snapshot(pg_db, failed_run):
    from scripts.acceptance_smoke import DEV_HELPER

    row = pg_db.seed_settings(
        active_event_slug=OLD, event_display_name=OLD_NAME, tournament_name="Original Tournament"
    )
    original = postgres.get_active_settings_with_id()["fields"]
    tree = ast.parse(DEV_HELPER)
    assignments = {
        node.targets[0].id: node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"changes", "snapshot"}
    }
    cleanup = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "cleanup_and_restore"
    )
    namespace = {
        "slug": "acceptance-20261107123456-123456abcdef",
        "current": {"fields": original},
        "settings_id": str(row["id"]),
        "storage": postgres,
        "guard": lambda: None,
    }
    actual_helper = ast.Module(
        body=[assignments["changes"], assignments["snapshot"], cleanup], type_ignores=[]
    )
    exec(compile(actual_helper, "<actual-acceptance-settings-helper>", "exec"), namespace)
    assert namespace["snapshot"]["event_display_name"] == OLD_NAME
    assert namespace["snapshot"]["tournament_name"] == "Original Tournament"
    outcome = (
        pytest.raises(RuntimeError, match="Synthetic smoke failure")
        if failed_run
        else nullcontext()
    )
    with pg_db.pool.connection() as connection, outcome:
        try:
            postgres.update_settings(str(row["id"]), namespace["changes"])
            during_smoke = postgres.get_active_settings()
            assert (
                during_smoke["event_display_name"]
                == during_smoke["tournament_name"]
                == "Acceptance Smoke"
            )
            if failed_run:
                raise RuntimeError("Synthetic smoke failure")
        finally:
            assert namespace["cleanup_and_restore"](connection) is True
    assert postgres.get_active_settings_with_id()["fields"] == original


@pytest.mark.parametrize("dates", [["2026-11-07"], ["2026-11-07", "2026-11-07"]])
def test_single_date_trend_uses_a_category_axis_without_millisecond_ticks(dashboard, dates):
    rows = [
        {"event_date": day, "event_display_name": f"Synthetic {index}", "total_participants": 12}
        for index, day in enumerate(dates)
    ]
    figure = dashboard.callbacks["render_events_noshow_trend"](rows)
    assert figure["layout"]["xaxis"]["type"] == "category"
    assert len(figure["data"]) == 2
    assert all(trace["x"] == dates for trace in figure["data"])
    assert all("markers" in trace["mode"] for trace in figure["data"])


def test_multiple_date_trend_keeps_chronological_date_axis(dashboard):
    figure = dashboard.callbacks["render_events_noshow_trend"](
        [{"event_date": "2026-11-07"}, {"event_date": "2026-10-01"}]
    )
    assert figure["layout"]["xaxis"]["type"] == "date"
    assert figure["data"][0]["x"] == ["2026-10-01", "2026-11-07"]


def test_insights_event_overview_is_newest_first_with_undated_events_last(dashboard):
    events = [
        {"event_slug": "test-september", "event_date": "2026-09-10"},
        {"event_slug": "test-november", "event_date": "2026-11-07"},
        {"event_slug": "test-undated", "event_date": None},
        {"event_slug": "test-august", "event_date": "2026-08-01"},
        {"event_slug": "test-invalid-date", "event_date": "unknown"},
    ]
    dashboard.storage.get_event_history_dashboard = Mock(return_value=events)
    callback = dashboard.callbacks["update_insights"]
    result = callback("tab-insights", 0, [], "all", [], 15, "all", "", "", "")
    outputs = dashboard.definitions["update_insights"]["output"]
    values = {output.component_id: value for output, value in zip(outputs, result)}
    rows = values["insights-events-table"]
    assert [row["event_slug"] for row in rows[:3]] == [
        "test-november",
        "test-september",
        "test-august",
    ]
    assert {row["event_slug"] for row in rows[3:]} == {"test-undated", "test-invalid-date"}

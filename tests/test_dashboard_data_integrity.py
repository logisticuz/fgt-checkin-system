"""Real dashboard callbacks keep canonical games and produce safe literal data."""

import csv
import io

import pandas as pd
import pytest
from dash import no_update

# Reuse T-001's isolated real-callback setup; it mocks storage and HTTP before import.
from tests.test_dashboard_row_identity import dashboard, make_row


def table_result(dashboard, rows, search=None, game_filter=None):
    dashboard.storage.get_checkins.return_value = rows
    return dashboard.callbacks["update_table"](
        "test-event",
        0,
        0,
        None,
        ["name", "tag", "telephone", "tournament_games_registered"],
        "all",
        search,
        game_filter,
        0,  # live-dup-version (T-068)
        {},
    )


def test_games_column_is_read_only_while_contact_columns_remain_editable(dashboard):
    row = {**make_row(0), "telephone": "0000", "tournament_games_registered": ["Street Fighter 6"]}
    rows, columns = table_result(dashboard, [row])[:2]
    by_id = {column["id"]: column for column in columns}
    assert rows[0]["tournament_games_registered"] == "SF6"
    assert by_id["tournament_games_registered"]["editable"] is False
    for field in ("name", "tag", "telephone"):
        assert by_id[field]["editable"] is True


def test_games_edit_never_reaches_storage(dashboard):
    previous = {**make_row(0), "tournament_games_registered": "Street Fighter 6"}
    current = {**previous, "tournament_games_registered": "SF6"}
    result = dashboard.callbacks["save_tag_edits"](
        1,
        [current],
        [previous],
        {"row": 0, "row_id": "record-0", "column_id": "tournament_games_registered"},
        "test-event",
        {},
    )
    assert result == (no_update, no_update)
    dashboard.storage.update_checkin.assert_not_called()


@pytest.mark.parametrize(
    "field, value", [("name", "Updated Player"), ("tag", "updated-tag"), ("telephone", "0000")]
)
def test_contact_edit_cannot_write_changed_games_from_table_data(dashboard, field, value):
    previous = {**make_row(0), "telephone": "", "tournament_games_registered": "Street Fighter 6"}
    current = {**previous, field: value, "tournament_games_registered": "SF6"}
    dashboard.callbacks["save_tag_edits"](
        1,
        [current],
        [previous],
        {"row": 0, "row_id": "record-0", "column_id": field},
        "test-event",
        {},
    )
    dashboard.storage.update_checkin.assert_called_once_with(
        "record-0", {field: value}, typecast=True
    )


@pytest.mark.parametrize("query", ["(", "[", ".*", "\\"])
@pytest.mark.parametrize("field", ["name", "tag"])
def test_name_and_tag_search_match_regex_characters_literally(dashboard, query, field):
    matching = {**make_row(0), field: f"Literal {query} match"}
    unmatched = make_row(1)
    rows = table_result(dashboard, [matching, unmatched], search=query)[0]
    assert [row["record_id"] for row in rows] == ["record-0"]


@pytest.mark.parametrize("query", ["(", "[", ".*", "\\"])
def test_game_filter_also_matches_literally(dashboard, query):
    matching = {**make_row(0), "tournament_games_registered": [f"Test {query} Game"]}
    unmatched = {**make_row(1), "tournament_games_registered": ["Ordinary Game"]}
    rows = table_result(dashboard, [matching, unmatched], game_filter=query)[0]
    assert [row["record_id"] for row in rows] == ["record-0"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("=1+1", "'=1+1"),
        ("+x", "'+x"),
        ("-x", "'-x"),
        ("@x", "'@x"),
        ("\t=x", "'\t=x"),
        ("\r=x", "'\r=x"),
        ("Normal value", "Normal value"),
        ("123", "123"),
        ("", ""),
        (42, 42),
        (-5, -5),
        (3.5, 3.5),
        (None, None),
    ],
)
def test_csv_safe_neutralizes_formula_strings_without_mutating_source(dashboard, value, expected):
    csv_safe = dashboard.callbacks["export_insights_csv"].__globals__["_csv_safe"]
    source = pd.DataFrame({"value": [value]})
    original = source.copy(deep=True)
    result = csv_safe(source)
    assert result.iloc[0]["value"] == expected
    pd.testing.assert_frame_equal(source, original)


@pytest.mark.parametrize(
    "subtab, position", [("players", 0), ("games", 1), ("events", 2), ("earnings", 3)]
)
def test_every_insights_export_uses_csv_guard_and_preserves_rows(dashboard, subtab, position):
    datasets = [[], [], [], []]
    datasets[position] = [{"Name": "=1+1", "Amount": -5}, {"Name": "Normal", "Amount": 7}]
    download = dashboard.callbacks["export_insights_csv"](1, subtab, *datasets)
    rows = list(csv.DictReader(io.StringIO(download["content"])))
    assert download["filename"] == f"insights_{subtab}.csv"
    assert rows == [{"Name": "'=1+1", "Amount": "-5"}, {"Name": "Normal", "Amount": "7"}]


def test_guest_export_guards_cells_and_preserves_existing_guest_game_expansion(dashboard):
    data = [
        {"tag": "=1+1", "is_guest": True, "tournament_games_registered": ["Normal Game", "@Game"]},
        {"tag": "+x", "is_guest": True, "tournament_games_registered": ["\t=Game"]},
        {
            "tag": "Excluded Player",
            "is_guest": False,
            "tournament_games_registered": ["Other Game"],
        },
    ]
    download, _ = dashboard.callbacks["export_guests_csv"](1, data)
    rows = list(csv.DictReader(io.StringIO(download["content"])))
    assert len(rows) == 3
    assert {(row["Tag"], row["Game"]) for row in rows} == {
        ("'=1+1", "Normal Game"),
        ("'=1+1", "'@Game"),
        ("'+x", "'\t=Game"),
    }

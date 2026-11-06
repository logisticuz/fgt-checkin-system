"""Game validation, canonical payment status, and migration-backed case-insensitive dedupe."""

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from scripts import report_duplicate_checkins as report
from shared import migrate, postgres_api as postgres
from tests.fixtures import make_checkin

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "shared" / "migrations" / "002_checkin_unique_tag.sql"
ALLOWED = ["Test Game A", "Test Game B"]


@pytest.fixture
def games_client(app_client, pg_db):
    with pg_db.pool.connection() as connection:
        connection.execute("UPDATE settings SET default_game = %s", (ALLOWED,))
    return app_client


@pytest.mark.db
@pytest.mark.parametrize("checkin_id, status", [(None, 400), ("", 400), ("unknown", 404)])
def test_games_requires_an_existing_checkin_id(games_client, pg_db, checkin_id, status):
    row = pg_db.seed_checkin()
    body = {"tag": row["tag"], "slug": row["event_slug"], "games": [ALLOWED[0]]}
    if checkin_id is not None:
        body["checkin_id"] = checkin_id
    response = games_client.patch("/api/player/games", json=body)
    assert response.status_code == status
    assert pg_db.checkins()[0]["tournament_games_registered"] == []


@pytest.mark.db
@pytest.mark.parametrize("game", ["Unconfigured Game", "<img src=x onerror=alert(1)>"])
def test_unknown_game_is_rejected_with_allowed_names_and_no_write(games_client, pg_db, game):
    row = pg_db.seed_checkin(payment_expected=25, tournament_games_registered=[ALLOWED[0]])
    response = games_client.patch(
        "/api/player/games", json={"checkin_id": row["record_id"], "games": [game]}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["allowed_games"] == ALLOWED
    stored = pg_db.checkins()[0]
    assert stored["tournament_games_registered"] == [ALLOWED[0]]
    assert stored["payment_expected"] == 25


@pytest.mark.db
def test_games_uses_stored_identity_and_server_price(games_client, pg_db):
    row = pg_db.seed_checkin()
    response = games_client.patch(
        "/api/player/games",
        json={
            "checkin_id": row["record_id"],
            "tag": "spoofed-tag",
            "slug": "another-event",
            "games": ALLOWED,
            "payment_expected": 1,
        },
    )
    assert response.status_code == 200
    assert response.json()["tag"] == row["tag"]
    assert response.json()["payment_expected"] == 50
    stored = pg_db.checkins()[0]
    assert stored["tournament_games_registered"] == ALLOWED
    assert stored["payment_expected"] == 50


@pytest.mark.db
def test_repeated_games_are_billed_once(games_client, pg_db):
    row = pg_db.seed_checkin()
    response = games_client.patch(
        "/api/player/games",
        json={"checkin_id": row["record_id"], "games": [ALLOWED[0], ALLOWED[0]]},
    )
    assert response.status_code == 200
    assert response.json()["payment_expected"] == 25
    assert pg_db.checkins()[0]["tournament_games_registered"] == [ALLOWED[0]]


@pytest.mark.db
def test_checkin_from_a_different_event_cannot_be_changed(games_client, pg_db):
    row = pg_db.seed_checkin(event_slug="different-test-event")
    response = games_client.patch(
        "/api/player/games", json={"checkin_id": row["record_id"], "games": ALLOWED}
    )
    assert response.status_code == 404
    assert pg_db.checkins()[0]["tournament_games_registered"] == []


@pytest.mark.db
@pytest.mark.parametrize(
    "events", [[{"id": "test-1", "name": ALLOWED[0]}], {"events": [{"name": ALLOWED[0]}]}]
)
def test_settings_event_list_is_used_when_no_default_games(games_client, pg_db, events):
    row = pg_db.seed_checkin()
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE settings SET default_game = NULL, events_json = %s", (Jsonb(events),)
        )
    response = games_client.patch(
        "/api/player/games", json={"checkin_id": row["record_id"], "games": [ALLOWED[0]]}
    )
    assert response.status_code == 200
    assert pg_db.checkins()[0]["payment_expected"] == 25


@pytest.mark.db
def test_active_configured_subset_wins_over_unselected_startgg_games(games_client, pg_db):
    row = pg_db.seed_checkin()
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE settings SET default_game = %s, events_json = %s",
            ([ALLOWED[0]], Jsonb([{"name": game} for game in ALLOWED])),
        )
    response = games_client.patch(
        "/api/player/games", json={"checkin_id": row["record_id"], "games": [ALLOWED[1]]}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["allowed_games"] == [ALLOWED[0]]


@pytest.mark.db
def test_parallel_orchestrate_submissions_with_case_variants_keep_one_row(
    app_client, pg_db, backend_app, monkeypatch
):
    barrier = Barrier(2)
    original_execute = psycopg.Cursor.execute
    lookups, inserts = [], []

    def simultaneous_initial_lookup(cursor, query, params=None, **kwargs):
        result = original_execute(cursor, query, params, **kwargs)
        if isinstance(query, str):
            if query.startswith("SELECT record_id FROM active_event_data WHERE LOWER("):
                lookups.append(params)
                barrier.wait(timeout=5)
            elif "INSERT INTO active_event_data" in query:
                inserts.append(params)
        return result

    monkeypatch.setattr(psycopg.Cursor, "execute", simultaneous_initial_lookup)
    payload = make_checkin(tag="Test-Parallel-Tag")

    def submit(tag):
        async def request():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=backend_app.app), base_url="http://testserver"
            ) as client:
                return await client.post("/api/checkin/orchestrate", json={**payload, "tag": tag})

        return asyncio.run(request())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(submit, payload["tag"]), executor.submit(
            submit, payload["tag"].lower()
        )
        responses = [first.result(timeout=10), second.result(timeout=10)]
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["checkin_id"] for response in responses}) == 1
    assert len(pg_db.checkins()) == 1
    assert len(lookups) == len(inserts) == 2


@pytest.mark.db
def test_conflict_update_preserves_verified_fields_and_uses_canonical_status(pg_db, monkeypatch):
    pg_db.seed_settings(require_payment=True)
    row = pg_db.seed_checkin(
        member=True,
        startgg=True,
        payment_valid=True,
        payment_amount=50,
        payment_expected=50,
        tournament_games_registered=ALLOWED,
        status="Pending",
        acquisition_source="friend",
    )
    original_execute = psycopg.Cursor.execute
    lookups, inserts = [], []

    def force_conflict_lookup(cursor, query, params=None, **kwargs):
        if isinstance(query, str):
            if query.startswith("SELECT record_id FROM active_event_data WHERE LOWER("):
                lookups.append(params)
                return original_execute(
                    cursor, "SELECT record_id FROM active_event_data WHERE false", **kwargs
                )
            if "INSERT INTO active_event_data" in query:
                inserts.append(params)
        return original_execute(cursor, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", force_conflict_lookup)
    result = postgres.begin_checkin(
        row["event_slug"],
        {
            "tag": row["tag"].upper(),
            "name": "Updated Test Player",
            "email": "",
            "telephone": "",
            "member": False,
            "payment_valid": False,
            "payment_amount": 0,
            "tournament_games_registered": [],
        },
    )
    assert result["checkin_id"] == row["record_id"]
    assert len(lookups) == len(inserts) == 1
    stored = pg_db.checkins()[0]
    assert stored["name"] == "Updated Test Player"
    for key in (
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "email",
        "telephone",
        "acquisition_source",
    ):
        assert stored[key] == row[key]
    assert stored["status"] == "Ready"


@pytest.mark.db
def test_status_and_orchestrate_require_explicit_payment_valid(app_client, pg_db, n8n_stub):
    with pg_db.pool.connection() as connection:
        connection.execute("UPDATE settings SET require_payment = true")
    payload = make_checkin()
    row = pg_db.seed_checkin(
        **{**payload, "member": True, "startgg": True, "payment_expected": 25, "payment_amount": 50}
    )
    orchestration = app_client.post("/api/checkin/orchestrate", json=payload)
    status = app_client.get(f"/api/participant/{row['tag']}/status")
    assert orchestration.status_code == status.status_code == 200
    assert orchestration.json()["ready"] is status.json()["ready"] is False
    assert orchestration.json()["missing"] == status.json()["missing"] == ["Payment"]
    postgres.update_checkin(row["record_id"], {"payment_valid": True})
    assert app_client.post("/api/checkin/orchestrate", json=payload).json()["ready"] is True
    assert app_client.get(f"/api/participant/{row['tag']}/status").json()["ready"] is True


@pytest.fixture
def isolated_migration_db(pg_db, tmp_path):
    """A second private schema lets the real migration see pre-index duplicates safely."""
    schema = "fgt_test_unique_" + uuid.uuid4().hex
    (tmp_path / MIGRATION.name).write_text(MIGRATION.read_text(encoding="utf-8"), encoding="utf-8")
    with pg_db.pool.connection() as connection:
        original_path = connection.execute("SHOW search_path").fetchone()[0]
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connection.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
            )
            connection.execute((ROOT / "db" / "init.sql").read_text(encoding="utf-8"))
            yield connection, tmp_path, schema
        finally:
            connection.execute("SELECT set_config('search_path', %s, false)", (original_path,))
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def seed_duplicate(connection, tag="Duplicate-Test-Tag", event="test-event"):
    connection.execute(
        """INSERT INTO active_event_data (event_slug, tag, name, email, telephone)
           VALUES (%s, %s, 'Test Player', 'player@example.invalid', '0000000000')""",
        (event, tag),
    )


@pytest.mark.db
def test_unique_tag_migration_applies_and_allows_null_tags_and_different_events(
    isolated_migration_db,
):
    connection, directory, _ = isolated_migration_db
    seed_duplicate(connection)
    assert migrate.apply(connection, directory) == [MIGRATION.name]
    assert migrate.apply(connection, directory) == []
    with pytest.raises(psycopg.errors.UniqueViolation):
        seed_duplicate(connection, tag="duplicate-test-tag")
    seed_duplicate(connection, event="other-test-event")
    seed_duplicate(connection, tag=None)
    seed_duplicate(connection, tag=None)
    assert report.find_duplicate_tags(connection) == []


@pytest.mark.db
def test_duplicate_migration_fails_visibly_without_changing_rows(isolated_migration_db):
    connection, directory, _ = isolated_migration_db
    seed_duplicate(connection)
    seed_duplicate(connection, tag="duplicate-test-tag")
    before = connection.execute(
        "SELECT record_id, tag FROM active_event_data ORDER BY record_id"
    ).fetchall()
    with pytest.raises(migrate.MigrationError, match="report_duplicate_checkins.py"):
        migrate.apply(connection, directory)
    assert (
        connection.execute(
            "SELECT record_id, tag FROM active_event_data ORDER BY record_id"
        ).fetchall()
        == before
    )
    assert report.find_duplicate_tags(connection) == [
        {"event_slug": "test-event", "tag": "duplicate-test-tag", "count": 2}
    ]
    assert connection.execute("SELECT count(*) FROM fgt_schema_migrations").fetchone()[0] == 0


@pytest.mark.db
def test_report_cli_is_read_only_and_outputs_no_profile_data(
    isolated_migration_db, monkeypatch, capsys
):
    connection, _, schema = isolated_migration_db
    seed_duplicate(connection)
    seed_duplicate(connection, tag="duplicate-test-tag")
    original_connect = psycopg.connect
    original_report = report.find_duplicate_tags

    def connect(url, **kwargs):
        return original_connect(url, options=f"-csearch_path={schema},public", **kwargs)

    def readonly_report(active, event_slug):
        result = original_report(active, event_slug)
        assert active.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        return result

    monkeypatch.setattr(report.psycopg, "connect", connect)
    monkeypatch.setattr(report, "find_duplicate_tags", readonly_report)
    assert report.main(["--dry-run", "--event", "test-event"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "dry_run": True,
        "duplicates": [{"event_slug": "test-event", "tag": "duplicate-test-tag", "count": 2}],
    }
    assert "Test Player" not in output
    assert "player@example.invalid" not in output
    assert "0000000000" not in output

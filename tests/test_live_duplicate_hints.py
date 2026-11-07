"""T-068: live "possible duplicate" hints for the active event (real test Postgres, synthetic data)."""

import json

import pytest
from psycopg.rows import dict_row

from shared import postgres_api as postgres
from tests.test_safe_archiving import (  # noqa: F401  (pytest fixtures)
    archive,
    archive_db,
    event,
    seed_player,
    table_rows,
)

pytestmark = pytest.mark.db
TO = {"user_id": "42", "user_name": "Synthetic TO", "user_email": ""}


def check_in(slug, tag, name="Synthetic Kenji", phone="0700000399"):
    return postgres.begin_checkin(slug, {"name": name, "tag": tag, "telephone": phone})[
        "checkin_id"
    ]


def row_of(database, record_id):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM active_event_data WHERE record_id = %s", (record_id,))
            return cursor.fetchone()


def player(database, uuid):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM players WHERE uuid = %s", (uuid,))
            return cursor.fetchone()


@pytest.fixture
def kenji(archive_db):  # noqa: F811
    return seed_player(archive_db, "pw-kenji", name="Synthetic Kenji", telephone="0700000301")


def test_similar_check_in_gets_a_hint_with_the_insights_reasons(event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    hints = postgres.refresh_live_duplicate_hints(event)
    assert [h["candidate_uuid"] for h in hints[record]] == [kenji["uuid"]]
    hint = hints[record][0]
    assert hint["candidate_tag"] == "pw-kenji"
    assert set(hint["reasons"]) == {"similar_tag", "exact_name"}
    assert hint["confidence"] == "medium"
    # Same scoring as the Insights scan (shared helper).
    me = postgres._identity("Synthetic Kenji", "pw-kenjj", "0700000399")
    other = postgres._identity(kenji["name"], kenji["tag"], kenji["telephone"])
    assert postgres._duplicate_signals(me, other) == (hint["reasons"], hint["confidence"])


def test_unchanged_rows_are_not_rechecked_and_changes_are(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    postgres.refresh_live_duplicate_hints(event)
    before = table_rows(archive_db, "live_duplicate_hints")
    postgres.refresh_live_duplicate_hints(event)
    assert table_rows(archive_db, "live_duplicate_hints") == before  # no re-detection

    postgres.update_checkin(record, {"tag": "pw-zzzzzz", "name": "Somebody Else"})
    assert postgres.refresh_live_duplicate_hints(event) == {}
    marker = table_rows(archive_db, "live_duplicate_hints")
    assert [(r["record_id"], r["candidate_uuid"]) for r in marker] == [(record, "")]


def test_no_hint_without_similarity_and_none_for_its_own_player(event, kenji):  # noqa: F811
    check_in(event, "pw-totally-different", name="Another Person", phone="0700000398")
    exact = check_in(event, "pw-kenji", phone="0700000301")  # exact tag: linked at check-in
    hints = postgres.refresh_live_duplicate_hints(event)
    assert exact not in hints and len(hints) == 0


def test_not_same_creates_a_stub_only_on_the_action_and_dismisses(
    archive_db, event, kenji  # noqa: F811
):
    record = check_in(event, "pw-kenjj")
    postgres.refresh_live_duplicate_hints(event)
    assert row_of(archive_db, record)["player_uuid"] is None  # no stub per check-in

    result = postgres.resolve_live_duplicate(record, kenji["uuid"], "not_same", user=TO)
    stub = row_of(archive_db, record)["player_uuid"]
    assert result["player_uuid"] == stub and stub != kenji["uuid"]
    assert player(archive_db, stub)["tag"] == "pw-kenjj"
    assert postgres.is_duplicate_pair_dismissed(stub, kenji["uuid"])
    assert postgres.refresh_live_duplicate_hints(event) == {}
    pairs = {
        tuple(sorted((c["player_a"]["uuid"], c["player_b"]["uuid"])))
        for c in postgres.find_duplicate_candidates()
    }
    assert tuple(sorted((stub, kenji["uuid"]))) not in pairs  # not in the Insights queue either

    audit = [r for r in table_rows(archive_db, "audit_log") if r["action"] == "player_stub_created"]
    assert len(audit) == 1 and audit[0]["reason"] == "live_duplicate"
    assert json.loads(audit[0]["details"])["created_by"] == "TO action"
    assert audit[0]["user_name"] == "Synthetic TO"


def test_keep_candidate_merges_with_history_and_undo(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    result = postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    row = row_of(archive_db, record)
    assert row["player_uuid"] == kenji["uuid"]
    assert row["tag"] == "pw-kenjj"  # today's tag stays on the check-in row
    assert player(archive_db, result["remove_uuid"]) is None
    assert postgres.refresh_live_duplicate_hints(event) == {}
    history = postgres.get_merge_history(limit=1)
    assert history[0]["id"] == result["merge_id"]

    postgres.undo_merge(int(result["merge_id"]))
    restored = row_of(archive_db, record)["player_uuid"]
    assert restored == result["remove_uuid"]
    assert player(archive_db, restored)["tag"] == "pw-kenjj"


def test_keep_checkin_keeps_the_clicked_profile(archive_db, event, kenji):  # noqa: F811
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    alpah = seed_player(archive_db, "pw-alpah", name="Synthetic Alfa", telephone="0700000303")
    record = check_in(event, "pw-alpha", name="Synthetic Alpha", phone="0700000302")
    assert row_of(archive_db, record)["player_uuid"] == alpha["uuid"]
    hints = postgres.refresh_live_duplicate_hints(event)
    assert alpah["uuid"] in [h["candidate_uuid"] for h in hints[record]]
    result = postgres.resolve_live_duplicate(record, alpah["uuid"], "keep_checkin", user=TO)
    assert result["keep_uuid"] == alpha["uuid"]
    assert player(archive_db, alpah["uuid"]) is None
    audit = [r for r in table_rows(archive_db, "audit_log") if r["action"] == "player_stub_created"]
    assert audit == []  # an existing profile: no stub


def test_stale_hints_are_refused(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    with pytest.raises(ValueError):
        postgres.resolve_live_duplicate(record, "missing-player", "not_same", user=TO)
    with pytest.raises(ValueError):
        postgres.resolve_live_duplicate("missing-record", kenji["uuid"], "keep_candidate")
    with pytest.raises(ValueError):
        postgres.resolve_live_duplicate(record, kenji["uuid"], "delete_everything")
    assert row_of(archive_db, record)["player_uuid"] is None  # nothing was created


def test_pair_for_the_dialog(event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    pair = postgres.get_live_duplicate_pair(record, kenji["uuid"])
    assert pair["checkin"]["tag"] == "pw-kenjj" and pair["checkin"]["today"] is True
    assert pair["checkin"]["has_profile"] is False
    assert pair["candidate"]["tag"] == "pw-kenji" and pair["candidate"]["today"] is False
    assert pair["candidate"]["total_events"] == kenji["total_events"]
    assert set(pair["reasons"]) == {"similar_tag", "exact_name"}


# --- archive honours an explicit player_uuid (condition 2) ---------------------------------


def test_archive_keeps_a_linked_player_even_with_a_different_tag(
    archive_db, event, kenji
):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    before = player(archive_db, kenji["uuid"])["total_events"]
    archive(event)
    rows = [r for r in table_rows(archive_db, "event_archive") if r["event_slug"] == event]
    assert [r["player_uuid"] for r in rows] == [kenji["uuid"]]
    assert player(archive_db, kenji["uuid"])["total_events"] == before + 1
    tags = [p["tag"] for p in table_rows(archive_db, "players")]
    assert "pw-kenjj" not in tags  # no new profile for today's tag
    assert player(archive_db, kenji["uuid"])["tag"] == "pw-kenji"  # the kept profile's tag
    assert rows[0]["tag"] == "pw-kenjj"  # today's tag on the archived row


def test_archive_without_a_uuid_takes_exactly_the_old_path(archive_db, event, kenji):  # noqa: F811
    """No explicit uuid → no uuid lookup at all; tag/email matching as before."""
    statements = []

    class Recording:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, query, params=None):
            statements.append(str(query))
            return self.cursor.execute(query, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    from datetime import datetime, timezone

    with archive_db.pool.connection() as connection:
        with connection.cursor() as cursor:
            recording = Recording(cursor)
            matched, is_new = postgres._match_or_create_player(
                recording,
                {"tag": "PW-KENJI", "email": None, "player_uuid": None},
                event,
                "2026-11-07",
                datetime.now(timezone.utc),
            )
            created, created_new = postgres._match_or_create_player(
                recording,
                {"tag": "pw-brand-new", "email": None},
                event,
                "2026-11-07",
                datetime.now(timezone.utc),
            )
        connection.rollback()
    assert matched == kenji["uuid"] and is_new is False
    assert created_new is True and created != kenji["uuid"]
    explicit = [
        q for q in statements if q.lstrip().upper().startswith("SELECT") and "WHERE uuid = %s" in q
    ]
    assert explicit == []  # the explicit-uuid lookup never runs without a uuid


# --- privacy: the public check-in never sees candidates -------------------------------------


def test_public_check_in_response_has_no_candidate_info(app_client, pg_db, backend_app):
    from tests.fixtures import make_checkin

    seed_player(pg_db, "pw-kenji", name="Synthetic Kenji", telephone="0700000301")
    payload = make_checkin(tag="pw-kenjj", name="Synthetic Kenji")
    response = app_client.post("/api/checkin/orchestrate", json=payload)
    assert response.status_code == 200
    text = json.dumps(response.json()).lower()
    for leak in ("pw-kenji\"", "duplicate", "candidate", "is this you"):
        assert leak not in text, leak
    # The check-in path doesn't even compute hints; only the TO dashboard does.
    assert table_rows(pg_db, "live_duplicate_hints") == []


# --- security review: only TO-confirmed links are trusted by archive ------------------------


def linked_flag(database, record_id):
    with database.pool.connection() as connection:
        return connection.execute(
            "SELECT player_linked_by_to FROM active_event_data WHERE record_id = %s", (record_id,)
        ).fetchone()[0]


def test_only_a_to_action_sets_the_link_flag(archive_db, event, kenji):  # noqa: F811
    exact = check_in(event, "pw-kenji", phone="0700000301")  # automatic tag link
    assert row_of(archive_db, exact)["player_uuid"] == kenji["uuid"]
    assert linked_flag(archive_db, exact) is False
    record = check_in(event, "pw-kenjj")
    postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    assert linked_flag(archive_db, record) is True


def test_an_automatic_stale_link_keeps_the_old_archive_matching(archive_db, event):  # noqa: F811
    first = seed_player(archive_db, "pw-first", name="Synthetic First", telephone="0700000311")
    second = seed_player(archive_db, "pw-second", name="Synthetic Second", telephone="0700000312")
    # e.g. linked automatically at check-in, then the tag was corrected by a TO
    archive_db.seed_checkin(event_slug=event, tag="pw-second", player_uuid=first["uuid"])
    archive(event)
    rows = [r for r in table_rows(archive_db, "event_archive") if r["event_slug"] == event]
    assert [r["player_uuid"] for r in rows] == [second["uuid"]]  # tag wins, as before
    assert player(archive_db, first["uuid"])["total_events"] == first["total_events"]


def test_editing_the_tag_or_email_drops_the_to_link(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    postgres.update_checkin(record, {"status": "Pending"})  # unrelated edit keeps it
    assert linked_flag(archive_db, record) is True
    postgres.update_checkin(record, {"tag": "PW-KENJJ"})  # case only: same identity
    assert linked_flag(archive_db, record) is True
    postgres.update_checkin(record, {"tag": "pw-someone-else"})
    assert linked_flag(archive_db, record) is False


def test_a_to_linked_profile_keeps_its_identity_fields(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj", name="Synthetic Typo Name", phone="0700000399")
    postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    archive(event)
    kept = player(archive_db, kenji["uuid"])
    assert (kept["tag"], kept["name"], kept["telephone"], kept["email"]) == (
        kenji["tag"],
        kenji["name"],
        kenji["telephone"],
        kenji["email"],
    )

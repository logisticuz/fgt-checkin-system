"""T-068: live "possible duplicate" hints for the active event (real test Postgres, synthetic data)."""

import json
from datetime import datetime, timezone

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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


def check_in(slug, tag, name="Synthetic Kenji", phone="0700000399", email=None):
    data = {"name": name, "tag": tag, "telephone": phone}
    if email:
        data["email"] = email
    return postgres.begin_checkin(slug, data)["checkin_id"]


def decide(record, candidate, action, version=None):
    """What the dialog does: open (read the version), then submit the choice."""
    if version is None:
        version = postgres.get_live_duplicate_pair(record, candidate)["version"]
    return postgres.resolve_live_duplicate(record, candidate, action, user=TO, version=version)


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


def archived(database, slug):
    return [r for r in table_rows(database, "event_archive") if r["event_slug"] == slug]


def actions(database):
    return [r["action"] for r in table_rows(database, "audit_log")]


@pytest.fixture
def kenji(archive_db):  # noqa: F811
    return seed_player(archive_db, "pw-kenji", name="Synthetic Kenji", telephone="0700000301")


# --- detection --------------------------------------------------------------------------------


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
    assert postgres.detect_live_duplicate_hints(event) == 1
    before = table_rows(archive_db, "live_duplicate_hints")
    assert postgres.detect_live_duplicate_hints(event) == 0  # nothing changed: no re-check
    assert table_rows(archive_db, "live_duplicate_hints") == before

    postgres.update_checkin(record, {"tag": "pw-zzzzzz", "name": "Somebody Else"})
    assert postgres.detect_live_duplicate_hints(event) == 1
    assert postgres.get_live_duplicate_hints(event) == {}
    marker = table_rows(archive_db, "live_duplicate_hints")
    assert [(r["record_id"], r["candidate_uuid"]) for r in marker] == [(record, "")]
    # The stored fingerprint is opaque: no name, tag or phone in it.
    assert all(len(r["fingerprint"]) == 64 for r in marker)
    assert "zzzzzz" not in marker[0]["fingerprint"]


def test_no_hint_without_similarity_and_none_for_its_own_player(event, kenji):  # noqa: F811
    check_in(event, "pw-totally-different", name="Another Person", phone="0700000398")
    exact = check_in(event, "pw-kenji", phone="0700000301")  # exact tag: linked at check-in
    hints = postgres.refresh_live_duplicate_hints(event)
    assert exact not in hints and len(hints) == 0


def test_live_candidates_match_the_insights_scan_for_the_row(archive_db, event):  # noqa: F811
    """Round 2 P2#3: parity with Insights, incl. typos at both ends and phone formats."""
    both_ends = seed_player(archive_db, "abca", name="Profile One", telephone="0711000001")
    spaced = seed_player(archive_db, "pw-qqq", name="Profile Two", telephone=" 0700000777 ")
    dashed = seed_player(archive_db, "pw-rrr", name="Profile Three", telephone="070-000 07 77")
    seed_player(archive_db, "pw-unrelated", name="Someone Else", telephone="0711000009")
    seed_player(archive_db, "zz-far-away", name="Nobody Similar", telephone="0711000010")
    record = check_in(event, "xbcb", name="Row Person", phone="0700000777")
    live = {h["candidate_uuid"] for h in postgres.refresh_live_duplicate_hints(event)[record]}

    # Insights, for the same identity as a profile:
    twin = seed_player(archive_db, "xbcb", name="Row Person", telephone="0700000777")
    insights = set()
    for pair in postgres.find_duplicate_candidates(limit=1000):
        uuids = {pair["player_a"]["uuid"], pair["player_b"]["uuid"]}
        if twin["uuid"] in uuids:
            insights |= uuids - {twin["uuid"]}
    assert live == insights == {both_ends["uuid"], spaced["uuid"], dashed["uuid"]}


def test_phone_format_changes_never_leave_a_stale_result(archive_db, event):  # noqa: F811
    """Round 2 P2#4: scoring and the cache fingerprint both use digits only."""
    owner = seed_player(archive_db, "pw-owner", name="Phone Owner", telephone="0700000105")
    record = check_in(event, "pw-somebody", name="Other Name", phone="070-000-0105")
    hints = postgres.refresh_live_duplicate_hints(event)
    assert [h["candidate_uuid"] for h in hints[record]] == [owner["uuid"]]
    assert hints[record][0]["reasons"] == ["same_phone"]
    postgres.update_checkin(record, {"telephone": "0700000105"})  # same digits
    assert record in postgres.refresh_live_duplicate_hints(event)
    postgres.update_checkin(record, {"telephone": "0700000999"})  # another number
    assert postgres.refresh_live_duplicate_hints(event) == {}


def test_phone_digits_is_the_validation_normaliser():
    from backend.validation import sanitize_phone

    for value in ("070-123 45 67", "+46 70 123 45 67", " 0700000777 ", "", "07O0"):
        assert postgres._phone_digits(value) == sanitize_phone(value)


def test_undoing_a_dismissal_brings_the_live_suggestion_back(archive_db, event, kenji):  # noqa: F811
    """Review P2#7: a cached "no candidate" is re-checked when "Not same" is undone."""
    record = check_in(event, "pw-kenjj")
    decide(record, kenji["uuid"], "not_same")
    assert postgres.refresh_live_duplicate_hints(event) == {}
    stub = row_of(archive_db, record)["player_uuid"]
    assert postgres.undo_duplicate_dismissal(stub, kenji["uuid"], user=TO)
    hints = postgres.refresh_live_duplicate_hints(event)
    assert [h["candidate_uuid"] for h in hints[record]] == [kenji["uuid"]]


def test_hints_of_other_events_are_cleaned_up(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    postgres.detect_live_duplicate_hints(event)
    postgres.detect_live_duplicate_hints("pw-next-event")  # the next active event
    assert record not in {r["record_id"] for r in table_rows(archive_db, "live_duplicate_hints")}


# --- decisions ----------------------------------------------------------------------------------


def test_not_same_creates_a_stub_only_on_the_action_and_dismisses(
    archive_db, event, kenji  # noqa: F811
):
    record = check_in(event, "pw-kenjj")
    postgres.refresh_live_duplicate_hints(event)
    assert row_of(archive_db, record)["player_uuid"] is None  # no stub per check-in

    result = decide(record, kenji["uuid"], "not_same")
    stub = row_of(archive_db, record)["player_uuid"]
    assert result["player_uuid"] == stub and stub != kenji["uuid"]
    assert result["event_slug"] == event
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
    assert "duplicate_dismissed" in actions(archive_db)


def test_keep_candidate_merges_with_history_and_undo(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    result = decide(record, kenji["uuid"], "keep_candidate")
    row = row_of(archive_db, record)
    assert row["player_uuid"] == kenji["uuid"]
    assert row["tag"] == "pw-kenjj"  # today's tag stays on the check-in row
    assert player(archive_db, result["remove_uuid"]) is None
    assert postgres.refresh_live_duplicate_hints(event) == {}
    history = postgres.get_merge_history(limit=1)
    assert history[0]["id"] == result["merge_id"]
    merged = [r for r in table_rows(archive_db, "audit_log") if r["action"] == "player_merged"]
    assert json.loads(merged[-1]["details"])["source"] == "live_duplicate"

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
    result = decide(record, alpah["uuid"], "keep_checkin")
    assert result["keep_uuid"] == alpha["uuid"]
    assert player(archive_db, alpah["uuid"]) is None
    assert "player_stub_created" not in actions(archive_db)  # an existing profile: no stub


def test_stale_hints_are_refused(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    with pytest.raises(ValueError):
        decide(record, "missing-player", "not_same", version="x")
    with pytest.raises(ValueError):
        decide("missing-record", kenji["uuid"], "keep_candidate", version="x")
    with pytest.raises(ValueError):
        decide(record, kenji["uuid"], "delete_everything")
    with pytest.raises(postgres.StaleLiveDuplicate):  # never without the dialog's version
        postgres.resolve_live_duplicate(record, kenji["uuid"], "keep_candidate", user=TO)
    assert row_of(archive_db, record)["player_uuid"] is None  # nothing was created


def test_pair_for_the_dialog(event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    pair = postgres.get_live_duplicate_pair(record, kenji["uuid"])
    assert pair["checkin"]["tag"] == "pw-kenjj" and pair["checkin"]["today"] is True
    assert pair["checkin"]["has_profile"] is False
    assert pair["candidate"]["tag"] == "pw-kenji" and pair["candidate"]["today"] is False
    assert pair["candidate"]["total_events"] == kenji["total_events"]
    assert set(pair["reasons"]) == {"similar_tag", "exact_name"}
    assert len(pair["version"]) == 64
    assert pair["version"] == postgres.get_live_duplicate_pair(record, kenji["uuid"])["version"]


# --- review P1#1: the decision is bound to what the dialog showed --------------------------------


def test_a_profile_merged_elsewhere_after_opening_is_never_merged(archive_db, event):  # noqa: F811
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    alpah = seed_player(archive_db, "pw-alpah", name="Synthetic Alfa", telephone="0700000303")
    third = seed_player(archive_db, "pw-third", name="Synthetic Third", telephone="0700000304")
    record = check_in(event, "pw-alpha", name="Synthetic Alpha", phone="0700000302")
    shown = postgres.get_live_duplicate_pair(record, alpah["uuid"])["version"]
    postgres.merge_players(third["uuid"], alpha["uuid"], reason="another TO")  # A → C
    merges = len(postgres.get_merge_history())

    with pytest.raises(postgres.StaleLiveDuplicate):
        decide(record, alpah["uuid"], "keep_candidate", version=shown)
    assert player(archive_db, third["uuid"]) is not None  # C (never shown) is untouched
    assert player(archive_db, alpah["uuid"]) is not None
    assert len(postgres.get_merge_history()) == merges


def test_a_pair_dismissed_elsewhere_after_opening_is_not_merged(archive_db, event):  # noqa: F811
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    alpah = seed_player(archive_db, "pw-alpah", name="Synthetic Alfa", telephone="0700000303")
    record = check_in(event, "pw-alpha", name="Synthetic Alpha", phone="0700000302")
    shown = postgres.get_live_duplicate_pair(record, alpah["uuid"])["version"]
    postgres.dismiss_duplicate_pair(alpha["uuid"], alpah["uuid"], dismissed_by="another TO")
    with pytest.raises(postgres.StaleLiveDuplicate):
        decide(record, alpah["uuid"], "keep_checkin", version=shown)
    assert player(archive_db, alpah["uuid"]) is not None
    assert postgres.get_merge_history() == []


def test_an_edited_profile_needs_a_fresh_look(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    shown = postgres.get_live_duplicate_pair(record, kenji["uuid"])["version"]
    with archive_db.pool.connection() as connection:
        connection.execute(
            "UPDATE players SET name = 'Synthetic Renamed' WHERE uuid = %s", (kenji["uuid"],)
        )
    with pytest.raises(postgres.StaleLiveDuplicate):
        decide(record, kenji["uuid"], "keep_candidate", version=shown)
    assert row_of(archive_db, record)["player_uuid"] is None  # no stub either
    assert actions(archive_db) == []
    decide(record, kenji["uuid"], "keep_candidate")  # re-opened: the new version works
    assert row_of(archive_db, record)["player_uuid"] == kenji["uuid"]


# --- review P1#2: one transaction ----------------------------------------------------------------


def test_a_failed_merge_leaves_no_stub_link_or_audit(
    archive_db, event, kenji, monkeypatch  # noqa: F811
):
    record = check_in(event, "pw-kenjj")
    players_before = len(table_rows(archive_db, "players"))

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic merge failure")

    monkeypatch.setattr(postgres, "_merge_players_locked", broken)
    with pytest.raises(RuntimeError):
        decide(record, kenji["uuid"], "keep_candidate")
    assert row_of(archive_db, record)["player_uuid"] is None
    assert len(table_rows(archive_db, "players")) == players_before
    assert actions(archive_db) == []


def test_a_failed_audit_rolls_the_whole_decision_back(
    archive_db, event, kenji, monkeypatch  # noqa: F811
):
    record = check_in(event, "pw-kenjj")
    real = postgres._insert_audit

    def audit(cur, user, action, *args, **kwargs):
        if action == "player_merged":
            raise RuntimeError("synthetic audit failure")
        return real(cur, user, action, *args, **kwargs)

    monkeypatch.setattr(postgres, "_insert_audit", audit)
    with pytest.raises(RuntimeError):
        decide(record, kenji["uuid"], "keep_candidate")
    assert row_of(archive_db, record)["player_uuid"] is None
    assert postgres.get_merge_history() == []
    assert player(archive_db, kenji["uuid"]) is not None
    assert actions(archive_db) == []


# --- archive: the decision lives in merge_log (survives edits, reopen, other rows) ---------------


def test_archive_keeps_the_merged_into_profile_and_its_identity(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj", name="Synthetic Typo Name", phone="0700000399")
    decide(record, kenji["uuid"], "keep_candidate")
    before = player(archive_db, kenji["uuid"])["total_events"]
    archive(event)
    rows = archived(archive_db, event)
    assert [r["player_uuid"] for r in rows] == [kenji["uuid"]]
    assert rows[0]["tag"] == "pw-kenjj"  # today's tag on the archived row
    kept = player(archive_db, kenji["uuid"])
    assert kept["total_events"] == before + 1
    assert (kept["tag"], kept["name"], kept["telephone"], kept["email"]) == (
        kenji["tag"],
        kenji["name"],
        kenji["telephone"],
        kenji["email"],
    )
    assert "pw-kenjj" not in [p["tag"] for p in table_rows(archive_db, "players")]


def test_an_identity_edited_after_the_decision_is_matched_as_edited(
    archive_db, event, kenji  # noqa: F811
):
    """Review P1#2/P2#4: no stale trust to clear; archive uses the row's identity as it is."""
    other = seed_player(archive_db, "pw-other", name="Synthetic Other", telephone="0700000305")
    record = check_in(event, "pw-kenjj")
    decide(record, kenji["uuid"], "keep_candidate")
    postgres.update_checkin(record, {"tag": "pw-other"})  # any writer: TO edit, re-check-in…
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [other["uuid"]]
    assert player(archive_db, kenji["uuid"])["total_events"] == kenji["total_events"]


def test_a_new_email_on_the_row_keeps_the_decision_by_tag(archive_db, event, kenji):  # noqa: F811
    """Review P2#4: e.g. a public re-check-in with a new email; tag priority as always."""
    record = check_in(event, "pw-kenjj")
    decide(record, kenji["uuid"], "keep_candidate")
    with archive_db.pool.connection() as connection:  # whichever writer changed it
        connection.execute(
            "UPDATE active_event_data SET email = 'pw-new@example.invalid' WHERE record_id = %s",
            (record,),
        )
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [kenji["uuid"]]
    assert player(archive_db, kenji["uuid"])["email"] == kenji["email"]


def test_the_decision_survives_reopen_and_rearchive(archive_db, event, kenji):  # noqa: F811
    """Review P2#3: archive → clear → reopen → archive keeps the kept profile."""
    record = check_in(event, "pw-kenjj")
    decide(record, kenji["uuid"], "keep_candidate")
    archive(event)
    with archive_db.pool.connection() as connection:
        connection.execute("DELETE FROM active_event_data WHERE event_slug = %s", (event,))
    postgres.reopen_event(event, restore_active=True, user=TO)
    archive(event)
    assert {r["player_uuid"] for r in archived(archive_db, event)} == {kenji["uuid"]}
    assert "pw-kenjj" not in [p["tag"] for p in table_rows(archive_db, "players")]


def test_other_rows_of_the_merged_profile_follow_it(archive_db, event, kenji):  # noqa: F811
    """Review P2#3: every row moved by the merge, not only the clicked one."""
    typo = seed_player(archive_db, "pw-kenjj", name="Synthetic Kenji", telephone="0700000399")
    second_event = event + "-day2"
    first = check_in(event, "pw-kenjj")
    second = check_in(second_event, "pw-kenjj")
    assert row_of(archive_db, second)["player_uuid"] == typo["uuid"]
    decide(first, kenji["uuid"], "keep_candidate")
    archive(event)
    archive(second_event)
    assert [r["player_uuid"] for r in archived(archive_db, second_event)] == [kenji["uuid"]]
    assert "pw-kenjj" not in [p["tag"] for p in table_rows(archive_db, "players")]


def test_a_name_only_decision_survives_archive_and_reopen(archive_db, event, kenji):  # noqa: F811
    """Round 2 P2#1: no tag/email to match by; the decision covers the row itself."""
    archive_db.seed_checkin(
        event_slug=event, record_id="rec-name-only", name="Synthetic Kenji", tag=None, email=None,
        telephone=None,
    )
    decide("rec-name-only", kenji["uuid"], "keep_candidate")
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [kenji["uuid"]]
    kept = player(archive_db, kenji["uuid"])
    assert (kept["tag"], kept["email"]) == (kenji["tag"], kenji["email"])
    with archive_db.pool.connection() as connection:
        connection.execute("DELETE FROM active_event_data WHERE event_slug = %s", (event,))
    postgres.reopen_event(event, restore_active=True, user=TO)
    archive(event)
    assert {r["player_uuid"] for r in archived(archive_db, event)} == {kenji["uuid"]}
    names = [p["name"] for p in table_rows(archive_db, "players")]
    assert names.count("Synthetic Kenji") == 1  # no second profile for the name-only row


def test_a_name_only_not_same_survives_archive_and_reopen(archive_db, event, kenji):  # noqa: F811
    """Round 3 P2#1: the row stays on the TO's distinct profile; the candidate stays gone."""
    archive_db.seed_checkin(
        event_slug=event, record_id="rec-not-same", name="Synthetic Kenji", tag=None, email=None,
        telephone=None,
    )
    assert "rec-not-same" in postgres.refresh_live_duplicate_hints(event)
    decide("rec-not-same", kenji["uuid"], "not_same")
    stub = row_of(archive_db, "rec-not-same")["player_uuid"]
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [stub]
    with archive_db.pool.connection() as connection:
        connection.execute("DELETE FROM active_event_data WHERE event_slug = %s", (event,))
    postgres.reopen_event(event, restore_active=True, user=TO)
    assert row_of(archive_db, "rec-not-same")["player_uuid"] == stub
    assert postgres.refresh_live_duplicate_hints(event) == {}  # not suggested again
    archive(event)
    assert {r["player_uuid"] for r in archived(archive_db, event)} == {stub}
    assert player(archive_db, kenji["uuid"])["total_events"] == kenji["total_events"]


def test_undoing_the_not_same_releases_the_row(archive_db, event, kenji):  # noqa: F811
    archive_db.seed_checkin(
        event_slug=event, record_id="rec-undo", name="Synthetic Kenji", tag=None, email=None,
        telephone=None,
    )
    decide("rec-undo", kenji["uuid"], "not_same")
    stub = row_of(archive_db, "rec-undo")["player_uuid"]
    assert postgres.undo_duplicate_dismissal(stub, kenji["uuid"], user=TO)
    with archive_db.pool.connection() as connection:
        with connection.cursor() as cursor:
            checkin = {"record_id": "rec-undo", "name": "Synthetic Kenji"}
            assert postgres._confirmed_row_survivor(cursor, checkin) is None


def test_a_confirmed_row_never_overwrites_the_chosen_profile(archive_db, event):  # noqa: F811
    """Round 3 P2#2: both without a tag; the TO's survivor keeps its name and phone."""
    alice = seed_player(archive_db, "pw-alice", name="Synthetic Alice", telephone="0700000201")
    with archive_db.pool.connection() as connection:  # a profile without tag or email
        connection.execute(
            "UPDATE players SET tag = NULL, email = NULL WHERE uuid = %s", (alice["uuid"],)
        )
    archive_db.seed_checkin(
        event_slug=event, record_id="rec-alicf", name="Synthetic Alicf", tag=None, email=None,
        telephone="0700000202",
    )
    assert "rec-alicf" in postgres.refresh_live_duplicate_hints(event)
    decide("rec-alicf", alice["uuid"], "keep_candidate")
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [alice["uuid"]]
    kept = player(archive_db, alice["uuid"])
    assert (kept["name"], kept["telephone"], kept["tag"]) == ("Synthetic Alice", "0700000201", None)
    assert archived(archive_db, event)[0]["name"] == "Synthetic Alicf"  # today's on the row


def test_undoing_a_merge_drops_a_later_not_same_proof_for_its_rows(
    archive_db, event  # noqa: F811
):
    """Round 4 P2 (exact repro): tagged row A -> Keep B -> Not same vs C -> Undo -> archive A."""
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    other = seed_player(archive_db, "pw-alpah", name="Synthetic Alpha", telephone="0700000303")
    record = check_in(event, "pw-alfa", name="Synthetic Alpha", phone="0700000399")
    merged = decide(record, alpha["uuid"], "keep_candidate")  # stub A merged into B
    stub = merged["remove_uuid"]
    decide(record, other["uuid"], "not_same")  # proof: record -> B
    postgres.undo_merge(int(merged["merge_id"]))
    assert row_of(archive_db, record)["player_uuid"] == stub

    dismissals = table_rows(archive_db, "player_duplicate_dismissals")
    proofs = [r["confirmed_rows"] or {} for r in dismissals]
    assert all(record not in proof for proof in proofs)  # removed with the Undo
    audit = [
        r for r in table_rows(archive_db, "audit_log") if r["action"] == "decision_proofs_invalidated"
    ]
    assert len(audit) == 1
    assert json.loads(audit[0]["details"])["merge_id"] == merged["merge_id"]

    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [stub]
    assert player(archive_db, alpha["uuid"])["total_events"] == alpha["total_events"]


def test_archive_ignores_a_proof_reached_through_an_undone_merge(archive_db, event):  # noqa: F811
    """Defence in depth: even if such a proof were left behind, archive does not use it."""
    alpha = seed_player(archive_db, "pw-alpha", name="Synthetic Alpha", telephone="0700000302")
    other = seed_player(archive_db, "pw-alpah", name="Synthetic Alpha", telephone="0700000303")
    record = check_in(event, "pw-alfa", name="Synthetic Alpha", phone="0700000399")
    merged = decide(record, alpha["uuid"], "keep_candidate")
    decide(record, other["uuid"], "not_same")
    with archive_db.pool.connection() as connection:
        saved = connection.execute(
            "SELECT id, confirmed_rows FROM player_duplicate_dismissals"
        ).fetchall()
    postgres.undo_merge(int(merged["merge_id"]))
    with archive_db.pool.connection() as connection:  # put the stale proof back by hand
        for dismissal_id, proof in saved:
            connection.execute(
                "UPDATE player_duplicate_dismissals SET confirmed_rows = %s WHERE id = %s",
                (Jsonb(proof), dismissal_id),
            )
        with connection.cursor() as cursor:
            row = row_of(archive_db, record)
            assert postgres._confirmed_row_survivor(cursor, row) is None


def test_the_decision_records_the_rows_it_covers(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    result = decide(record, kenji["uuid"], "keep_candidate")
    entry = [r for r in table_rows(archive_db, "merge_log") if r["id"] == result["merge_id"]][0]
    assert list(entry["confirmed_rows"]) == [record]
    assert len(entry["confirmed_rows"][record]) == 64  # an opaque identity hash, no contact data


def test_later_merges_of_the_survivor_are_followed(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    decide(record, kenji["uuid"], "keep_candidate")
    final = seed_player(archive_db, "pw-final", name="Synthetic Final", telephone="0700000306")
    postgres.merge_players(final["uuid"], kenji["uuid"], reason="later merge")
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [final["uuid"]]
    assert player(archive_db, final["uuid"])["tag"] == "pw-final"


def test_an_undone_decision_no_longer_counts(archive_db, event, kenji):  # noqa: F811
    record = check_in(event, "pw-kenjj")
    result = decide(record, kenji["uuid"], "keep_candidate")
    postgres.undo_merge(int(result["merge_id"]))
    with archive_db.pool.connection() as connection:
        with connection.cursor() as cursor:
            assert postgres._merged_survivor(cursor, "tag", "PW-KENJJ") is None
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [result["remove_uuid"]]


def test_an_automatic_link_keeps_the_old_archive_matching(archive_db, event):  # noqa: F811
    first = seed_player(archive_db, "pw-first", name="Synthetic First", telephone="0700000311")
    second = seed_player(archive_db, "pw-second", name="Synthetic Second", telephone="0700000312")
    # e.g. linked automatically at check-in, then the tag was corrected by a TO
    archive_db.seed_checkin(event_slug=event, tag="pw-second", player_uuid=first["uuid"])
    archive(event)
    assert [r["player_uuid"] for r in archived(archive_db, event)] == [second["uuid"]]
    assert player(archive_db, first["uuid"])["total_events"] == first["total_events"]


def test_archive_without_a_merge_match_behaves_exactly_as_before(
    archive_db, event, kenji  # noqa: F811
):
    """Tag > email > new profile as before; the only extra statements are read-only
    merge_log lookups, and they run only after a direct tag/email miss."""
    statements = []

    class Recording:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, query, params=None):
            statements.append(str(query))
            return self.cursor.execute(query, params)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    def match(recording, checkin):
        return postgres._match_or_create_player(
            recording, checkin, event, "2026-11-07", datetime.now(timezone.utc)
        )

    with archive_db.pool.connection() as connection:
        with connection.cursor() as cursor:
            recording = Recording(cursor)
            matched, is_new = match(recording, {"tag": "PW-KENJI", "email": None})
            direct = list(statements)
            by_email, email_new = match(
                recording, {"tag": "pw-unknown", "email": kenji["email"].upper()}
            )
            created, created_new = match(recording, {"tag": "pw-brand-new", "email": None})
        connection.rollback()
    assert matched == kenji["uuid"] and is_new is False
    assert by_email == kenji["uuid"] and email_new is False
    assert created_new is True and created != kenji["uuid"]
    assert not any("merge_log" in q for q in direct)  # a direct tag hit never looks further
    extra = [q for q in statements if "merge_log" in q]
    assert extra and all(q.lstrip().upper().startswith("SELECT") for q in extra)


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

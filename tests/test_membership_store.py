"""Encrypted membership state and concurrency tests use only invented data and private PG."""

import base64
import importlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import psycopg
import pytest
from psycopg.rows import dict_row

from shared import membership_crypto as crypto, membership_store as store, migrate

ROOT = Path(__file__).resolve().parents[1]
PNR = "000000000000"
CODE = "123456"
EMAIL = "player@example.invalid"
PHONE = "0000000000"
CONTEXT = {"email": EMAIL, "phone": PHONE, "lang": "en"}


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch):
    monkeypatch.setenv("MEMBERSHIP_ENC_KEY", base64.b64encode(bytes(range(32))).decode("ascii"))


@pytest.fixture
def membership_db(pg_db):
    # Explicit path supports both the original T-006 runner and its packaged-path revision.
    migrate.apply(pg_db.pool, migrations_dir=ROOT / "shared" / "migrations")
    return pg_db


def make_attempt(**overrides):
    parameters = {
        "pnr": PNR,
        "contact": f"player-{uuid.uuid4().hex}@example.invalid",
        "channel": "email",
        "event_slug": "test-event",
        "code": CODE,
        "bylaws_version": "test-bylaws-v1",
        "privacy_version": "test-privacy-v1",
        "email": EMAIL,
        "phone": PHONE,
    }
    parameters.update(overrides)
    return store.create_attempt(**parameters)


def attempt_row(database, attempt_id):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM membership_attempts WHERE id = %s", (attempt_id,))
            return cursor.fetchone()


def receipt_row(database, attempt_id):
    with database.pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT * FROM membership_receipts WHERE attempt_id = %s", (attempt_id,))
            return cursor.fetchone()


def allow_resend(database, attempt_id):
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET code_sent_at = clock_timestamp() - interval '61 seconds' WHERE id = %s",
            (attempt_id,),
        )


def test_membership_migration_is_additive():
    path = ROOT / "shared" / "migrations" / "001_membership.sql"
    migrate.lint(path.read_text(encoding="utf-8"), path.name)


def test_crypto_is_randomized_bound_to_attempt_and_uses_keyed_hashes():
    attempt_id = uuid.uuid4()
    first = crypto.encrypt_pnr(PNR, attempt_id)
    second = crypto.encrypt_pnr(PNR, attempt_id)
    assert first != second
    assert len(first) == len(PNR.encode()) + 12 + 16
    assert crypto.decrypt_pnr(first, attempt_id) == PNR
    assert PNR.encode() not in first
    assert crypto.fingerprint(PNR) == crypto.fingerprint(PNR)
    assert len(crypto.fingerprint(PNR)) == 64
    hashed = crypto.hash_code(CODE, attempt_id)
    assert crypto.verify_code(CODE, attempt_id, hashed)
    assert not crypto.verify_code("654321", attempt_id, hashed)
    assert not crypto.verify_code(CODE, uuid.uuid4(), hashed)
    assert not crypto.verify_code("invalid", attempt_id, hashed)
    assert len(crypto.generate_code()) == 6
    with pytest.raises(crypto.MembershipCryptoError) as caught:
        crypto.decrypt_pnr(first, uuid.uuid4())
    assert PNR not in str(caught.value)


@pytest.mark.parametrize("key", [None, "invalid!", base64.b64encode(b"short").decode()])
def test_missing_or_invalid_key_fails_on_first_use_without_plaintext(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("MEMBERSHIP_ENC_KEY")
    else:
        monkeypatch.setenv("MEMBERSHIP_ENC_KEY", key)
    with pytest.raises(crypto.MembershipCryptoError) as caught:
        crypto.encrypt_pnr(PNR, uuid.uuid4())
    assert "MEMBERSHIP_ENC_KEY" in str(caught.value)
    assert PNR not in str(caught.value)


@pytest.mark.parametrize("operation", ["personal_id", "attempt_id", "ciphertext"])
def test_invalid_input_exception_messages_never_echo_the_personal_id(operation, caplog):
    with pytest.raises(ValueError) as caught:
        if operation == "personal_id":
            make_attempt(pnr=PNR + "invalid")
        elif operation == "attempt_id":
            store.begin_submit(PNR, CODE)
        else:
            crypto.decrypt_pnr(PNR.encode(), uuid.uuid4())
    assert PNR not in str(caught.value)
    assert PNR not in caplog.text


@pytest.mark.db
@pytest.mark.parametrize("state", ["submitted", "failed", "outcome_unknown"])
@pytest.mark.parametrize("error_code", ["provider_" + PNR, PNR, {"message": PNR}])
def test_all_persisted_outcomes_replace_prefixed_personal_id_error_codes(
    membership_db, state, error_code
):
    attempt_id = make_attempt()
    assert store.begin_submit(attempt_id, CODE).status == "submitting"
    assert store.finish_submit(attempt_id, state, error_code=error_code)
    row = attempt_row(membership_db, attempt_id)
    assert row["outcome_error_code"] == "internal_error"
    assert PNR not in row["outcome_error_code"]


@pytest.mark.db
def test_stable_whitelisted_outcome_error_code_is_preserved(membership_db):
    attempt_id = make_attempt()
    assert store.begin_submit(attempt_id, CODE).status == "submitting"
    assert store.finish_submit(attempt_id, "failed", error_code="ebas_unavailable")
    assert attempt_row(membership_db, attempt_id)["outcome_error_code"] == "ebas_unavailable"


@pytest.mark.db
def test_resend_rechecks_expiry_after_contact_lock_contention(membership_db, monkeypatch):
    attempt_id = make_attempt()
    fingerprint = attempt_row(membership_db, attempt_id)["contact_fingerprint"]
    new_hash = crypto.hash_code("654321", attempt_id)
    reached_lock = Event()
    original_lock = store._lock_fingerprint
    timing = []

    def observed_lock(connection, fingerprint, domain):
        started = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if domain == "contact":
            reached_lock.set()
        original_lock(connection, fingerprint, domain)
        timing.append((started, connection.execute("SELECT clock_timestamp()").fetchone()[0]))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with membership_db.pool.connection() as blocker:
            with blocker.transaction():
                original_lock(blocker, fingerprint, "contact")
                expires_at = blocker.execute(
                    """UPDATE membership_attempts SET
                           code_sent_at = statement_timestamp() - interval '61 seconds',
                           created_at = statement_timestamp() - interval '14 minutes 57 seconds',
                           expires_at = statement_timestamp() + interval '3 seconds'
                       WHERE id = %s RETURNING expires_at""",
                    (attempt_id,),
                ).fetchone()[0]
            # Commit the timestamps, then hold only the advisory lock so the worker
            # can reach it before the expiration and cooldown checks.
            with blocker.transaction():
                original_lock(blocker, fingerprint, "contact")
                monkeypatch.setattr(store, "_lock_fingerprint", observed_lock)
                resend = executor.submit(store.resend_code, attempt_id, new_hash)
                assert reached_lock.wait(timeout=5)
                deadline = time.monotonic() + 15
                while blocker.execute("SELECT clock_timestamp() < %s", (expires_at,)).fetchone()[0]:
                    assert time.monotonic() < deadline, "Test database clock did not reach expiry"
                    time.sleep(0.05)
                assert not resend.done(), timing
        assert resend.result(timeout=5) is False, timing
    assert timing and timing[0][1] >= expires_at
    row = attempt_row(membership_db, attempt_id)
    assert crypto.verify_code(CODE, attempt_id, row["code_hash"])
    with membership_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_code_sends").fetchone()[0] == 1


@pytest.mark.db
def test_create_persists_secrets_consent_and_send_accounting_together(membership_db):
    attempt_id = make_attempt(year=2026)
    attempt = attempt_row(membership_db, attempt_id)
    receipt = receipt_row(membership_db, attempt_id)
    assert attempt["state"] == "code_sent"
    assert crypto.decrypt_pnr(attempt["pnr_enc"], attempt_id) == PNR
    assert attempt["pnr_fingerprint"] == crypto.fingerprint(PNR)
    assert crypto.verify_code(CODE, attempt_id, attempt["code_hash"])
    assert attempt["code_expires_at"] - attempt["created_at"] == timedelta(minutes=10)
    assert attempt["expires_at"] - attempt["created_at"] == timedelta(minutes=15)
    assert receipt["year"] == 2026
    assert receipt["bylaws_version"] == "test-bylaws-v1"
    assert receipt["verified_at"] is None
    with membership_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_code_sends").fetchone()[0] == 1


@pytest.mark.db
def test_create_failure_rolls_back_attempt_receipt_and_send(membership_db, monkeypatch, caplog):
    def fail(*args):
        raise psycopg.IntegrityError(f"synthetic database error containing {PNR}")

    monkeypatch.setattr(store, "_record_send", fail)
    with pytest.raises(store.MembershipStoreError) as caught:
        make_attempt()
    assert PNR not in str(caught.value)
    assert PNR not in caplog.text
    with membership_db.pool.connection() as connection:
        for table in ("membership_attempts", "membership_receipts", "membership_code_sends"):
            assert (
                connection.execute(
                    psycopg.sql.SQL("SELECT count(*) FROM {}").format(psycopg.sql.Identifier(table))
                ).fetchone()[0]
                == 0
            )


@pytest.mark.db
def test_concurrent_begin_submit_has_exactly_one_winner(membership_db):
    attempt_id = make_attempt()
    barrier = Barrier(2)

    def begin():
        barrier.wait(timeout=5)
        return store.begin_submit(attempt_id, CODE)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: begin(), range(2)))
    assert sorted(result.status for result in results) == ["already_submitting", "submitting"]
    winner = next(result for result in results if result.status == "submitting")
    assert winner.pnr == PNR
    assert PNR not in repr(winner)
    assert attempt_row(membership_db, attempt_id)["submit_started_at"] is not None
    assert receipt_row(membership_db, attempt_id)["verified_at"] is not None
    importlib.reload(store)
    assert store.begin_submit(attempt_id, CODE).status == "already_submitting"


@pytest.mark.db
def test_five_wrong_codes_lock_and_scrub_the_attempt(membership_db):
    attempt_id = make_attempt()
    for _ in range(4):
        assert store.begin_submit(attempt_id, "999999").status == "invalid_code"
    assert store.begin_submit(attempt_id, "999999").status == "locked"
    assert store.begin_submit(attempt_id, CODE).status == "locked"
    row = attempt_row(membership_db, attempt_id)
    assert row["verify_attempts"] == 5
    assert row["pnr_enc"] is row["code_hash"] is None
    assert receipt_row(membership_db, attempt_id)["outcome"] == "locked"


@pytest.mark.db
def test_expired_code_cannot_begin_submit(membership_db):
    attempt_id = make_attempt()
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET code_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
            (attempt_id,),
        )
    assert store.begin_submit(attempt_id, CODE).status == "expired"
    row = attempt_row(membership_db, attempt_id)
    assert row["state"] == "expired"
    assert row["pnr_enc"] is row["code_hash"] is None


@pytest.mark.db
def test_resend_cooldown_invalidates_old_code_without_resetting_strikes(membership_db):
    attempt_id = make_attempt()
    new_hash = crypto.hash_code("654321", attempt_id)
    assert not store.resend_code(attempt_id, new_hash)
    assert store.begin_submit(attempt_id, "999999").status == "invalid_code"
    allow_resend(membership_db, attempt_id)
    assert store.resend_code(attempt_id, new_hash)
    assert attempt_row(membership_db, attempt_id)["verify_attempts"] == 1
    assert store.begin_submit(attempt_id, CODE).status == "invalid_code"
    assert store.begin_submit(attempt_id, "654321").status == "submitting"
    assert not store.resend_code(attempt_id, crypto.hash_code("111111", attempt_id))


@pytest.mark.db
def test_rate_limit_counts_resends_of_the_same_attempt(membership_db):
    attempt_id = make_attempt()
    for code in ("234567", "345678"):
        allow_resend(membership_db, attempt_id)
        assert store.resend_code(attempt_id, crypto.hash_code(code, attempt_id))
    fingerprint = attempt_row(membership_db, attempt_id)["contact_fingerprint"]
    assert not store.rate_limit_ok(fingerprint)
    allow_resend(membership_db, attempt_id)
    assert not store.resend_code(attempt_id, crypto.hash_code("456789", attempt_id))
    with membership_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_code_sends").fetchone()[0] == 3
        connection.execute(
            "UPDATE membership_code_sends SET sent_at = clock_timestamp() - interval '16 minutes'"
        )
    assert store.rate_limit_ok(fingerprint)


@pytest.mark.db
def test_concurrent_creation_cannot_exceed_the_contact_rate_limit(membership_db):
    barrier = Barrier(4)

    def create():
        barrier.wait(timeout=5)
        try:
            return make_attempt(contact="shared-test-contact@example.invalid")
        except store.RateLimitExceeded:
            return "rate_limited"

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: create(), range(4)))
    assert results.count("rate_limited") == 1
    with membership_db.pool.connection() as connection:
        assert connection.execute("SELECT count(*) FROM membership_attempts").fetchone()[0] == 3


@pytest.mark.db
@pytest.mark.parametrize("state", ["submitted", "failed", "outcome_unknown"])
def test_finish_outcomes_and_reconcile_are_durable(membership_db, state):
    attempt_id = make_attempt()
    assert store.begin_submit(attempt_id, CODE).status == "submitting"
    assert store.finish_submit(
        attempt_id, state, error_code="provider_timeout" if state == "outcome_unknown" else None
    )
    row = attempt_row(membership_db, attempt_id)
    assert row["state"] == state
    assert row["code_hash"] is None
    assert (row["pnr_enc"] is not None) is (state == "outcome_unknown")
    assert receipt_row(membership_db, attempt_id)["outcome"] == state
    assert receipt_row(membership_db, attempt_id)["verified_at"] is not None
    if state == "outcome_unknown":
        assert store.begin_submit(attempt_id, CODE).status == "already_submitting"
        assert store.finish_submit(attempt_id, "submitted")
        assert attempt_row(membership_db, attempt_id)["pnr_enc"] is None
    assert store.begin_submit(attempt_id, CODE).pnr is None


@pytest.mark.db
def test_partial_unique_index_blocks_another_attempt_for_the_same_person(membership_db):
    first, second = make_attempt(), make_attempt()
    assert store.begin_submit(first, CODE).status == "submitting"
    assert store.begin_submit(second, CODE).status == "already_submitting"
    with membership_db.pool.connection() as connection:
        with pytest.raises(psycopg.errors.UniqueViolation) as caught:
            with connection.transaction():
                connection.execute(
                    "UPDATE membership_attempts SET state = 'submitting' WHERE id = %s", (second,)
                )
    assert PNR not in str(caught.value)
    assert store.finish_submit(first, "outcome_unknown")
    assert store.begin_submit(second, CODE).status == "already_submitting"


@pytest.mark.db
def test_new_attempt_after_success_does_not_submit_the_person_twice_in_one_year(membership_db):
    first = make_attempt(year=2026)
    second = make_attempt(year=2026)
    next_year = make_attempt(year=2027)
    assert store.begin_submit(first, CODE).status == "submitting"
    assert store.finish_submit(first, "submitted")
    duplicate = store.begin_submit(second, CODE)
    assert duplicate.status == "already_submitted"
    assert duplicate.pnr is None
    assert attempt_row(membership_db, second)["pnr_enc"] is None
    assert store.begin_submit(next_year, CODE).status == "submitting"


@pytest.mark.db
def test_moved_ciphertext_fails_without_plaintext_in_logs_or_errors(membership_db, caplog):
    first, second = make_attempt(), make_attempt()
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET pnr_enc = (SELECT pnr_enc FROM membership_attempts WHERE id = %s) WHERE id = %s",
            (first, second),
        )
    with pytest.raises(crypto.MembershipCryptoError) as caught:
        store.begin_submit(second, CODE)
    assert PNR not in str(caught.value)
    assert PNR not in caplog.text
    assert attempt_row(membership_db, second)["state"] == "code_sent"


@pytest.mark.db
def test_cleanup_scrubs_old_secrets_preserves_receipts_and_unknown_state(membership_db):
    expired, unknown, fresh = make_attempt(), make_attempt(), make_attempt()
    assert store.begin_submit(unknown, CODE).status == "submitting"
    assert store.finish_submit(unknown, "outcome_unknown")
    now = datetime.now(timezone.utc)
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET created_at = %s, expires_at = %s WHERE id IN (%s, %s)",
            (now - timedelta(minutes=16), now - timedelta(minutes=1), expired, unknown),
        )
        connection.execute(
            "UPDATE membership_code_sends SET sent_at = %s", (now - timedelta(hours=25),)
        )
    assert store.cleanup_expired(now) == 2
    assert store.cleanup_expired(now) == 0
    for attempt_id in (expired, unknown):
        row = attempt_row(membership_db, attempt_id)
        assert row["pnr_enc"] is row["context_enc"] is row["code_hash"] is None
        assert receipt_row(membership_db, attempt_id) is not None
    assert attempt_row(membership_db, expired)["state"] == "expired"
    assert attempt_row(membership_db, unknown)["state"] == "manual_review"
    assert attempt_row(membership_db, fresh)["pnr_enc"] is not None
    with membership_db.pool.connection() as connection:
        connection.execute("DELETE FROM membership_attempts WHERE id = %s", (expired,))
        assert connection.execute("SELECT count(*) FROM membership_code_sends").fetchone()[0] == 0
    assert receipt_row(membership_db, expired) is not None


@pytest.mark.db
def test_plaintext_personal_id_never_appears_in_database_columns(membership_db, caplog):
    attempt_id = make_attempt()
    store.begin_submit(attempt_id, CODE)
    store.finish_submit(attempt_id, "outcome_unknown")
    with membership_db.pool.connection() as connection:
        for table in ("membership_attempts", "membership_receipts", "membership_code_sends"):
            columns = connection.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = %s
                     AND data_type IN ('text', 'bytea')""",
                (table,),
            ).fetchall()
            for (column,) in columns:
                query = psycopg.sql.SQL("SELECT {} FROM {}").format(
                    psycopg.sql.Identifier(column), psycopg.sql.Identifier(table)
                )
                for (value,) in connection.execute(query).fetchall():
                    if value is not None:
                        encoded = value.encode() if isinstance(value, str) else bytes(value)
                        for secret in (PNR, EMAIL, PHONE):
                            assert secret.encode() not in encoded
    assert PNR not in caplog.text


@pytest.mark.db
def test_get_attempt_exposes_plaintext_only_by_explicit_opt_in_and_redacts_repr(membership_db):
    attempt_id = make_attempt()
    metadata = store.get_attempt(attempt_id)
    assert metadata.id == attempt_id
    assert metadata.state == "code_sent"
    assert metadata.pnr is metadata.context is None
    assert metadata.pnr_enc is not None and metadata.context_enc is not None
    plain = store.get_attempt(attempt_id, decrypt=True)
    assert plain.pnr == PNR
    assert plain.context == CONTEXT
    for value in (PNR, EMAIL, PHONE, "pnr_enc", "context_enc", "code_hash"):
        assert value not in repr(metadata)
        assert value not in repr(plain)
    plain.context["email"] = "different@example.invalid"
    assert store.get_attempt(attempt_id, decrypt=True).context == CONTEXT
    assert store.get_attempt(uuid.uuid4()) is None


@pytest.mark.db
def test_context_ciphertext_is_bound_to_its_attempt(membership_db, caplog):
    first = make_attempt()
    second = make_attempt(pnr="000000000001")
    encrypted = attempt_row(membership_db, first)["context_enc"]
    assert crypto.decrypt_context(encrypted, first) == CONTEXT
    with pytest.raises(crypto.MembershipCryptoError):
        crypto.decrypt_context(encrypted, second)
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET context_enc = %s WHERE id = %s", (encrypted, second)
        )
    with pytest.raises(crypto.MembershipCryptoError) as caught:
        store.begin_submit(second, CODE)
    assert attempt_row(membership_db, second)["state"] == "code_sent"
    for secret in (PNR, EMAIL, PHONE):
        assert secret not in str(caught.value)
        assert secret not in caplog.text


@pytest.mark.db
@pytest.mark.parametrize("error_code", ["send_failed", "provider_" + PNR, {"message": PNR}])
def test_fail_attempt_scrubs_context_and_preserves_receipt(membership_db, error_code):
    attempt_id = make_attempt()
    assert store.fail_attempt(attempt_id, error_code)
    row = attempt_row(membership_db, attempt_id)
    assert row["state"] == "failed"
    assert row["pnr_enc"] is row["context_enc"] is row["code_hash"] is None
    assert row["outcome_error_code"] == (
        "send_failed" if error_code == "send_failed" else "internal_error"
    )
    assert receipt_row(membership_db, attempt_id)["outcome"] == "failed"
    assert not store.fail_attempt(attempt_id, "send_failed")
    assert not store.fail_attempt(uuid.uuid4(), "send_failed")


@pytest.mark.db
def test_fail_attempt_and_begin_submit_are_atomic_competitors(membership_db):
    attempt_id = make_attempt()
    barrier = Barrier(2)

    def begin():
        barrier.wait(timeout=5)
        return store.begin_submit(attempt_id, CODE)

    def fail():
        barrier.wait(timeout=5)
        return store.fail_attempt(attempt_id, "send_failed")

    with ThreadPoolExecutor(max_workers=2) as executor:
        beginning, failing = executor.submit(begin), executor.submit(fail)
        begun, failed = beginning.result(timeout=5), failing.result(timeout=5)
    if begun.status == "submitting":
        assert failed is False
        assert begun.context == CONTEXT
        assert store.get_attempt(attempt_id).state == "submitting"
    else:
        assert begun.status == "failed"
        assert failed is True
        assert begun.pnr is begun.context is None


@pytest.mark.db
def test_concurrent_reconcile_has_one_claim_and_honors_the_interval(membership_db):
    attempt_id = make_attempt()
    assert store.claim_reconcile(attempt_id) is None
    assert store.begin_submit(attempt_id, CODE).context == CONTEXT
    assert store.claim_reconcile(attempt_id) is None
    assert store.finish_submit(attempt_id, "outcome_unknown")
    deadline = store.get_attempt(attempt_id).expires_at
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return store.claim_reconcile(attempt_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    claims = [result for result in results if result is not None]
    assert len(claims) == 1
    assert claims[0].pnr == PNR and claims[0].context == CONTEXT
    assert all(secret not in repr(claims[0]) for secret in (PNR, EMAIL, PHONE))
    assert store.get_attempt(attempt_id).last_reconciled_at is not None
    assert store.claim_reconcile(attempt_id) is None
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET last_reconciled_at = clock_timestamp() - interval '31 seconds' WHERE id = %s",
            (attempt_id,),
        )
    assert store.claim_reconcile(attempt_id) is not None
    assert store.get_attempt(attempt_id).expires_at == deadline
    assert store.claim_reconcile(uuid.uuid4()) is None


@pytest.mark.db
def test_hard_deadline_never_moves_on_resend_verify_or_reconcile(membership_db):
    attempt_id = make_attempt()
    original = store.get_attempt(attempt_id)
    assert original.expires_at == original.created_at + timedelta(minutes=15)
    allow_resend(membership_db, attempt_id)
    assert store.resend_code(attempt_id, crypto.hash_code("654321", attempt_id))
    resent = store.get_attempt(attempt_id)
    assert resent.expires_at == original.expires_at
    assert resent.code_expires_at <= original.expires_at
    assert store.begin_submit(attempt_id, CODE).status == "invalid_code"
    assert store.begin_submit(attempt_id, "654321").status == "submitting"
    assert store.finish_submit(attempt_id, "outcome_unknown")
    assert store.claim_reconcile(attempt_id) is not None
    assert store.get_attempt(attempt_id).expires_at == original.expires_at
    with membership_db.pool.connection() as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    "UPDATE membership_attempts SET expires_at = expires_at + interval '1 second' WHERE id = %s",
                    (attempt_id,),
                )


@pytest.mark.db
def test_deadline_purges_unknown_context_into_manual_review_and_prevents_resubmit(membership_db):
    attempt_id = make_attempt()
    assert store.begin_submit(attempt_id, CODE).status == "submitting"
    assert store.finish_submit(attempt_id, "outcome_unknown")
    deadline = store.get_attempt(attempt_id).expires_at
    assert store.cleanup_expired(deadline) == 1
    metadata = store.get_attempt(attempt_id, decrypt=True)
    assert metadata.state == "manual_review"
    assert metadata.pnr_enc is metadata.context_enc is metadata.code_hash is None
    assert metadata.pnr is metadata.context is None
    assert receipt_row(membership_db, attempt_id)["outcome"] == "manual_review"
    assert store.claim_reconcile(attempt_id) is None
    assert store.begin_submit(attempt_id, CODE).status == "manual_review"
    assert not store.resend_code(attempt_id, crypto.hash_code("654321", attempt_id))
    assert not store.fail_attempt(attempt_id, "rejected")
    another = make_attempt()
    assert store.begin_submit(another, CODE).status != "submitting"
    assert store.get_attempt(attempt_id).state == "manual_review"


@pytest.mark.db
def test_reconcile_with_purged_context_returns_nothing_and_marks_manual_review(membership_db):
    attempt_id = make_attempt()
    store.begin_submit(attempt_id, CODE)
    store.finish_submit(attempt_id, "outcome_unknown")
    with membership_db.pool.connection() as connection:
        connection.execute(
            "UPDATE membership_attempts SET context_enc = NULL WHERE id = %s", (attempt_id,)
        )
    assert store.claim_reconcile(attempt_id) is None
    metadata = store.get_attempt(attempt_id, decrypt=True)
    assert metadata.state == "manual_review"
    assert metadata.pnr_enc is metadata.context_enc is metadata.pnr is metadata.context is None


@pytest.mark.db
def test_get_and_reconcile_never_decrypt_after_deadline_even_before_cleanup(membership_db):
    attempt_id = make_attempt()
    store.begin_submit(attempt_id, CODE)
    store.finish_submit(attempt_id, "outcome_unknown")
    with membership_db.pool.connection() as connection:
        connection.execute(
            """UPDATE membership_attempts SET
               created_at = statement_timestamp() - interval '16 minutes',
               expires_at = statement_timestamp() - interval '1 minute'
               WHERE id = %s""",
            (attempt_id,),
        )
    metadata = store.get_attempt(attempt_id, decrypt=True)
    assert metadata.pnr is metadata.context is None
    assert store.claim_reconcile(attempt_id) is None
    metadata = store.get_attempt(attempt_id)
    assert metadata.state == "manual_review"
    assert metadata.pnr_enc is metadata.context_enc is None

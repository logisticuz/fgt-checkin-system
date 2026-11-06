"""Durable membership verification; callers submit externally only after begin_submit commits."""

import hashlib
import math
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from shared import membership_crypto as crypto, postgres_api

CODE_LIFETIME = timedelta(minutes=10)
ATTEMPT_LIFETIME = timedelta(minutes=15)
RESEND_DELAY = timedelta(seconds=60)
OUTCOME_ERROR_CODES = frozenset(
    {
        "wrong_code",
        "locked",
        "expired",
        "rate_limited",
        "send_failed",
        "rejected",
        "in_progress",
        "outcome_unknown",
        "unavailable",
        "ebas_unavailable",
        "invalid_details",
        "consent_required",
        "stale_texts",
        "year_changed",
        "internal_error",
    }
)


class MembershipStoreError(ValueError):
    """A membership operation failed without exposing its sensitive inputs."""


class RateLimitExceeded(MembershipStoreError):
    """A contact has received the maximum allowed number of verification codes."""


@dataclass(frozen=True)
class BeginSubmitResult:
    status: str
    pnr: str | None = field(default=None, repr=False)
    context: dict[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MembershipAttempt:
    id: str
    checkin_id: str | None
    event_slug: str
    state: str
    channel: str
    code_sent_at: datetime
    code_expires_at: datetime
    verify_attempts: int
    submit_started_at: datetime | None
    last_reconciled_at: datetime | None
    outcome_error_code: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    membership_year: int
    pnr_fingerprint: str | None = field(repr=False)
    contact_fingerprint: str = field(repr=False)
    pnr_enc: bytes | None = field(repr=False)
    context_enc: bytes | None = field(repr=False)
    code_hash: str | None = field(repr=False)
    pnr: str | None = field(default=None, repr=False)
    context: dict[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ReconcileClaim:
    attempt_id: str
    pnr: str = field(repr=False)
    context: dict[str, str] = field(repr=False)


def _safe_error_code(error_code):
    if error_code is None:
        return None
    return (
        error_code
        if isinstance(error_code, str) and error_code in OUTCOME_ERROR_CODES
        else "internal_error"
    )


@contextmanager
def _transaction():
    try:
        with postgres_api._get_pool().connection() as connection:
            with connection.transaction():
                yield connection
    except psycopg.Error:
        raise MembershipStoreError("Membership storage operation failed") from None


def _id(attempt_id):
    try:
        return uuid.UUID(str(attempt_id))
    except (TypeError, ValueError, AttributeError):
        raise MembershipStoreError("Invalid membership attempt ID") from None


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MembershipStoreError("Membership timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def contact_fingerprint(channel: str, contact: str) -> str:
    """Canonicalize presentation, then hash; callers should supply the phone's country prefix."""
    if channel not in ("sms", "email") or not isinstance(contact, str):
        raise MembershipStoreError("Invalid membership contact")
    canonical = contact.strip().casefold()
    if channel == "sms":
        canonical = re.sub(r"[^0-9]", "", canonical)
    if not canonical:
        raise MembershipStoreError("Invalid membership contact")
    return crypto.fingerprint(canonical)


def _lock_fingerprint(connection, fingerprint, domain):
    lock_id = int.from_bytes(
        hashlib.sha256(f"{domain}:{fingerprint}".encode("ascii")).digest()[:8], "big"
    )
    if lock_id >= 1 << 63:
        lock_id -= 1 << 64
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))


def _locked_person_attempt(connection, attempt_id):
    # Serialize both begin and finish before locking a row, including different attempts
    # for one person. Separate lock domains prevent contact/person lock inversions.
    found = connection.execute(
        "SELECT pnr_fingerprint FROM membership_attempts WHERE id = %s", (attempt_id,)
    ).fetchone()
    if found is None:
        return None
    if found[0] is not None:
        _lock_fingerprint(connection, found[0], "person")
    return _attempt(connection, attempt_id)


def _rate_limit_ok(connection, fingerprint, window_min=15, max_codes=3):
    return connection.execute(
        """SELECT count(*) < %s FROM membership_code_sends
           WHERE contact_fingerprint = %s
             AND sent_at > clock_timestamp() - %s * interval '1 minute'""",
        (max_codes, fingerprint, window_min),
    ).fetchone()[0]


def _record_send(connection, attempt_id, fingerprint, sent_at):
    connection.execute(
        """INSERT INTO membership_code_sends (attempt_id, contact_fingerprint, sent_at)
           VALUES (%s, %s, %s)""",
        (attempt_id, fingerprint, sent_at),
    )


def _attempt(connection, attempt_id):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM membership_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
        return cursor.fetchone()


def _receipt_outcome(connection, attempt_id, state, verified=False):
    connection.execute(
        """UPDATE membership_receipts SET outcome = %s,
           verified_at = CASE WHEN %s THEN COALESCE(verified_at, clock_timestamp())
                              ELSE verified_at END
           WHERE attempt_id = %s""",
        (state, verified, attempt_id),
    )


def create_attempt(
    *,
    pnr,
    contact,
    channel,
    event_slug,
    code,
    bylaws_version,
    privacy_version,
    email,
    phone,
    lang="en",
    checkin_id=None,
    year=None,
    consented_at=None,
) -> str:
    """Persist secrets, consent and send accounting together. PNR must be canonical 12 digits."""
    if not isinstance(pnr, str) or re.fullmatch(r"[0-9]{12}", pnr) is None:
        raise MembershipStoreError("Membership personal ID must contain twelve digits")
    if not all(
        isinstance(value, str) and value for value in (event_slug, bylaws_version, privacy_version)
    ):
        raise MembershipStoreError("Membership event and consent versions are required")
    if year is not None and (not isinstance(year, int) or not 1900 <= year <= 9999):
        raise MembershipStoreError("Invalid membership year")
    consented_at = _aware(consented_at) if consented_at is not None else None
    attempt_id = uuid.uuid4()
    encrypted = crypto.encrypt_pnr(pnr, attempt_id)
    context_enc = crypto.encrypt_context({"email": email, "phone": phone, "lang": lang}, attempt_id)
    pnr_fingerprint = crypto.fingerprint(pnr)
    fingerprint = contact_fingerprint(channel, contact)
    code_hash = crypto.hash_code(code, attempt_id)
    with _transaction() as connection:
        _lock_fingerprint(connection, fingerprint, "contact")
        if not _rate_limit_ok(connection, fingerprint):
            raise RateLimitExceeded("Too many membership verification codes for this contact")
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        connection.execute(
            """INSERT INTO membership_attempts
               (id, checkin_id, event_slug, state, pnr_enc, context_enc, pnr_fingerprint, channel,
                contact_fingerprint, code_hash, code_sent_at, code_expires_at,
                created_at, updated_at, expires_at)
               VALUES (%s, %s, %s, 'code_sent', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                attempt_id,
                checkin_id,
                event_slug,
                encrypted,
                context_enc,
                pnr_fingerprint,
                channel,
                fingerprint,
                code_hash,
                now,
                now + CODE_LIFETIME,
                now,
                now,
                now + ATTEMPT_LIFETIME,
            ),
        )
        connection.execute(
            """INSERT INTO membership_receipts
               (attempt_id, year, bylaws_version, privacy_version, consented_at, channel, outcome)
               VALUES (%s, %s, %s, %s, %s, %s, 'code_sent')""",
            (
                attempt_id,
                year or now.year,
                bylaws_version,
                privacy_version,
                consented_at or now,
                channel,
            ),
        )
        _record_send(connection, attempt_id, fingerprint, now)
    return str(attempt_id)


def begin_submit(attempt_id: str | uuid.UUID, code: str) -> BeginSubmitResult:
    """Claim exactly one submission, committing before returning plaintext in memory only."""
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        row = _locked_person_attempt(connection, attempt_id)
        if row is None:
            return BeginSubmitResult("not_found")
        state = row["state"]
        if state in ("submitting", "outcome_unknown"):
            return BeginSubmitResult("already_submitting")
        if state != "code_sent":
            return BeginSubmitResult("already_submitted" if state == "submitted" else state)
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if row["expires_at"] <= now or row["code_expires_at"] <= now:
            connection.execute(
                """UPDATE membership_attempts SET state = 'expired', pnr_enc = NULL, context_enc = NULL,
                   code_hash = NULL, updated_at = %s WHERE id = %s""",
                (now, attempt_id),
            )
            _receipt_outcome(connection, attempt_id, "expired")
            return BeginSubmitResult("expired")
        if not crypto.verify_code(code, attempt_id, row["code_hash"]):
            attempts = min(row["verify_attempts"] + 1, 5)
            state = "locked" if attempts == 5 else "code_sent"
            connection.execute(
                """UPDATE membership_attempts SET verify_attempts = %s, state = %s,
                   pnr_enc = CASE WHEN %s THEN NULL ELSE pnr_enc END,
                   context_enc = CASE WHEN %s THEN NULL ELSE context_enc END,
                   code_hash = CASE WHEN %s THEN NULL ELSE code_hash END,
                   updated_at = %s WHERE id = %s""",
                (
                    attempts,
                    state,
                    state == "locked",
                    state == "locked",
                    state == "locked",
                    now,
                    attempt_id,
                ),
            )
            if state == "locked":
                _receipt_outcome(connection, attempt_id, state)
            return BeginSubmitResult("locked" if state == "locked" else "invalid_code")
        already_submitted = connection.execute(
            """SELECT EXISTS (
                 SELECT 1 FROM membership_attempts previous
                 JOIN membership_receipts previous_receipt ON previous_receipt.attempt_id = previous.id
                 JOIN membership_receipts current_receipt ON current_receipt.attempt_id = %s
                 WHERE previous.pnr_fingerprint = %s AND previous.state = 'submitted'
                   AND previous_receipt.year = current_receipt.year)""",
            (attempt_id, row["pnr_fingerprint"]),
        ).fetchone()[0]
        if already_submitted:
            connection.execute(
                """UPDATE membership_attempts SET state = 'submitted', pnr_enc = NULL, context_enc = NULL,
                   code_hash = NULL, updated_at = clock_timestamp() WHERE id = %s""",
                (attempt_id,),
            )
            _receipt_outcome(connection, attempt_id, "submitted", verified=True)
            return BeginSubmitResult("already_submitted")
        pnr = crypto.decrypt_pnr(row["pnr_enc"], attempt_id)
        context = crypto.decrypt_context(row["context_enc"], attempt_id)
        try:
            # Savepoint preserves the transaction if another attempt already owns this PNR.
            with connection.transaction():
                claimed = connection.execute(
                    """UPDATE membership_attempts SET state = 'submitting', code_hash = NULL,
                       submit_started_at = clock_timestamp(), updated_at = clock_timestamp()
                       WHERE id = %s AND state = 'code_sent'
                         AND code_expires_at > clock_timestamp()
                         AND expires_at > clock_timestamp() RETURNING id""",
                    (attempt_id,),
                ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name != "membership_attempts_one_inflight_pnr":
                raise
            return BeginSubmitResult("already_submitting")
        if claimed is None:
            connection.execute(
                """UPDATE membership_attempts SET state = 'expired', pnr_enc = NULL, context_enc = NULL,
                   code_hash = NULL, updated_at = clock_timestamp() WHERE id = %s""",
                (attempt_id,),
            )
            _receipt_outcome(connection, attempt_id, "expired")
            return BeginSubmitResult("expired")
        _receipt_outcome(connection, attempt_id, "submitting", verified=True)
        return BeginSubmitResult("submitting", pnr, context)


def finish_submit(attempt_id: str | uuid.UUID, state: str, error_code: str | None = None) -> bool:
    if state not in ("submitted", "failed", "outcome_unknown"):
        raise MembershipStoreError("Invalid membership submission outcome")
    error_code = _safe_error_code(error_code)
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        row = _locked_person_attempt(connection, attempt_id)
        if row is None:
            return False
        if row["state"] == state:
            return True
        if row["state"] not in ("submitting", "outcome_unknown"):
            raise MembershipStoreError("Invalid membership state transition")
        connection.execute(
            """UPDATE membership_attempts SET state = %s, outcome_error_code = %s,
               pnr_enc = CASE WHEN %s THEN NULL ELSE pnr_enc END,
               context_enc = CASE WHEN %s THEN NULL ELSE context_enc END,
               code_hash = NULL, updated_at = clock_timestamp() WHERE id = %s""",
            (
                state,
                error_code,
                state in ("submitted", "failed"),
                state in ("submitted", "failed"),
                attempt_id,
            ),
        )
        _receipt_outcome(connection, attempt_id, state, verified=state == "submitted")
    return True


def get_attempt(attempt_id: str | uuid.UUID, decrypt: bool = False) -> MembershipAttempt | None:
    """Return metadata; opt-in plaintext is available only before the hard deadline."""
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        row = _attempt(connection, attempt_id)
        if row is None:
            return None
        values = {name: row[name] for name in MembershipAttempt.__dataclass_fields__ if name in row}
        values["id"] = str(row["id"])
        receipt = connection.execute(
            "SELECT year FROM membership_receipts WHERE attempt_id = %s", (attempt_id,)
        ).fetchone()
        if receipt is None:
            raise MembershipStoreError("Membership consent year is unavailable")
        values["membership_year"] = receipt[0]
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if (
            decrypt
            and row["expires_at"] > now
            and row["pnr_enc"] is not None
            and row["context_enc"] is not None
        ):
            values["pnr"] = crypto.decrypt_pnr(row["pnr_enc"], attempt_id)
            values["context"] = crypto.decrypt_context(row["context_enc"], attempt_id)
        return MembershipAttempt(**values)


def fail_attempt(attempt_id: str | uuid.UUID, error_code: str | None) -> bool:
    """Fail only an unclaimed challenge; a late sender error cannot erase an active submit."""
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        changed = connection.execute(
            """UPDATE membership_attempts SET state = 'failed', pnr_enc = NULL,
               context_enc = NULL, code_hash = NULL, outcome_error_code = %s,
               updated_at = clock_timestamp()
               WHERE id = %s AND state = 'code_sent' RETURNING id""",
            (_safe_error_code(error_code), attempt_id),
        ).fetchone()
        if changed is None:
            return False
        _receipt_outcome(connection, attempt_id, "failed")
    return True


def _mark_manual_review(connection, attempt_id):
    changed = connection.execute(
        """UPDATE membership_attempts SET state = 'manual_review', pnr_enc = NULL,
           context_enc = NULL, code_hash = NULL, updated_at = clock_timestamp()
           WHERE id = %s AND state = 'outcome_unknown' RETURNING id""",
        (attempt_id,),
    ).fetchone()
    if changed is not None:
        _receipt_outcome(connection, attempt_id, "manual_review")


def claim_reconcile(attempt_id: str | uuid.UUID, min_interval_s: int = 30) -> ReconcileClaim | None:
    """Claim one read-only provider reconciliation; this never authorizes a new submit."""
    if not isinstance(min_interval_s, int) or min_interval_s < 0:
        raise MembershipStoreError("Invalid membership reconciliation interval")
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        row = _locked_person_attempt(connection, attempt_id)
        if row is None or row["state"] != "outcome_unknown":
            return None
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if row["expires_at"] <= now or row["pnr_enc"] is None or row["context_enc"] is None:
            _mark_manual_review(connection, attempt_id)
            return None
        previous = row["last_reconciled_at"]
        if previous is not None and (now - previous).total_seconds() < min_interval_s:
            return None
        changed = connection.execute(
            """UPDATE membership_attempts SET last_reconciled_at = clock_timestamp(),
               updated_at = clock_timestamp()
               WHERE id = %s AND state = 'outcome_unknown' AND expires_at > clock_timestamp()
                 AND (last_reconciled_at IS NULL OR
                      last_reconciled_at <= clock_timestamp() - %s * interval '1 second')
               RETURNING id""",
            (attempt_id, min_interval_s),
        ).fetchone()
        if changed is None:
            expired = connection.execute(
                "SELECT expires_at <= clock_timestamp() FROM membership_attempts WHERE id = %s",
                (attempt_id,),
            ).fetchone()[0]
            if expired:
                _mark_manual_review(connection, attempt_id)
            return None
        return ReconcileClaim(
            str(attempt_id),
            crypto.decrypt_pnr(row["pnr_enc"], attempt_id),
            crypto.decrypt_context(row["context_enc"], attempt_id),
        )


def resend_code(attempt_id: str | uuid.UUID, new_code_hash: str) -> bool:
    """Return false for cooldown/expiry/limits; never reset the five-strike counter."""
    if not isinstance(new_code_hash, str) or re.fullmatch(r"[0-9a-f]{64}", new_code_hash) is None:
        raise MembershipStoreError("Invalid membership verification hash")
    attempt_id = _id(attempt_id)
    with _transaction() as connection:
        found = connection.execute(
            "SELECT contact_fingerprint FROM membership_attempts WHERE id = %s", (attempt_id,)
        ).fetchone()
        if found is None:
            return False
        _lock_fingerprint(connection, found[0], "contact")
        row = _attempt(connection, attempt_id)
        if row is None or row["state"] != "code_sent":
            return False
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if row["expires_at"] <= now or now - row["code_sent_at"] < RESEND_DELAY:
            return False
        if row["code_hash"] == new_code_hash:
            return False
        if not _rate_limit_ok(connection, row["contact_fingerprint"]):
            return False
        updated = connection.execute(
            """UPDATE membership_attempts SET code_hash = %s, code_sent_at = clock_timestamp(),
               code_expires_at = LEAST(clock_timestamp() + interval '10 minutes', expires_at),
               updated_at = clock_timestamp()
               WHERE id = %s AND state = 'code_sent' AND expires_at > clock_timestamp()
                 AND code_sent_at <= clock_timestamp() - interval '60 seconds'
               RETURNING code_sent_at""",
            (new_code_hash, attempt_id),
        ).fetchone()
        if updated is None:
            return False
        _record_send(connection, attempt_id, row["contact_fingerprint"], updated[0])
    return True


def rate_limit_ok(contact_fingerprint: str, window_min: int = 15, max_codes: int = 3) -> bool:
    if (
        not isinstance(contact_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", contact_fingerprint) is None
    ):
        raise MembershipStoreError("Invalid membership contact fingerprint")
    if not isinstance(window_min, int) or not 1 <= window_min <= 1440:
        raise MembershipStoreError("Invalid membership rate-limit window")
    if not isinstance(max_codes, int) or max_codes < 1:
        raise MembershipStoreError("Invalid membership rate limit")
    with _transaction() as connection:
        return _rate_limit_ok(connection, contact_fingerprint, window_min, max_codes)


def recover_abandoned_submitting(older_than_s: int | float) -> int:
    """Recover durable abandoned claims for confirmation only; never authorize a resubmit."""
    if (
        type(older_than_s) not in (int, float)
        or not math.isfinite(older_than_s)
        or older_than_s < 0
    ):
        raise MembershipStoreError("Invalid abandoned membership submission interval")
    with _transaction() as connection:
        rows = connection.execute(
            """UPDATE membership_attempts SET state = 'outcome_unknown',
               outcome_error_code = NULL, updated_at = clock_timestamp()
               WHERE state = 'submitting'
                 AND submit_started_at < clock_timestamp() - %s * interval '1 second'
               RETURNING id""",
            (older_than_s,),
        ).fetchall()
        for (attempt_id,) in rows:
            _receipt_outcome(connection, attempt_id, "outcome_unknown")
    return len(rows)


def cleanup_expired(now: datetime | None = None) -> int:
    now = _aware(now) if now is not None else None
    with _transaction() as connection:
        if now is None:
            now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        rows = connection.execute(
            """UPDATE membership_attempts SET pnr_enc = NULL, context_enc = NULL, code_hash = NULL,
               state = CASE WHEN state = 'code_sent' THEN 'expired'
                            WHEN state IN ('submitting', 'outcome_unknown')
                              THEN 'manual_review' ELSE state END,
               updated_at = %s
               WHERE expires_at <= %s
                 AND (pnr_enc IS NOT NULL OR context_enc IS NOT NULL OR code_hash IS NOT NULL
                      OR state IN ('code_sent', 'submitting', 'outcome_unknown'))
               RETURNING id, state""",
            (now, now),
        ).fetchall()
        for attempt_id, state in rows:
            if state in ("expired", "manual_review"):
                _receipt_outcome(connection, attempt_id, state)
        connection.execute(
            "DELETE FROM membership_code_sends WHERE sent_at < %s", (now - timedelta(hours=24),)
        )
    return len(rows)

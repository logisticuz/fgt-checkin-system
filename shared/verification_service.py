"""App-owned code issuance and checking behind a replaceable verification boundary."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from shared import membership_crypto as crypto, membership_store as store
from shared.verification_sender import get_sender


class VerificationServiceError(ValueError):
    def __init__(self, error_code: str, retry_after_s: int | None = None):
        self.error_code = error_code
        self.retry_after_s = retry_after_s
        super().__init__(error_code)


@dataclass(frozen=True)
class VerificationCheck:
    status: Literal["verified", "invalid", "expired", "locked", "unavailable"]
    submission_claim: store.BeginSubmitResult | None = field(default=None, repr=False)


def _field(record, name, default=None):
    return record.get(name, default) if isinstance(record, dict) else getattr(record, name, default)


def _context(record, name, default=None):
    value = _field(record, name)
    return _field(_field(record, "context", {}), name, default) if value is None else value


def start_verification(attempt_id, channel, destination, *, code=None):
    """Deliver a stored initial code; without code, atomically rotate and resend.

    The initial caller must create the durable attempt before passing its code.
    A provider-owned challenge will need its own issuance path after the API decision.
    """
    try:
        attempt = store.get_attempt(attempt_id)
        if attempt is None:
            raise VerificationServiceError("not_found")
        state = _field(attempt, "state")
        if state != "code_sent":
            raise VerificationServiceError(
                state if state in ("expired", "locked", "manual_review") else "unavailable"
            )
        if channel != _field(attempt, "channel") or store.contact_fingerprint(
            channel, destination
        ) != _field(attempt, "contact_fingerprint"):
            raise VerificationServiceError("unavailable")
        if code is None:
            code = crypto.generate_code()
            if not store.resend_code(attempt_id, crypto.hash_code(code, attempt_id)):
                latest = store.get_attempt(attempt_id)
                if _field(latest, "expires_at", datetime.now(timezone.utc)) <= datetime.now(
                    timezone.utc
                ):
                    raise VerificationServiceError("expired")
                quota_ok = store.rate_limit_ok(_field(attempt, "contact_fingerprint"))
                raise VerificationServiceError("rate_limited", 60 if quota_ok else 900)
            attempt = store.get_attempt(attempt_id)
        elif _field(attempt, "code_hash") is not None and not crypto.verify_code(
            code, attempt_id, _field(attempt, "code_hash")
        ):
            raise VerificationServiceError("unavailable")
        try:
            sent = get_sender().send_code(
                channel, destination, code, _context(attempt, "lang", "en")
            )
            success = _field(sent, "ok") is True
        except Exception:
            success = False
        if not success:
            store.fail_attempt(attempt_id, "send_failed")
            raise VerificationServiceError("send_failed")
        return {
            "challenge_ref": str(attempt_id),
            "expires_at": _field(attempt, "code_expires_at"),
            "resend_policy": {"resend_after_s": 60, "max_codes_per_15_minutes": 3},
        }
    except VerificationServiceError:
        raise
    except Exception:
        raise VerificationServiceError("unavailable") from None


def check_verification(attempt_id, challenge_ref, code) -> VerificationCheck:
    """Check and claim once; the plaintext winner claim is never cached or represented."""
    try:
        if uuid.UUID(str(attempt_id)) != uuid.UUID(str(challenge_ref)):
            return VerificationCheck("unavailable")
        claim = store.begin_submit(attempt_id, code)
        status = claim.status
        if status in ("submitting", "already_submitted"):
            return VerificationCheck("verified", claim)
        if status == "invalid_code":
            return VerificationCheck("invalid", claim)
        if status in ("expired", "locked"):
            return VerificationCheck(status, claim)
        return VerificationCheck("unavailable", claim)
    except Exception:
        return VerificationCheck("unavailable")

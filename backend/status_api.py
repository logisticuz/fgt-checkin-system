"""Capability-authorized check-in status, restricted to the active event."""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared import postgres_api

DEFAULT_TTL = timedelta(hours=6)
_KEY_CONTEXT = b"fgt/checkin/status-capability/v1"
_SERVER_SECRETS = (
    "MEMBERSHIP_ENC_KEY",
    "STARTGG_CLIENT_SECRET",
    "INTERNAL_API_TOKEN",
    "ADMIN_AUTH_COOKIE_TOKEN",
)
_TOKEN_KEY = None
_KEY_LOCK = threading.Lock()
_TOKEN_ENCODING = re.compile(r"[A-Za-z0-9_-]+")
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/checkin", tags=["Checkin"])


def _load_signing_key() -> bytes:
    configured = os.getenv("STATUS_TOKEN_KEY")
    if configured and configured.strip():
        return configured.encode("utf-8")
    for name in _SERVER_SECRETS:
        secret = os.getenv(name)
        if secret and secret.strip():
            return hmac.new(secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()
    logger.warning(
        "STATUS_TOKEN_KEY and private server secrets are unset; using a per-process key. "
        "Status capability tokens will not survive restarts."
    )
    return secrets.token_bytes(32)


def _key() -> bytes:
    global _TOKEN_KEY
    if _TOKEN_KEY is None:
        with _KEY_LOCK:
            if _TOKEN_KEY is None:
                _TOKEN_KEY = _load_signing_key()
    return _TOKEN_KEY


def _now_seconds() -> int:
    return int(time.time())


def _valid_id(checkin_id) -> bool:
    return (
        isinstance(checkin_id, str)
        and 0 < len(checkin_id) <= 128
        and "|" not in checkin_id
        and all(ord(character) >= 32 and ord(character) != 127 for character in checkin_id)
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def capability_token(checkin_id: str, ttl: timedelta | int = DEFAULT_TTL) -> str:
    """Bind an opaque check-in ID and expiry to the server's status HMAC key."""
    if not _valid_id(checkin_id):
        raise ValueError("Invalid check-in ID for status capability")
    try:
        seconds = int(ttl.total_seconds()) if isinstance(ttl, timedelta) else int(ttl)
        expires = _now_seconds() + seconds
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid status capability lifetime") from None
    payload = f"{checkin_id}|{expires}".encode("utf-8")
    signature = hmac.new(_key(), payload, hashlib.sha256).digest()
    return _encode(payload + b"|" + signature)


def verify_capability(checkin_id: str, token: str | None) -> bool:
    if (
        not _valid_id(checkin_id)
        or not isinstance(token, str)
        or len(token) > 1024
        or _TOKEN_ENCODING.fullmatch(token) is None
    ):
        return False
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if len(raw) <= 33 or raw[-33:-32] != b"|":
            return False
        # Reject alternative base64 encodings that decode to the same bytes.
        if not hmac.compare_digest(_encode(raw), token):
            return False
        payload, signature = raw[:-33], raw[-32:]
        expected = hmac.new(_key(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        signed_id, expiry = payload.rsplit(b"|", 1)
        if not hmac.compare_digest(signed_id, checkin_id.encode("utf-8")):
            return False
        return expiry.isdigit() and int(expiry) > _now_seconds()
    except (ValueError, UnicodeError, TypeError):
        return False


def status_context(request) -> dict:
    checkin_id = request.query_params.get("checkin_id")
    token = request.query_params.get("t")
    if verify_capability(checkin_id, token):
        return {"checkin_id": checkin_id, "status_token": token}
    return {}


def _not_found() -> dict:
    return {"found": False, "ready": False}


def lookup_checkin_status(checkin_id: str, *, include_display: bool = False) -> dict:
    """Load active-event status for a capability-verified caller; expose only safe fields."""
    settings = postgres_api.get_active_settings() or {}
    active_slug = settings.get("active_event_slug")
    if not active_slug:
        return _not_found()
    record = postgres_api.get_checkin_by_record_id(checkin_id)
    fields = (record or {}).get("fields", {})
    if not record or fields.get("event_slug") != active_slug:
        return _not_found()
    computed = postgres_api.compute_checkin_status(fields, settings)
    games = fields.get("tournament_games_registered")
    result = {
        "found": True,
        "ready": computed["ready"],
        "missing": computed["missing"],
        "member": bool(fields.get("member")),
        "startgg": bool(fields.get("startgg")),
        "payment_ok": "Payment" not in computed["missing"],
        "games": (
            [game for game in games if isinstance(game, str)]
            if isinstance(games, list)
            else []
        ),
        "status": computed["status"],
    }
    if include_display:
        result["tag"] = fields.get("tag") or ""
    return result


@router.get("/{checkin_id}/status")
def checkin_status(checkin_id: str, t: str | None = None):
    if not verify_capability(checkin_id, t):
        return JSONResponse(_not_found(), status_code=403)
    try:
        return lookup_checkin_status(checkin_id)
    except Exception:
        logger.warning("Capability status lookup is temporarily unavailable")
        return JSONResponse(_not_found(), status_code=503)

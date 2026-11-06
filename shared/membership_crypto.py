"""Short-lived membership secrets; no plaintext values are included in errors."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class MembershipCryptoError(ValueError):
    """Invalid membership key, ciphertext, or cryptographic input."""


def _key():
    try:
        encoded = os.environ["MEMBERSHIP_ENC_KEY"]
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (KeyError, ValueError, TypeError):
        raise MembershipCryptoError(
            "MEMBERSHIP_ENC_KEY must be a base64-encoded 32-byte key"
        ) from None
    if len(key) != 32:
        raise MembershipCryptoError("MEMBERSHIP_ENC_KEY must be a base64-encoded 32-byte key")
    return key


def _attempt_bytes(attempt_id):
    try:
        return uuid.UUID(str(attempt_id)).bytes
    except (ValueError, TypeError, AttributeError):
        raise MembershipCryptoError("Invalid membership attempt ID") from None


def _derived_key(info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_key())


def encrypt_pnr(pnr: str, attempt_id: str | uuid.UUID) -> bytes:
    """Return 96-bit nonce followed by ciphertext/tag, bound to this attempt's UUID."""
    if not isinstance(pnr, str):
        raise MembershipCryptoError("Invalid membership personal ID")
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(_key()).encrypt(nonce, pnr.encode("utf-8"), _attempt_bytes(attempt_id))


def decrypt_pnr(ciphertext: bytes, attempt_id: str | uuid.UUID) -> str:
    try:
        value = bytes(ciphertext)
        if len(value) < 28:
            raise MembershipCryptoError("Cannot decrypt membership personal ID")
        plaintext = AESGCM(_key()).decrypt(value[:12], value[12:], _attempt_bytes(attempt_id))
        return plaintext.decode("utf-8")
    except MembershipCryptoError:
        raise
    except (InvalidTag, ValueError, TypeError):
        raise MembershipCryptoError("Cannot decrypt membership personal ID") from None


def fingerprint(value: str) -> str:
    """Stable keyed digest for a canonical personal ID or canonical contact address."""
    if not isinstance(value, str):
        raise MembershipCryptoError("Invalid membership fingerprint input")
    return hmac.new(
        _derived_key(b"fgt-fingerprint"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str, attempt_id: str | uuid.UUID) -> str:
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise MembershipCryptoError("Verification code must contain six digits")
    message = _attempt_bytes(attempt_id) + b":" + code.encode("ascii")
    return hmac.new(_derived_key(b"fgt-verification-code"), message, hashlib.sha256).hexdigest()


def verify_code(code: str, attempt_id: str | uuid.UUID, expected_hash: str) -> bool:
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        return False
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        return False
    return hmac.compare_digest(hash_code(code, attempt_id), expected_hash)

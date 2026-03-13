# validation.py
"""
Input validation and sanitization for check-in data.

Why validate?
- Trimming: "Viktor " and "Viktor" should be the same person
- Max length: Prevent database overflow and attacks
- Phone/personnummer cleanup: "070-123 45 67" should match "0701234567"
- Normalize missing keys: "StartGG" and "startgg" should be treated the same
"""

import re
import logging

logger = logging.getLogger(__name__)

# === Field length limits ===
MAX_LENGTHS = {
    "namn": 100,
    "name": 100,
    "telefon": 20,
    "phone": 20,
    "personnummer": 12,
    "personal_id": 12,
    "tag": 30,
    "nick": 30,
    "email": 254,  # RFC 5321
    "discord": 50,
    "acquisition_source": 32,
}

# === Valid "missing" keys (normalized to English) ===
# Maps various input formats (including Swedish) to standard English keys
VALID_MISSING_KEYS = {
    "startgg": "startgg",
    "start.gg": "startgg",
    "membership": "membership",
    "medlemskap": "membership",  # Swedish backward compat
    "member": "membership",
    "payment": "payment",
    "betalning": "payment",  # Swedish backward compat
    "swish": "payment",
}


def sanitize_string(value: str, field_name: str = "") -> str:
    """
    Basic string sanitization:
    - Strip leading/trailing whitespace
    - Enforce max length if field_name is known
    """
    if not isinstance(value, str):
        return value

    result = value.strip()

    # Check max length
    max_len = MAX_LENGTHS.get(field_name.lower())
    if max_len and len(result) > max_len:
        logger.warning(f"Field '{field_name}' truncated from {len(result)} to {max_len} chars")
        result = result[:max_len]

    return result


def sanitize_phone(value: str) -> str:
    """
    Normalize phone number to digits only.
    "070-123 45 67" → "0701234567"
    "+46 70 123 45 67" → "46701234567"
    """
    if not isinstance(value, str):
        return value

    # Keep only digits
    digits = ''.join(c for c in value if c.isdigit())

    return digits


def sanitize_personnummer(value: str) -> str:
    """
    Normalize Swedish personal ID number.
    "19900101-1234" → "199001011234"
    "900101-1234" → "9001011234"
    """
    if not isinstance(value, str):
        return value

    # Remove dashes and spaces
    cleaned = value.replace("-", "").replace(" ", "")

    # Keep only digits
    digits = ''.join(c for c in cleaned if c.isdigit())

    return digits


def validate_personnummer(value: str) -> tuple:
    """
    Validate Swedish personnummer using Luhn algorithm.
    Accepts sanitized digit string (10 or 12 digits).
    Returns (is_valid, error_message).
    """
    if not value:
        return (False, "Personal ID is required")

    digits = sanitize_personnummer(value)
    if len(digits) not in (10, 12):
        return (False, f"Personal ID must be 10 or 12 digits (got {len(digits)})")

    # Use last 10 digits for Luhn (strip century if 12 digits)
    d = digits[-10:]

    # Basic date sanity check (YYMMDD)
    month = int(d[2:4])
    day = int(d[4:6])
    if month < 1 or month > 12:
        return (False, "Invalid month in Personal ID")
    if day < 1 or day > 31:
        return (False, "Invalid day in Personal ID")

    # Luhn checksum on all 10 digits
    weights = [2, 1, 2, 1, 2, 1, 2, 1, 2, 1]
    total = 0
    for i in range(10):
        val = int(d[i]) * weights[i]
        if val >= 10:
            val -= 9
        total += val

    if total % 10 != 0:
        return (False, "Invalid Personal ID (checksum failed)")

    return (True, "")


def normalize_missing_keys(missing: list) -> list:
    """
    Normalize 'missing' keys to consistent lowercase English values.
    ["StartGG", "Betalning"] → ["startgg", "payment"]
    Also deduplicates.
    """
    if not isinstance(missing, list):
        return missing

    normalized = set()
    for item in missing:
        if not isinstance(item, str):
            continue

        key = item.strip().lower()

        # Map to standard key if known
        if key in VALID_MISSING_KEYS:
            normalized.add(VALID_MISSING_KEYS[key])
        else:
            # Keep unknown keys as-is (lowercase)
            normalized.add(key)

    return list(normalized)


def sanitize_checkin_payload(data: dict) -> dict:
    """
    Sanitize a full check-in payload.
    Returns a new dict with cleaned values.
    """
    if not isinstance(data, dict):
        return data

    result = data.copy()

    # String fields to trim
    string_fields = ["namn", "name", "tag", "nick", "email", "discord", "acquisition_source"]
    for field in string_fields:
        if field in result and isinstance(result[field], str):
            result[field] = sanitize_string(result[field], field)

    # Phone fields (including 'telephone' used by register.html eBas form)
    phone_fields = ["telefon", "phone", "telephone"]
    for field in phone_fields:
        if field in result and isinstance(result[field], str):
            result[field] = sanitize_phone(result[field])

    # Personnummer fields
    pnr_fields = ["personnummer", "personal_id"]
    for field in pnr_fields:
        if field in result and isinstance(result[field], str):
            result[field] = sanitize_personnummer(result[field])

    # Missing keys normalization (supports both 'missing' and legacy 'saknas' key)
    if "saknas" in result and isinstance(result["saknas"], list):
        result["missing"] = normalize_missing_keys(result["saknas"])
        del result["saknas"]  # Convert to English key
    if "missing" in result and isinstance(result["missing"], list):
        result["missing"] = normalize_missing_keys(result["missing"])

    return result


def validate_checkin_payload(data: dict) -> list[str]:
    """
    Validate a check-in payload.
    Returns a list of error messages (empty if valid).
    """
    errors = []

    if not isinstance(data, dict):
        return ["Invalid payload format"]

    # Check required fields have content after trimming
    required = ["namn", "name"]  # At least one of these
    has_name = any(
        field in data and isinstance(data[field], str) and data[field].strip()
        for field in required
    )
    if not has_name:
        errors.append("Name is required")

    # Tag is required and must not be empty
    tag_fields = ["tag", "nick"]
    has_tag = any(
        field in data and isinstance(data[field], str) and data[field].strip()
        for field in tag_fields
    )
    if not has_tag:
        errors.append("Tag/gamertag is required")

    # Validate personnummer with Luhn (if provided)
    for field in ["personnummer", "personal_id"]:
        if field in data and data[field]:
            is_valid, err = validate_personnummer(str(data[field]))
            if not is_valid:
                errors.append(err)
                break

    # Validate phone format (if provided)
    for field in ["telefon", "phone", "telephone"]:
        if field in data and data[field]:
            phone = sanitize_phone(str(data[field]))
            if phone and len(phone) < 7:
                errors.append(f"Phone number too short (minimum 7 digits)")

    return errors

import os
import logging
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- ENV / Tables ---
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
BASE_ID = os.getenv("AIRTABLE_BASE_ID")

PLAYERS_TABLE = "players"
CHECKINS_TABLE = "active_event_data"   # real-time check-ins
EVENT_HISTORY_TABLE = "event_history"
SETTINGS_TABLE = "settings"
EVENT_HISTORY_DASHBOARD_TABLE = "event_history_dashboard"  # moved up

if not AIRTABLE_API_KEY or not BASE_ID:
    logger.critical("❌ Missing Airtable config in .env (AIRTABLE_API_KEY / AIRTABLE_BASE_ID)")
    raise EnvironmentError("Missing Airtable config in .env")

BASE_URL = f"https://api.airtable.com/v0/{BASE_ID}"

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {AIRTABLE_API_KEY}"})
DEFAULT_TIMEOUT = (5, 20)  # (connect, read)

# -----------------------------
# Core helpers (pagination etc.)
# -----------------------------
def _list_records(
    table: str,
    *,
    filter_formula: Optional[str] = None,
    fields: Optional[List[str]] = None,
    max_records: Optional[int] = None,
    page_size: int = 100
) -> List[Dict[str, Any]]:
    """List Airtable records with pagination + optional filter/fields."""
    url = f"{BASE_URL}/{table}"
    params: Dict[str, Any] = {"pageSize": page_size}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    if fields:
        for i, f in enumerate(fields):
            params[f"fields[{i}]"] = f  # Airtable multi-field param form
    if max_records:
        params["maxRecords"] = max_records

    records: List[Dict[str, Any]] = []
    offset = None

    while True:
        if offset:
            params["offset"] = offset
        try:
            resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"❌ Error listing '{table}': {e}")
            break

        data = resp.json() or {}
        batch = data.get("records", []) or []
        records.extend(batch)

        offset = data.get("offset")
        if not offset:
            break

        if max_records and len(records) >= max_records:
            records = records[:max_records]
            break

    logger.info(f"✅ Listed {len(records)} records from '{table}'")
    return records


# -----------------------------
# Settings (active event)
# -----------------------------
def get_active_settings() -> Optional[Dict[str, Any]]:
    """Return fields from the single active settings row (is_active = TRUE())."""
    # NOTE: your base uses 'is_active' (underscore)
    recs = _list_records(
        SETTINGS_TABLE,
        filter_formula="{is_active}=TRUE()",
        max_records=1,
    )
    if not recs:
        logger.warning("⚠️ No active settings row found.")
        return None
    return recs[0].get("fields", {}) or {}


def get_active_slug() -> Optional[str]:
    """Convenience: read active_event_slug from active settings row."""
    s = get_active_settings() or {}
    slug = s.get("active_event_slug")
    if slug:
        logger.info(f"🎯 Active slug: {slug}")
    else:
        logger.warning("⚠️ active_event_slug missing on active settings row.")
    return slug


# -----------------------------
# Checkins (active_event_data)
# -----------------------------
def get_checkins(slug: str = None, include_all: bool = False) -> List[Dict[str, Any]]:
    """
    Return check-ins for a given event_slug from active_event_data.
    Field list matches your actual Airtable schema.

    Args:
        slug: Event slug to filter by. If None and include_all=False, returns empty.
        include_all: If True, returns ALL records regardless of slug (for debugging).
    """
    if not slug and not include_all:
        return []

    # Build filter formula
    if include_all:
        formula = None  # No filter - return all records
    else:
        # Escape single quotes for Airtable formula
        safe_slug = (slug or "").replace("'", "''")
        formula = f"{{event_slug}} = '{safe_slug}'"

    # ✅ Only request fields that exist in your table
    fields = [
        "name", "email", "telephone", "tag",
        "payment_amount", "payment_expected", "payment_valid",
        "member", "startgg", "is_guest", "status",
        "tournament_games_registered",
        "UUID", "event_slug", "startgg_event_id", "external_id",
    ]

    recs = _list_records(CHECKINS_TABLE, filter_formula=formula, fields=fields)

    result: List[Dict[str, Any]] = []
    for r in recs:
        f = r.get("fields", {}) or {}

        # --- CHANGED: createdTime comes from record top-level, not fields[] ---
        result.append({
            "record_id": r.get("id"),  # Airtable record ID for updates
            "created": r.get("createdTime"),  # correct source
            "event_slug": f.get("event_slug"),
            "status": f.get("status"),

            # Membership / registration / payment
            "member": f.get("member"),
            "startgg": f.get("startgg"),
            "is_guest": f.get("is_guest"),
            "payment_amount": f.get("payment_amount"),
            "payment_expected": f.get("payment_expected"),
            "payment_valid": f.get("payment_valid"),

            # Player info
            "name": f.get("name"),
            "email": f.get("email"),
            "tag": f.get("tag"),
            "telephone": f.get("telephone"),

            # Extras
            "tournament_games_registered": f.get("tournament_games_registered"),
            "UUID": f.get("UUID"),
            "startgg_event_id": f.get("startgg_event_id"),
            "external_id": f.get("external_id"),
        })
        # --- /CHANGED ---

    if include_all:
        logger.info(f"📥 Found {len(result)} checkins (ALL events)")
    else:
        logger.info(f"📥 Found {len(result)} checkins for slug '{slug}'")
    return result


def get_all_event_slugs() -> List[str]:
    """
    Collect unique event_slug values from active_event_data,
    plus the active slug from settings as a fallback.
    """
    recs = _list_records(CHECKINS_TABLE, fields=["event_slug"])
    slugs = {r.get("fields", {}).get("event_slug") for r in recs if r.get("fields", {}).get("event_slug")}

    active = get_active_slug()
    if active:
        slugs.add(active)

    out = sorted(slugs)
    logger.info(f"📚 Retrieved {len(out)} unique event slugs (including active fallback).")
    return out


# -----------------------------
# Players
# -----------------------------
def get_players() -> List[Dict[str, Any]]:
    """
    Return players with new field names:
      - name, email, tag, telephone
    """
    # --- CHANGED: we don't need 'createdTime' in fields; it's record-level ---
    fields = ["name", "email", "tag", "telephone"]
    recs = _list_records(PLAYERS_TABLE, fields=fields)

    result = []
    for r in recs:
        f = r.get("fields", {}) or {}
        result.append({
            "id": r.get("id"),
            "name": f.get("name"),
            "email": f.get("email"),
            "tag": f.get("tag"),
            "telephone": f.get("telephone"),
            "created": r.get("createdTime"),  # <-- correct
        })
    logger.info(f"👥 Retrieved {len(result)} players.")
    return result


# -----------------------------
# Event history (snapshot)
# -----------------------------
def get_event_history() -> List[Dict[str, Any]]:
    """
    Return archived rows; adjust fields to your event_history table.
    Baseline:
      - event_slug, status, participants, created
    """
    # --- CHANGED: do not request createdTime in fields, read from record ---
    fields = ["event_slug", "status", "participants"]
    recs = _list_records(EVENT_HISTORY_TABLE, fields=fields)

    result = []
    for r in recs:
        f = r.get("fields", {}) or {}
        result.append({
            "id": r.get("id"),
            "event_slug": f.get("event_slug"),
            "status": f.get("status"),
            "participants": f.get("participants"),
            "created": r.get("createdTime"),  # <-- correct
        })
    logger.info(f"📦 Retrieved {len(result)} historical rows.")
    return result


# -----------------------------
# Event history Dashboard
# -----------------------------
def get_event_history_dashboard() -> List[Dict[str, Any]]:
    """
    Denormalized view for Dash (fast reads). Adjust fields to your actual columns.
    """
    # --- CHANGED: same createdTime approach as above ---
    fields = ["event_slug", "status", "participants"]
    recs = _list_records(EVENT_HISTORY_DASHBOARD_TABLE, fields=fields)

    result = []
    for r in recs:
        f = r.get("fields", {}) or {}
        result.append({
            "id": r.get("id"),
            "event_slug": f.get("event_slug"),
            "status": f.get("status"),
            "participants": f.get("participants"),
            "created": r.get("createdTime"),  # <-- correct
        })
    logger.info(f"📊 Retrieved {len(result)} dashboard-history rows.")
    return result


# -----------------------------
# Generic update/delete helpers
# -----------------------------
def _update_record(
    table: str, record_id: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Update a single record in Airtable. Returns updated record or None on error.

    Args:
        typecast: If True, Airtable will auto-create multi-select options that don't exist.
    """
    url = f"{BASE_URL}/{table}/{record_id}"
    payload = {"fields": fields}
    if typecast:
        payload["typecast"] = True
    try:
        resp = session.patch(url, json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ Updated record {record_id} in '{table}'")
        return data
    except requests.RequestException as e:
        logger.error(f"❌ Failed to update record {record_id} in '{table}': {e}")
        return None


def _delete_record(table: str, record_id: str) -> bool:
    """Delete a single record from Airtable. Returns True on success."""
    url = f"{BASE_URL}/{table}/{record_id}"
    try:
        resp = session.delete(url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        logger.info(f"🗑️ Deleted record {record_id} from '{table}'")
        return True
    except requests.RequestException as e:
        logger.error(f"❌ Failed to delete record {record_id} from '{table}': {e}")
        return False


# -----------------------------
# Settings operations
# -----------------------------
def get_active_settings_with_id() -> Optional[Dict[str, Any]]:
    """Return the active settings row with its record_id included."""
    recs = _list_records(
        SETTINGS_TABLE,
        filter_formula="{is_active}=TRUE()",
        max_records=1,
    )
    if not recs:
        logger.warning("⚠️ No active settings row found.")
        return None
    rec = recs[0]
    return {
        "record_id": rec.get("id"),
        "fields": rec.get("fields", {}),
    }


def update_settings(record_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update fields on a settings record."""
    return _update_record(SETTINGS_TABLE, record_id, fields)


# -----------------------------
# Checkin operations (active_event_data)
# -----------------------------
def get_checkin_by_name(name: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Find a checkin record by name (case-insensitive).
    Optionally filter by event_slug.
    Returns dict with record_id and fields, or None if not found.
    """
    safe_name = (name or "").replace("'", "''")
    formula = f"LOWER({{name}}) = LOWER('{safe_name}')"
    if slug:
        safe_slug = slug.replace("'", "''")
        formula = f"AND({formula}, {{event_slug}} = '{safe_slug}')"

    recs = _list_records(CHECKINS_TABLE, filter_formula=formula, max_records=1)
    if not recs:
        return None

    rec = recs[0]
    return {
        "record_id": rec.get("id"),
        "fields": rec.get("fields", {}),
    }


def get_checkin_by_tag(tag: str, slug: str) -> Optional[Dict[str, Any]]:
    """
    Find a checkin record by tag + event_slug (case-insensitive tag match).
    Returns dict with record_id and fields, or None if not found.
    """
    safe_tag = (tag or "").replace("'", "''")
    safe_slug = (slug or "").replace("'", "''")
    formula = f"AND(LOWER({{tag}})=LOWER('{safe_tag}'), {{event_slug}}='{safe_slug}')"

    recs = _list_records(CHECKINS_TABLE, filter_formula=formula, max_records=1)
    if not recs:
        return None

    rec = recs[0]
    return {
        "record_id": rec.get("id"),
        "fields": rec.get("fields", {}),
    }


def update_checkin(
    record_id: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Update fields on a checkin record (e.g., payment_valid, status).

    Args:
        typecast: If True, auto-create multi-select options (for tournament_games_registered).
    """
    return _update_record(CHECKINS_TABLE, record_id, fields, typecast=typecast)


def delete_checkin(record_id: str) -> bool:
    """Delete a checkin record."""
    return _delete_record(CHECKINS_TABLE, record_id)

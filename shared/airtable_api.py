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
SESSIONS_TABLE = "sessions"
AUDIT_LOG_TABLE = "audit_log"

if not AIRTABLE_API_KEY or not BASE_ID:
    logger.critical("❌ Missing Airtable config in .env (AIRTABLE_API_KEY / AIRTABLE_BASE_ID)")
    raise EnvironmentError("Missing Airtable config in .env")

BASE_URL = f"https://api.airtable.com/v0/{BASE_ID}"

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {AIRTABLE_API_KEY}"})
DEFAULT_TIMEOUT = (5, 20)  # (connect, read)


# -----------------------------
# Requirement helpers (Task 3.1)
# See: docs/9_Systemkontrakt_och_Invarianter.md
# -----------------------------
def compute_requirements(settings: Dict[str, Any]) -> Dict[str, bool]:
    """
    Compute which requirements are active based on settings.

    Airtable checkbox semantics:
    - Checkbox checked → True
    - Checkbox unchecked → field missing (None)
    - We treat None as False (requirement is OFF when unchecked)

    This allows TOs to disable requirements by unchecking the checkbox.
    All layers (backend, dashboard, n8n) should use this same logic.

    NOTE: This layer does NOT decide status - status is DERIVED data
    calculated by the consuming layer using the READY formula.
    """
    return {
        "require_payment": settings.get("require_payment") is True,
        "require_membership": settings.get("require_membership") is True,
        "require_startgg": settings.get("require_startgg") is True,
    }


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


def _create_record(
    table: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Create a single record in Airtable. Returns created record or None on error.

    Args:
        table: Airtable table name
        fields: Dict of field name -> value
        typecast: If True, Airtable auto-creates single/multi-select options.
    """
    url = f"{BASE_URL}/{table}"
    payload: Dict[str, Any] = {"fields": fields}
    if typecast:
        payload["typecast"] = True
    try:
        resp = session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ Created record in '{table}': {data.get('id')}")
        return data
    except requests.RequestException as e:
        logger.error(f"❌ Failed to create record in '{table}': {e}")
        return None


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


def begin_checkin(event_slug: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create or update a check-in attempt and return checkin_id.

    Dedupe strategy:
    - event_slug + tag (case-insensitive) if tag exists
    - else event_slug + name (case-insensitive) if name exists
    """
    if not event_slug:
        raise ValueError("event_slug is required")

    payload = payload or {}
    tag = (payload.get("tag") or "").strip()
    name = (payload.get("name") or "").strip()

    existing = None
    if tag:
        existing = get_checkin_by_tag(tag, event_slug)
    elif name:
        existing = get_checkin_by_name(name, event_slug)

    games = payload.get("tournament_games_registered")
    if isinstance(games, str):
        games = [g.strip() for g in games.split(",") if g.strip()]
    if not isinstance(games, list):
        games = []

    fields = {
        "event_slug": event_slug,
        "name": payload.get("name"),
        "tag": payload.get("tag"),
        "email": payload.get("email"),
        "telephone": payload.get("telephone"),
        "status": payload.get("status") or "Pending",
        "member": bool(payload.get("member", False)),
        "startgg": bool(payload.get("startgg", False)),
        "is_guest": bool(payload.get("is_guest", False)),
        "payment_valid": bool(payload.get("payment_valid", False)),
        "payment_amount": payload.get("payment_amount") or 0,
        "payment_expected": payload.get("payment_expected") or 0,
        "tournament_games_registered": games,
        "UUID": payload.get("UUID") or payload.get("checkin_uuid"),
        "startgg_event_id": payload.get("startgg_event_id"),
        "external_id": payload.get("external_id"),
    }

    if existing and existing.get("record_id"):
        checkin_id = existing["record_id"]
        updated = update_checkin(checkin_id, fields, typecast=True)
        if not updated:
            raise RuntimeError("Failed to update existing checkin")
        return {
            "checkin_id": checkin_id,
            "record_id": checkin_id,
            "event_slug": event_slug,
            "created": False,
        }

    created = _create_record(CHECKINS_TABLE, fields, typecast=True)
    if not created:
        raise RuntimeError("Failed to create checkin")

    checkin_id = created.get("id")
    return {
        "checkin_id": checkin_id,
        "record_id": checkin_id,
        "event_slug": event_slug,
        "created": True,
    }


# -----------------------------
# Sessions (Airtable-backed)
# -----------------------------
import uuid
from datetime import datetime, timezone, timedelta

# Session timeouts (from 04-security-and-auth.md)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)   # Full event day
SESSION_IDLE_TIMEOUT = timedelta(hours=2)        # Inactivity limit


def create_session(user_info: dict, access_token: str) -> Optional[str]:
    """
    Create a new session in Airtable and return the session_id.

    Args:
        user_info: Dict from auth.get_startgg_user() with id, name, email, slug
        access_token: Start.gg OAuth access token (stored for future API calls)

    Returns:
        session_id (uuid4 string) or None on failure
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    fields = {
        "session_id": session_id,
        "user_id": str(user_info.get("id", "")),
        "user_name": user_info.get("name", ""),
        "user_email": user_info.get("email", ""),
        "access_token": access_token,
        "created_at": now.isoformat(),
        "expires_at": (now + SESSION_ABSOLUTE_TIMEOUT).isoformat(),
        "last_active": now.isoformat(),
    }

    record = _create_record(SESSIONS_TABLE, fields)
    if record:
        logger.info(f"🔐 Created session for user '{user_info.get('name')}' ({session_id[:8]}...)")
        return session_id

    logger.error("❌ Failed to create session in Airtable")
    return None


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve and validate a session from Airtable.

    Checks two timeouts:
    1. Absolute timeout (8h from creation) - prevents indefinite sessions
    2. Idle timeout (2h since last activity) - catches abandoned sessions

    Returns dict with session fields or None if expired/invalid/not found.
    """
    if not session_id:
        return None

    safe_id = session_id.replace("'", "''")
    formula = f"{{session_id}} = '{safe_id}'"
    recs = _list_records(SESSIONS_TABLE, filter_formula=formula, max_records=1)

    if not recs:
        return None

    fields = recs[0].get("fields", {})
    record_id = recs[0].get("id")
    now = datetime.now(timezone.utc)

    # Check absolute expiry
    expires_at_str = fields.get("expires_at", "")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if now > expires_at:
                logger.info(f"🔒 Session {session_id[:8]}... expired (absolute timeout)")
                _delete_record(SESSIONS_TABLE, record_id)
                return None
        except ValueError:
            pass

    # Check idle timeout
    last_active_str = fields.get("last_active", "")
    if last_active_str:
        try:
            last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            if now - last_active > SESSION_IDLE_TIMEOUT:
                logger.info(f"🔒 Session {session_id[:8]}... expired (idle timeout)")
                _delete_record(SESSIONS_TABLE, record_id)
                return None
        except ValueError:
            pass

    # Session is valid - include record_id for updates
    fields["_record_id"] = record_id
    return fields


def delete_session(session_id: str) -> bool:
    """Delete a session from Airtable (logout)."""
    if not session_id:
        return False

    safe_id = session_id.replace("'", "''")
    formula = f"{{session_id}} = '{safe_id}'"
    recs = _list_records(SESSIONS_TABLE, filter_formula=formula, max_records=1)

    if not recs:
        return False

    record_id = recs[0].get("id")
    return _delete_record(SESSIONS_TABLE, record_id)


def update_session_activity(session_id: str) -> None:
    """
    Touch the last_active timestamp on a session.

    Called from /auth/me (once per page load) to keep the idle timeout fresh.
    Failures are logged but don't break the request.
    """
    if not session_id:
        return

    safe_id = session_id.replace("'", "''")
    formula = f"{{session_id}} = '{safe_id}'"
    recs = _list_records(SESSIONS_TABLE, filter_formula=formula, max_records=1, fields=["session_id"])

    if not recs:
        return

    record_id = recs[0].get("id")
    now = datetime.now(timezone.utc).isoformat()
    _update_record(SESSIONS_TABLE, record_id, {"last_active": now})


def cleanup_expired_sessions() -> int:
    """
    Delete all sessions past their absolute expiry.

    Called on container startup and can be called periodically.
    Returns number of sessions deleted.
    """
    now = datetime.now(timezone.utc).isoformat()
    formula = f"IS_BEFORE({{expires_at}}, '{now}')"
    recs = _list_records(SESSIONS_TABLE, filter_formula=formula)

    deleted = 0
    for rec in recs:
        if _delete_record(SESSIONS_TABLE, rec.get("id")):
            deleted += 1

    if deleted:
        logger.info(f"🧹 Cleaned up {deleted} expired sessions")
    return deleted


# -----------------------------
# Audit Log
# -----------------------------
def log_action(
    user: Dict[str, Any],
    action: str,
    target_table: str,
    *,
    target_event: str = "",
    target_record: str = "",
    target_player: str = "",
    reason: str = "",
    details: str = "",
    before_state: str = "",
    after_state: str = "",
) -> Optional[str]:
    """
    Write an entry to the audit log.

    Args:
        user: Session dict with user_id, user_name, user_email
        action: What happened (e.g., "event_archived", "player_deleted")
        target_table: Which Airtable table was affected
        **kwargs: Optional context fields (target_event, reason, before/after state)

    Returns:
        Record ID of the audit log entry, or None on failure.

    Note: Best-effort - failures are logged but don't block the original operation.
    Uses typecast=True so the action single-select auto-creates new options.
    """
    now = datetime.now(timezone.utc).isoformat()

    fields: Dict[str, Any] = {
        "timestamp": now,
        "user_id": user.get("user_id", ""),
        "user_name": user.get("user_name", "system"),
        "user_email": user.get("user_email", ""),
        "action": action,
        "target_table": target_table,
    }

    # Only include non-empty optional fields
    if target_event:
        fields["target_event"] = target_event
    if target_record:
        fields["target_record"] = target_record
    if target_player:
        fields["target_player"] = target_player
    if reason:
        fields["reason"] = reason
    if details:
        fields["details"] = details
    if before_state:
        fields["before_state"] = before_state
    if after_state:
        fields["after_state"] = after_state

    record = _create_record(AUDIT_LOG_TABLE, fields, typecast=True)

    if record:
        record_id = record.get("id", "")
        logger.info(f"📋 Audit: {user.get('user_name', '?')} -> {action} on {target_table}")
        return record_id

    logger.error(f"❌ Failed to write audit log: {action}")
    return None


def get_audit_log(
    *,
    action: Optional[str] = None,
    target_event: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Retrieve audit log entries with optional filters.

    Returns list of audit log entries, newest first.
    """
    conditions = []
    if action:
        safe_action = action.replace("'", "''")
        conditions.append(f"{{action}} = '{safe_action}'")
    if target_event:
        safe_event = target_event.replace("'", "''")
        conditions.append(f"{{target_event}} = '{safe_event}'")
    if user_id:
        safe_uid = user_id.replace("'", "''")
        conditions.append(f"{{user_id}} = '{safe_uid}'")

    formula = None
    if len(conditions) == 1:
        formula = conditions[0]
    elif len(conditions) > 1:
        formula = f"AND({', '.join(conditions)})"

    recs = _list_records(AUDIT_LOG_TABLE, filter_formula=formula, max_records=limit)

    result = []
    for r in recs:
        f = r.get("fields", {})
        result.append({
            "id": r.get("id"),
            "timestamp": f.get("timestamp"),
            "user_id": f.get("user_id"),
            "user_name": f.get("user_name"),
            "user_email": f.get("user_email"),
            "action": f.get("action"),
            "target_table": f.get("target_table"),
            "target_event": f.get("target_event"),
            "target_record": f.get("target_record"),
            "target_player": f.get("target_player"),
            "reason": f.get("reason"),
            "details": f.get("details"),
            "before_state": f.get("before_state"),
            "after_state": f.get("after_state"),
        })

    # Sort newest first
    result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    logger.info(f"📋 Retrieved {len(result)} audit log entries")
    return result

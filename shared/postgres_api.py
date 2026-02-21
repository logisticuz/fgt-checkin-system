"""
Postgres storage backend for FGC Check-in System.

Drop-in replacement for airtable_api.py - same public function signatures.
Activated when DATA_BACKEND=postgres in .env.

Connection uses DATABASE_URL from environment, with a connection pool
managed by psycopg3.
"""

import os
import logging
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- Database connection ---
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.critical("❌ Missing DATABASE_URL in .env")
    raise EnvironmentError("Missing DATABASE_URL in .env")

# Connection pool - lazy-initialized on first use
_pool = None


def _get_pool():
    """Lazy-init a connection pool (import psycopg only when Postgres backend is active)."""
    global _pool
    if _pool is None:
        import psycopg_pool  # type: ignore

        _pool = psycopg_pool.ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            open=True,
            kwargs={"autocommit": True},
        )
        logger.info("✅ Postgres connection pool initialized")
    return _pool


def _coerce_jsonb(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
    return {col: row[idx] for idx, col in enumerate(columns)}


def _checkin_fields_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("name"),
        "email": row.get("email"),
        "telephone": row.get("telephone"),
        "tag": row.get("tag"),
        "payment_amount": row.get("payment_amount"),
        "payment_expected": row.get("payment_expected"),
        "payment_valid": row.get("payment_valid"),
        "member": row.get("member"),
        "startgg": row.get("startgg"),
        "is_guest": row.get("is_guest"),
        "status": row.get("status"),
        "tournament_games_registered": row.get("tournament_games_registered"),
        "UUID": row.get("checkin_uuid"),
        "event_slug": row.get("event_slug"),
        "startgg_event_id": row.get("startgg_event_id"),
        "external_id": row.get("external_id"),
    }


def _settings_value(key: str, value: Any) -> Any:
    if key == "events_json":
        from psycopg.types.json import Json  # type: ignore

        return Json(value) if value is not None else None
    return value


# Session timeouts (same as airtable_api.py)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)
SESSION_IDLE_TIMEOUT = timedelta(hours=2)


# =============================================
# Requirement helpers
# =============================================
def compute_requirements(settings: Dict[str, Any]) -> Dict[str, bool]:
    """
    Compute which requirements are active based on settings.

    Postgres stores booleans natively (no Airtable checkbox semantics),
    but we keep the same None-safe logic for consistency.
    """
    return {
        "require_payment": settings.get("require_payment") is True,
        "require_membership": settings.get("require_membership") is True,
        "require_startgg": settings.get("require_startgg") is True,
    }


# =============================================
# Settings
# =============================================
def get_active_settings() -> Optional[Dict[str, Any]]:
    """Return fields from the active settings row (is_active = true)."""
    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM settings
                WHERE is_active = true
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        logger.warning("⚠️ No active settings row found.")
        return None

    data = _row_to_dict(columns, row)
    data.pop("id", None)
    return data


def get_active_slug() -> Optional[str]:
    """Return active_event_slug from the active settings row."""
    settings = get_active_settings() or {}
    slug = settings.get("active_event_slug")
    if slug:
        logger.info(f"🎯 Active slug: {slug}")
    else:
        logger.warning("⚠️ active_event_slug missing on active settings row.")
    return slug


def get_active_settings_with_id() -> Optional[Dict[str, Any]]:
    """Return the active settings row with its record_id included."""
    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM settings
                WHERE is_active = true
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        logger.warning("⚠️ No active settings row found.")
        return None

    data = _row_to_dict(columns, row)
    record_id = data.pop("id", None)
    return {"record_id": str(record_id) if record_id is not None else None, "fields": data}


def update_settings(record_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update fields on a settings record."""
    if not record_id:
        return None

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return None

    columns = []
    values: List[Any] = []
    for key, value in (fields or {}).items():
        columns.append(f"{key} = %s")
        values.append(_settings_value(key, value))

    if not columns:
        return None

    values.append(record_id)

    columns_out = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE settings SET {', '.join(columns)} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
            if row:
                columns_out = [desc[0] for desc in cur.description]

    if not row or not columns_out:
        return None

    data = _row_to_dict(columns_out, row)
    return {"record_id": str(data.get("id")), "fields": {k: v for k, v in data.items() if k != "id"}}


# =============================================
# Checkins (active_event_data)
# =============================================
def get_checkins(
    slug: Optional[str] = None, include_all: bool = False
) -> List[Dict[str, Any]]:  # type: ignore[assignment]
    """Return check-ins for a given event_slug from active_event_data."""
    if not slug and not include_all:
        return []

    params: List[Any] = []
    where_sql = ""
    if not include_all:
        where_sql = "WHERE event_slug = %s"
        params.append(slug)

    query = f"""
        SELECT record_id, created, event_slug, status, member, startgg, is_guest,
               payment_amount, payment_expected, payment_valid,
               name, email, tag, telephone,
               tournament_games_registered, checkin_uuid, startgg_event_id, external_id
        FROM active_event_data
        {where_sql}
        ORDER BY created DESC
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        (
            record_id,
            created,
            event_slug,
            status,
            member,
            startgg,
            is_guest,
            payment_amount,
            payment_expected,
            payment_valid,
            name,
            email,
            tag,
            telephone,
            tournament_games_registered,
            checkin_uuid,
            startgg_event_id,
            external_id,
        ) = row

        result.append(
            {
                "record_id": record_id,
                "created": created.isoformat() if created else None,
                "event_slug": event_slug,
                "status": status,
                "member": member,
                "startgg": startgg,
                "is_guest": is_guest,
                "payment_amount": payment_amount,
                "payment_expected": payment_expected,
                "payment_valid": payment_valid,
                "name": name,
                "email": email,
                "tag": tag,
                "telephone": telephone,
                "tournament_games_registered": tournament_games_registered,
                "UUID": checkin_uuid,
                "startgg_event_id": startgg_event_id,
                "external_id": external_id,
            }
        )

    if include_all:
        logger.info(f"📥 Found {len(result)} checkins (ALL events)")
    else:
        logger.info(f"📥 Found {len(result)} checkins for slug '{slug}'")
    return result


def get_all_event_slugs() -> List[str]:
    """Collect unique event_slug values from active_event_data + settings."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_slug FROM active_event_data WHERE event_slug IS NOT NULL")
            rows = cur.fetchall()

    slugs = {row[0] for row in rows if row and row[0]}

    active = get_active_slug()
    if active:
        slugs.add(active)

    out = sorted(slugs)
    logger.info(f"📚 Retrieved {len(out)} unique event slugs (including active fallback).")
    return out


def get_checkin_by_name(name: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find a checkin record by name (case-insensitive)."""
    if not name:
        return None

    params: List[Any] = [name]
    where_sql = "LOWER(name) = LOWER(%s)"

    if slug:
        where_sql += " AND event_slug = %s"
        params.append(slug)

    query = f"""
        SELECT record_id, name, tag, email, telephone, status, member, startgg,
               payment_valid, payment_amount, payment_expected,
               tournament_games_registered, checkin_uuid, event_slug,
               startgg_event_id, external_id, is_guest, created
        FROM active_event_data
        WHERE {where_sql}
        ORDER BY created DESC
        LIMIT 1
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

    if not row:
        return None

    columns = [
        "record_id",
        "name",
        "tag",
        "email",
        "telephone",
        "status",
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "checkin_uuid",
        "event_slug",
        "startgg_event_id",
        "external_id",
        "is_guest",
        "created",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def get_checkin_by_tag(tag: str, slug: str) -> Optional[Dict[str, Any]]:
    """Find a checkin record by tag + event_slug (case-insensitive tag match)."""
    if not tag or not slug:
        return None

    query = """
        SELECT record_id, name, tag, email, telephone, status, member, startgg,
               payment_valid, payment_amount, payment_expected,
               tournament_games_registered, checkin_uuid, event_slug,
               startgg_event_id, external_id, is_guest, created
        FROM active_event_data
        WHERE LOWER(tag) = LOWER(%s) AND event_slug = %s
        ORDER BY created DESC
        LIMIT 1
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (tag, slug))
            row = cur.fetchone()

    if not row:
        return None

    columns = [
        "record_id",
        "name",
        "tag",
        "email",
        "telephone",
        "status",
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "checkin_uuid",
        "event_slug",
        "startgg_event_id",
        "external_id",
        "is_guest",
        "created",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def update_checkin(
    record_id: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Update fields on a checkin record."""
    if not record_id:
        return None

    update_fields = {}
    for key, value in (fields or {}).items():
        if key == "UUID":
            update_fields["checkin_uuid"] = value
        else:
            update_fields[key] = value

    if not update_fields:
        return None

    set_sql = ", ".join([f"{k} = %s" for k in update_fields.keys()])
    params = list(update_fields.values())
    params.append(record_id)

    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE active_event_data SET {set_sql} WHERE record_id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        return None

    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def delete_checkin(record_id: str) -> bool:
    """Delete a checkin record."""
    if not record_id:
        return False

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_event_data WHERE record_id = %s", (record_id,))
            return cur.rowcount > 0


# =============================================
# Players
# =============================================
def get_players() -> List[Dict[str, Any]]:
    """Return all player profiles."""
    raise NotImplementedError("Postgres: get_players")


# =============================================
# Event history / archive
# =============================================
def get_event_history() -> List[Dict[str, Any]]:
    """Return archived event rows."""
    raise NotImplementedError("Postgres: get_event_history")


def get_event_history_dashboard() -> List[Dict[str, Any]]:
    """Return event history for dashboard view."""
    raise NotImplementedError("Postgres: get_event_history_dashboard")


# =============================================
# Sessions
# =============================================
def create_session(user_info: dict, access_token: str) -> Optional[str]:
    """Create a new session and return the session_id."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    user_info = user_info or {}

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    user_id,
                    user_name,
                    user_email,
                    access_token,
                    created_at,
                    expires_at,
                    last_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    str(user_info.get("id", "")),
                    user_info.get("name", ""),
                    user_info.get("email", ""),
                    access_token,
                    now,
                    now + SESSION_ABSOLUTE_TIMEOUT,
                    now,
                ),
            )

    logger.info(f"🔐 Created session for user '{user_info.get('name')}' ({session_id[:8]}...)")
    return session_id


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve and validate a session (checks absolute + idle timeout)."""
    if not session_id:
        return None

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, session_id, user_id, user_name, user_email, access_token,
                       created_at, expires_at, last_active
                FROM sessions
                WHERE session_id = %s
                LIMIT 1
                """,
                (session_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    (
        record_id,
        session_id,
        user_id,
        user_name,
        user_email,
        access_token,
        created_at,
        expires_at,
        last_active,
    ) = row

    now = datetime.now(timezone.utc)

    if expires_at and now > expires_at:
        logger.info(f"🔒 Session {session_id[:8]}... expired (absolute timeout)")
        delete_session(session_id)
        return None

    if last_active and now - last_active > SESSION_IDLE_TIMEOUT:
        logger.info(f"🔒 Session {session_id[:8]}... expired (idle timeout)")
        delete_session(session_id)
        return None

    return {
        "_record_id": record_id,
        "session_id": session_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "access_token": access_token,
        "created_at": created_at.isoformat() if created_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "last_active": last_active.isoformat() if last_active else None,
    }


def delete_session(session_id: str) -> bool:
    """Delete a session (logout)."""
    if not session_id:
        return False

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            return cur.rowcount > 0


def update_session_activity(session_id: str) -> None:
    """Touch the last_active timestamp on a session."""
    if not session_id:
        return

    now = datetime.now(timezone.utc)
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_active = %s WHERE session_id = %s",
                (now, session_id),
            )


def cleanup_expired_sessions() -> int:
    """Delete all sessions past their absolute expiry. Returns count deleted."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < now()")
            deleted = cur.rowcount

    if deleted:
        logger.info(f"🧹 Cleaned up {deleted} expired sessions")
    return deleted


# =============================================
# Audit Log
# =============================================
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
    """Write an entry to the audit log. Returns record ID or None."""
    now = datetime.now(timezone.utc)
    user = user or {}

    fields = {
        "timestamp": now,
        "user_id": user.get("user_id", ""),
        "user_name": user.get("user_name", "system"),
        "user_email": user.get("user_email", ""),
        "action": action,
        "target_table": target_table,
        "target_event": target_event or None,
        "target_record": target_record or None,
        "target_player": target_player or None,
        "reason": reason or None,
        "details": details or None,
        "before_state": _coerce_jsonb(before_state),
        "after_state": _coerce_jsonb(after_state),
    }

    columns = [k for k, v in fields.items() if v is not None]
    values = [fields[k] for k in columns]

    from psycopg.types.json import Json  # type: ignore

    values = [Json(v) if isinstance(v, (dict, list)) else v for v in values]

    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(columns)

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO audit_log ({col_sql}) VALUES ({placeholders}) RETURNING id",
                values,
            )
            row = cur.fetchone()

    if row:
        record_id = str(row[0])
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
    """Retrieve audit log entries with optional filters, newest first."""
    conditions = []
    params: List[Any] = []

    if action:
        conditions.append("action = %s")
        params.append(action)
    if target_event:
        conditions.append("target_event = %s")
        params.append(target_event)
    if user_id:
        conditions.append("user_id = %s")
        params.append(user_id)

    where_sql = ""
    if conditions:
        where_sql = "WHERE " + " AND ".join(conditions)

    params.append(limit)

    query = f"""
        SELECT id, timestamp, user_id, user_name, user_email, action,
               target_table, target_event, target_record, target_player,
               reason, details, before_state, after_state
        FROM audit_log
        {where_sql}
        ORDER BY timestamp DESC
        LIMIT %s
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result = []
    for row in rows:
        (
            record_id,
            timestamp,
            row_user_id,
            row_user_name,
            row_user_email,
            row_action,
            row_target_table,
            row_target_event,
            row_target_record,
            row_target_player,
            row_reason,
            row_details,
            row_before_state,
            row_after_state,
        ) = row

        result.append(
            {
                "id": record_id,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "user_id": row_user_id,
                "user_name": row_user_name,
                "user_email": row_user_email,
                "action": row_action,
                "target_table": row_target_table,
                "target_event": row_target_event,
                "target_record": row_target_record,
                "target_player": row_target_player,
                "reason": row_reason,
                "details": row_details,
                "before_state": row_before_state,
                "after_state": row_after_state,
            }
        )

    logger.info(f"📋 Retrieved {len(result)} audit log entries")
    return result

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
    raise NotImplementedError("Postgres: get_active_settings")


def get_active_slug() -> Optional[str]:
    """Return active_event_slug from the active settings row."""
    raise NotImplementedError("Postgres: get_active_slug")


def get_active_settings_with_id() -> Optional[Dict[str, Any]]:
    """Return the active settings row with its record_id included."""
    raise NotImplementedError("Postgres: get_active_settings_with_id")


def update_settings(record_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update fields on a settings record."""
    raise NotImplementedError("Postgres: update_settings")


# =============================================
# Checkins (active_event_data)
# =============================================
def get_checkins(
    slug: Optional[str] = None, include_all: bool = False
) -> List[Dict[str, Any]]:  # type: ignore[assignment]
    """Return check-ins for a given event_slug from active_event_data."""
    raise NotImplementedError("Postgres: get_checkins")


def get_all_event_slugs() -> List[str]:
    """Collect unique event_slug values from active_event_data + settings."""
    raise NotImplementedError("Postgres: get_all_event_slugs")


def get_checkin_by_name(name: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find a checkin record by name (case-insensitive)."""
    raise NotImplementedError("Postgres: get_checkin_by_name")


def get_checkin_by_tag(tag: str, slug: str) -> Optional[Dict[str, Any]]:
    """Find a checkin record by tag + event_slug (case-insensitive tag match)."""
    raise NotImplementedError("Postgres: get_checkin_by_tag")


def update_checkin(
    record_id: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Update fields on a checkin record."""
    raise NotImplementedError("Postgres: update_checkin")


def delete_checkin(record_id: str) -> bool:
    """Delete a checkin record."""
    raise NotImplementedError("Postgres: delete_checkin")


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

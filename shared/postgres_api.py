"""
Postgres storage backend for FGC Check-in System.

Drop-in replacement for airtable_api.py - same public function signatures.
Activated when DATA_BACKEND=postgres in .env.

Connection uses DATABASE_URL from environment, with a connection pool
managed by psycopg3.
"""

import os
import logging
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
        import psycopg_pool

        _pool = psycopg_pool.ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            open=True,
        )
        logger.info("✅ Postgres connection pool initialized")
    return _pool


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
def get_checkins(slug: str = None, include_all: bool = False) -> List[Dict[str, Any]]:
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
    raise NotImplementedError("Postgres: create_session")


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve and validate a session (checks absolute + idle timeout)."""
    raise NotImplementedError("Postgres: get_session")


def delete_session(session_id: str) -> bool:
    """Delete a session (logout)."""
    raise NotImplementedError("Postgres: delete_session")


def update_session_activity(session_id: str) -> None:
    """Touch the last_active timestamp on a session."""
    raise NotImplementedError("Postgres: update_session_activity")


def cleanup_expired_sessions() -> int:
    """Delete all sessions past their absolute expiry. Returns count deleted."""
    raise NotImplementedError("Postgres: cleanup_expired_sessions")


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
    raise NotImplementedError("Postgres: log_action")


def get_audit_log(
    *,
    action: Optional[str] = None,
    target_event: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve audit log entries with optional filters, newest first."""
    raise NotImplementedError("Postgres: get_audit_log")

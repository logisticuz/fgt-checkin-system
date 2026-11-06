"""Trusted dashboard identity from Flask's request inside the FastAPI WSGI mount.

DEV_TOOLS_ALLOWED_IDENTITIES contains server-configured user IDs only. Display names,
email addresses and the old display-name defaults never grant dev privileges. There is
no implicit ENV=dev/no-session identity: missing or invalid sessions stay unauthenticated.
The result is cached only in Flask g for the lifetime of the current request.
"""

import logging
import os

from flask import g, has_request_context, request

logger = logging.getLogger(__name__)
SESSION_COOKIE_NAME = "fgc_session"


def current_user() -> dict | None:
    """Return a minimal trusted user, never a session token or a client-store value."""
    if not has_request_context():
        return None
    if hasattr(g, "_fgt_dashboard_user"):
        return g._fgt_dashboard_user
    g._fgt_dashboard_user = None
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    try:
        from shared.storage import get_session

        session = get_session(session_id)
    except Exception:
        logger.warning("Dashboard session lookup failed")
        return None
    if not isinstance(session, dict):
        return None
    user_id = str(session.get("user_id") or "").strip()
    if not user_id:
        return None
    allowed_ids = {
        value.strip()
        for value in os.getenv("DEV_TOOLS_ALLOWED_IDENTITIES", "").split(",")
        if value.strip()
    }
    email = session.get("user_email") or ""
    g._fgt_dashboard_user = {
        "user_id": user_id,
        "user_name": session.get("user_name") or email or f"user-{user_id}",
        "email": email,
        "is_dev": user_id in allowed_ids,
    }
    return g._fgt_dashboard_user

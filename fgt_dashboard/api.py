# api.py
"""
FastAPI wrapper for the FGC Dashboard.

Mounts the Dash app via WSGI and provides:
- /auth/* endpoints for Start.gg OAuth login
- /health endpoint for container health checks

IMPORTANT: All FastAPI routes must be defined BEFORE the Dash WSGI mount,
since the mount at "/" catches all remaining requests.
"""

import os
import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.wsgi import WSGIMiddleware

from app import app as dash_app
from auth import build_authorize_url, exchange_code_for_token, get_startgg_user
from shared.storage import (
    create_session,
    get_session,
    delete_session,
    update_session_activity,
    cleanup_expired_sessions,
)

logger = logging.getLogger(__name__)

# --- Config ---
IS_PROD = os.getenv("ENV", "dev") == "prod"
OAUTH_REDIRECT_URI = os.getenv("STARTGG_REDIRECT_URI", "")
SESSION_COOKIE_NAME = "fgc_session"

# Create FastAPI app
app = FastAPI()


# =============================================
# Auth routes (MUST be before Dash WSGI mount)
# =============================================

@app.get("/auth/login")
async def auth_login():
    """Redirect user to Start.gg OAuth authorization page."""
    if not OAUTH_REDIRECT_URI:
        return JSONResponse(
            {"error": "STARTGG_REDIRECT_URI not configured"},
            status_code=500,
        )
    try:
        url = build_authorize_url(OAUTH_REDIRECT_URI)
        return RedirectResponse(url=url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/auth/callback")
async def auth_callback(code: str = ""):
    """
    Handle OAuth callback from Start.gg.

    Flow: exchange code -> fetch user info -> create session -> set cookie -> redirect
    """
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    try:
        # Step 1: Exchange code for access token
        token_data = exchange_code_for_token(code, OAUTH_REDIRECT_URI)
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"Token exchange returned no access_token: {token_data}")
            return JSONResponse({"error": "Token exchange failed"}, status_code=500)

        # Step 2: Get user info from Start.gg
        user_info = get_startgg_user(access_token)
        if not user_info.get("id"):
            logger.error("Could not retrieve user info from Start.gg")
            return JSONResponse({"error": "Could not retrieve user info"}, status_code=500)

        # Step 3: Create session in Airtable
        session_id = create_session(user_info, access_token)
        if not session_id:
            return JSONResponse({"error": "Session creation failed"}, status_code=500)

        # Step 4: Set cookie and redirect to dashboard
        # Dev: /admin/ (nginx prefix), Prod: / (own domain)
        redirect_path = "/" if IS_PROD else "/admin/"
        response = RedirectResponse(url=redirect_path, status_code=302)

        cookie_path = "/" if IS_PROD else "/admin/"
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            secure=IS_PROD,
            samesite="lax",
            max_age=8 * 60 * 60,  # 8 hours
            path=cookie_path,
        )

        logger.info(f"✅ User '{user_info.get('name')}' logged in successfully")
        return response

    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Authentication failed: {str(e)}"},
            status_code=500,
        )


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Delete session and clear cookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(session_id)

    redirect_path = "/" if IS_PROD else "/admin/"
    cookie_path = "/" if IS_PROD else "/admin/"
    response = RedirectResponse(url=redirect_path, status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path=cookie_path)
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    """
    Return current user info as JSON, or 401 if not authenticated.

    Called by frontend to check login status and display user info.
    Also touches last_active to keep the session alive.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session_data = get_session(session_id) if session_id else None

    if not session_data:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Update activity timestamp (once per page load via this endpoint)
    update_session_activity(session_id)

    return {
        "user_id": session_data.get("user_id"),
        "user_name": session_data.get("user_name"),
        "user_email": session_data.get("user_email"),
    }


# =============================================
# Health check
# =============================================

@app.get("/health")
def health_check():
    """Health endpoint for the dashboard container."""
    try:
        dash_ok = dash_app is not None and dash_app.server is not None
    except Exception:
        dash_ok = False

    return {
        "status": "ok" if dash_ok else "degraded",
        "components": {"fastapi": True, "dash": dash_ok},
        "version": "1.1.0",
    }


# =============================================
# Startup: cleanup expired sessions
# =============================================

@app.on_event("startup")
async def startup_cleanup():
    """Clean up expired sessions on container start."""
    try:
        cleaned = cleanup_expired_sessions()
        if cleaned:
            logger.info(f"Startup: cleaned {cleaned} expired sessions")
    except Exception as e:
        logger.warning(f"Startup session cleanup failed (sessions table may not exist yet): {e}")


# =============================================
# Mount Dash LAST - catches all remaining routes
# =============================================
app.mount("/", WSGIMiddleware(dash_app.server))

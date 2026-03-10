"""
Start.gg OAuth helpers for FGC Check-in System.

Pure functions with no FastAPI dependencies - importable from both
the backend and dashboard containers via shared/.

Uses httpx for Start.gg API calls (sync Client for simplicity).
"""

import os
import logging
from urllib.parse import urlencode
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# --- Config (from environment) ---
STARTGG_CLIENT_ID = os.getenv("STARTGG_CLIENT_ID", "")
STARTGG_CLIENT_SECRET = os.getenv("STARTGG_CLIENT_SECRET", "")

# Start.gg OAuth endpoints
STARTGG_AUTH_URL = "https://start.gg/oauth/authorize"
STARTGG_TOKEN_URL = "https://api.start.gg/oauth/access_token"
STARTGG_GRAPHQL_URL = "https://api.start.gg/gql/alpha"

# Scopes: identity + email for audit trail, tournament.manager for admin check
OAUTH_SCOPES = "user.identity user.email tournament.manager"

DEFAULT_TIMEOUT = 10.0


def build_authorize_url(redirect_uri: str) -> str:
    """
    Build the Start.gg OAuth authorization URL.

    The user's browser is redirected here. After approval, Start.gg
    redirects back to redirect_uri with ?code=xxx.

    Args:
        redirect_uri: Callback URL (must match registered redirect URI exactly)

    Returns:
        Full authorization URL string

    Raises:
        ValueError: If STARTGG_CLIENT_ID is not configured
    """
    if not STARTGG_CLIENT_ID:
        raise ValueError("STARTGG_CLIENT_ID not configured in environment")

    params = {
        "client_id": STARTGG_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
    }
    return f"{STARTGG_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    """
    Exchange an authorization code for an access token.

    Second step of OAuth flow - called from the callback endpoint
    after Start.gg redirects back with a code.

    Args:
        code: Authorization code from Start.gg callback
        redirect_uri: Must match the redirect_uri used in build_authorize_url

    Returns:
        Dict with access_token, token_type, expires_in, etc.

    Raises:
        ValueError: If client credentials are not configured
        httpx.HTTPStatusError: If token exchange fails
    """
    if not STARTGG_CLIENT_ID or not STARTGG_CLIENT_SECRET:
        raise ValueError("Start.gg OAuth credentials not configured")

    # Start.gg expects form-encoded POST (not JSON)
    payload = {
        "client_id": STARTGG_CLIENT_ID,
        "client_secret": STARTGG_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }

    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(
            STARTGG_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


def get_startgg_user(access_token: str) -> dict:
    """
    Fetch the current user's info from Start.gg GraphQL API.

    Args:
        access_token: OAuth access token from exchange_code_for_token

    Returns:
        Dict with: id, slug, name, email, avatar_url
        Empty dict if the API call fails.
    """
    query = {
        "query": """
            query CurrentUser {
                currentUser {
                    id
                    slug
                    name
                    email
                    player {
                        gamerTag
                    }
                    images(type: "profile") {
                        url
                    }
                }
            }
        """
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(STARTGG_GRAPHQL_URL, json=query, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        if "errors" in data:
            logger.error(f"Start.gg GraphQL errors: {data['errors']}")
            return {}

        user = data.get("data", {}).get("currentUser") or {}
        images = user.get("images") or []
        avatar_url = images[0].get("url") if images else None
        gamer_tag = (user.get("player") or {}).get("gamerTag", "")
        display_name = (
            user.get("name")
            or gamer_tag
            or user.get("email")
            or user.get("slug")
            or (f"user-{user.get('id')}" if user.get("id") else "unknown")
        )

        return {
            "id": str(user.get("id", "")),
            "slug": user.get("slug", ""),
            "name": display_name,
            "gamer_tag": gamer_tag,
            "email": user.get("email", ""),
            "avatar_url": avatar_url,
        }

    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch Start.gg user: {e}")
        return {}


def is_event_admin(access_token: str, event_slug: str) -> bool:
    """
    Check if the authenticated user is an admin of the given tournament.

    Queries Start.gg for tournament admins and compares against the
    current user's ID.

    Args:
        access_token: OAuth access token
        event_slug: Tournament slug (e.g., "fight-night-17-at-backstage-rockbar...")

    Returns:
        True if user is a tournament admin, False otherwise.

    Note: For Phase 1 this is informational. The primary auth gate is
    having a valid Start.gg account. TO-level checks can be tightened later.
    """
    allowed, _reason = check_event_admin(access_token, event_slug)
    return allowed is True


def check_event_admin(access_token: str, event_slug: str) -> Tuple[Optional[bool], str]:
    """
    Check tournament admin access with explicit verification status.

    Returns:
        (True, "ok")               -> verified TO/Admin
        (False, <reason>)          -> verified not admin / invalid slug
        (None, <reason>)           -> could not verify now (timeout/network/API)
    """
    # First get the current user's ID
    user = get_startgg_user(access_token)
    if not user.get("id"):
        return False, "user_lookup_failed"

    query = {
        "query": """
            query TournamentAdmin($slug: String!) {
                tournament(slug: $slug) {
                    id
                    name
                    admins {
                        id
                    }
                }
            }
        """,
        "variables": {"slug": event_slug},
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(STARTGG_GRAPHQL_URL, json=query, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        if "errors" in data:
            logger.warning(f"Start.gg admin check errors: {data['errors']}")
            return None, "graphql_error"

        tournament = data.get("data", {}).get("tournament")
        if not tournament:
            return False, "tournament_not_found"

        admin_ids = {str(a.get("id")) for a in (tournament.get("admins") or [])}
        return (user["id"] in admin_ids), "ok"

    except httpx.TimeoutException as e:
        logger.warning(f"Timeout during Start.gg admin check: {e}")
        return None, "timeout"

    except httpx.HTTPError as e:
        logger.error(f"Failed to check tournament admin status: {e}")
        return None, "http_error"

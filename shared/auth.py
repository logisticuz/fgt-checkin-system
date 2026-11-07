"""
Start.gg OAuth helpers for FGC Check-in System.

Pure functions with no FastAPI dependencies - importable from both
the backend and dashboard containers via shared/.

Uses httpx for Start.gg API calls (sync Client for simplicity).
"""

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Optional, Tuple
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

# --- Config (from environment) ---
STARTGG_CLIENT_ID = os.getenv("STARTGG_CLIENT_ID", "")
STARTGG_CLIENT_SECRET = os.getenv("STARTGG_CLIENT_SECRET", "")

OAUTH_STATE_COOKIE_NAME = "fgc_oauth_state"
OAUTH_STATE_MAX_AGE = 10 * 60
_STATE_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
_STATE_KEY_CONTEXT = b"fgt-oauth-state-v1"
if not STARTGG_CLIENT_SECRET.strip():
    logger.error("oauth_state_config_missing: STARTGG_CLIENT_SECRET must be configured for login")

# Start.gg OAuth endpoints
STARTGG_AUTH_URL = "https://www.start.gg/oauth/authorize"
STARTGG_TOKEN_URL = "https://api.start.gg/oauth/access_token"
STARTGG_GRAPHQL_URL = "https://api.start.gg/gql/alpha"

# Scopes: identity + email for audit trail, tournament.manager for admin check
OAUTH_SCOPES = "user.identity user.email tournament.manager"

DEFAULT_TIMEOUT = 10.0


def oauth_state_cookie_name(*, secure: bool) -> str:
    """Use a host-bound cookie whenever the existing secure-cookie policy applies."""
    return "__Host-fgt_oauth_state" if secure else OAUTH_STATE_COOKIE_NAME


def _state_signature(state: str, issued_at: str) -> str:
    if not STARTGG_CLIENT_SECRET.strip():
        raise ValueError("OAuth state signing secret is not configured")
    key = hmac.new(
        STARTGG_CLIENT_SECRET.encode("utf-8"), _STATE_KEY_CONTEXT, hashlib.sha256
    ).digest()
    return hmac.new(key, f"{state}|{issued_at}".encode("ascii"), hashlib.sha256).hexdigest()


def seal_oauth_state(state: str) -> str:
    """Bind a browser challenge to a signed issue time, shared across dashboard workers."""
    if not isinstance(state, str) or _STATE_FORMAT.fullmatch(state) is None:
        raise ValueError("Invalid OAuth state")
    issued_at = str(int(time.time()))
    return f"{state}.{issued_at}.{_state_signature(state, issued_at)}"


def verify_oauth_state(cookie: str | None, state: str | None) -> bool:
    """Verify browser binding and ten-minute expiry without per-worker state."""
    if not isinstance(cookie, str) or len(cookie) > 256 or not isinstance(state, str):
        return False
    if _STATE_FORMAT.fullmatch(state) is None:
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    signed_state, issued_at, signature = parts
    if (
        _STATE_FORMAT.fullmatch(signed_state) is None
        or re.fullmatch(r"[0-9]{1,12}", issued_at) is None
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
    ):
        return False
    try:
        expected = _state_signature(signed_state, issued_at)
    except ValueError:
        return False
    if not hmac.compare_digest(signature, expected):
        return False
    if not hmac.compare_digest(state, signed_state):
        return False
    age = int(time.time()) - int(issued_at)
    return 0 <= age < OAUTH_STATE_MAX_AGE


def build_authorize_url(redirect_uri: str, *, state: str) -> str:
    """
    Build the Start.gg OAuth authorization URL.

    The user's browser is redirected here. After approval, Start.gg
    redirects back to redirect_uri with ?code=xxx.

    Args:
        redirect_uri: Callback URL (must match registered redirect URI exactly)
        state: Browser-bound login challenge returned by Start.gg

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
        "state": state,
    }
    return f"{STARTGG_AUTH_URL}?{urlencode(params, quote_via=quote)}"


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

    # Preserve the production hotfix: exchange as JSON and include the configured scopes.
    payload = {
        "client_id": STARTGG_CLIENT_ID,
        "client_secret": STARTGG_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
        "scope": OAUTH_SCOPES,
    }

    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(
            STARTGG_TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
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

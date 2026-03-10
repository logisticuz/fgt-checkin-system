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
import json

import html as html_mod

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from starlette.middleware.wsgi import WSGIMiddleware

from app import app as dash_app
from auth import build_authorize_url, exchange_code_for_token, get_startgg_user, is_event_admin
from urllib.parse import urlparse
import re
from shared.storage import (
    create_session,
    get_session,
    delete_session,
    update_session_activity,
    cleanup_expired_sessions,
    get_active_slug,
    get_all_event_slugs,
    get_active_settings_with_id,
    update_settings,
    log_action,
)
from shared.postgres_api import reopen_event

logger = logging.getLogger(__name__)


def _display_name(name: str = "", email: str = "", user_id: str = "") -> str:
    """Normalize display name for UI/audit; never return blank."""
    return (name or email or (f"user-{user_id}" if user_id else "unknown")).strip()


def _audit_safe(user: dict, action: str, target_table: str, **kwargs) -> None:
    """Best-effort audit logging; never break auth flow on audit failure."""
    try:
        log_action(user or {}, action, target_table, **kwargs)
    except Exception as e:
        logger.warning(f"Audit write failed for {action}: {e}")

# --- Config ---
IS_PROD = os.getenv("ENV", "dev") == "prod"
OAUTH_REDIRECT_URI = os.getenv("STARTGG_REDIRECT_URI", "")
SESSION_COOKIE_NAME = "fgc_session"

# Create FastAPI app
app = FastAPI()


def _render_waiting_redirect_page(title: str, message: str, next_url: str, delay_ms: int = 1200) -> str:
    """Simple transition page shown after OAuth callback before redirecting."""
    safe_title = title.replace("<", "").replace(">", "")
    safe_message = message.replace("<", "").replace(">", "")
    safe_next = next_url.replace("'", "")
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width,initial-scale=1' />
        <title>{safe_title}</title>
        <style>
          body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background: #0b0f16;
            color: #d7e1ea;
            font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          }}
          .card {{
            width: min(92vw, 480px);
            border: 1px solid #243245;
            border-radius: 12px;
            background: #101826;
            padding: 1.25rem 1.4rem;
            box-shadow: 0 12px 40px rgba(0,0,0,0.35);
          }}
          h1 {{ margin: 0 0 0.5rem 0; font-size: 1.05rem; color: #66e3a1; }}
          p {{ margin: 0; line-height: 1.45; color: #a9b7c8; }}
        </style>
      </head>
      <body>
        <div class='card'>
          <h1>{safe_title}</h1>
          <p>{safe_message}</p>
        </div>
        <script>
          setTimeout(function() {{ window.location.href = '{safe_next}'; }}, {int(delay_ms)});
        </script>
      </body>
    </html>
    """


def _render_dashboard_landing_page(login_url: str) -> str:
    """Public landing page shown before auth for better dashboard UX."""
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width,initial-scale=1' />
        <title>FGC Dashboard</title>
        <style>
          body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background:
              radial-gradient(900px 500px at 10% -10%, rgba(0, 212, 255, 0.18), transparent 70%),
              radial-gradient(900px 500px at 100% 0%, rgba(16, 185, 129, 0.14), transparent 70%),
              linear-gradient(180deg, #0b1018 0%, #070b12 100%);
            color: #d8e6f2;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
          }}
          .shell {{
            width: min(92vw, 620px);
            display: flex;
            flex-direction: column;
            align-items: center;
          }}
          .card {{
            width: min(92vw, 620px);
            border: 1px solid #253a52;
            border-radius: 22px;
            background: linear-gradient(160deg, rgba(16, 24, 38, 0.92) 0%, rgba(13, 21, 32, 0.94) 100%);
            padding: 2.5rem 2.5rem 2rem;
            box-shadow: 0 18px 60px rgba(0,0,0,0.42), 0 0 60px rgba(0, 212, 255, 0.07);
            backdrop-filter: blur(6px);
            animation: cardGlow 4s ease-in-out infinite;
          }}
          @keyframes cardGlow {{
            0%   {{ box-shadow: 0 18px 60px rgba(0,0,0,0.42), 0 0 40px rgba(0, 212, 255, 0.06), 0 0 80px rgba(0, 212, 255, 0.03); }}
            50%  {{ box-shadow: 0 18px 60px rgba(0,0,0,0.42), 0 0 60px rgba(0, 212, 255, 0.13), 0 0 120px rgba(0, 212, 255, 0.06); }}
            100% {{ box-shadow: 0 18px 60px rgba(0,0,0,0.42), 0 0 40px rgba(0, 212, 255, 0.06), 0 0 80px rgba(0, 212, 255, 0.03); }}
          }}
          .card-head {{
            display: grid;
            place-items: center;
            margin-bottom: 1.2rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid transparent;
            background-image: linear-gradient(rgba(16,24,38,0), rgba(16,24,38,0)), linear-gradient(90deg, transparent, #2a4a6a, transparent);
            background-clip: padding-box, border-box;
            background-origin: padding-box, border-box;
          }}
          .card-head img {{
            height: 72px;
            width: auto;
            margin-bottom: 0.3rem;
            filter: drop-shadow(0 0 18px rgba(0, 212, 255, 0.28));
          }}
          h1 {{
            margin: 0 0 0.5rem 0;
            font-size: 1.4rem;
            letter-spacing: 0.14em;
            color: #69dcff;
            text-align: center;
            text-transform: uppercase;
            text-shadow: 0 0 20px rgba(0, 212, 255, 0.25);
          }}
          p {{ margin: 0 0 1rem 0; line-height: 1.5; color: #b8d0e8; text-align: center; font-size: 0.95rem; max-width: 440px; margin-left: auto; margin-right: auto; }}
          .chips {{
            display: flex;
            justify-content: center;
            gap: 0.6rem;
            flex-wrap: wrap;
            margin: 0.75rem 0 1.2rem 0;
          }}
          .chip {{
            border: 1px solid #2a3e57;
            background: #0e1623;
            color: #86a9c9;
            border-radius: 999px;
            padding: 0.3rem 0.8rem;
            font-size: 0.8rem;
            transition: border-color 0.2s;
          }}
          .chip:hover {{
            border-color: #3d5a7a;
          }}
          .actions {{ display: flex; justify-content: center; }}
          .btn {{
            display: inline-block;
            text-decoration: none;
            background: linear-gradient(90deg, #00b8ff, #19d3a2);
            color: #08131d;
            font-weight: 700;
            font-size: 0.95rem;
            border-radius: 12px;
            padding: 0.85rem 2rem;
            box-shadow: 0 10px 24px rgba(0, 195, 255, 0.22);
            transform-origin: center;
            animation: softPulse 2.2s ease-in-out infinite;
            transition: filter 0.2s, transform 0.2s;
          }}
          .btn:hover {{
            animation-play-state: paused;
            filter: brightness(1.12);
            transform: scale(1.04);
          }}
          @keyframes softPulse {{
            0% {{ transform: scale(1); box-shadow: 0 10px 24px rgba(0, 195, 255, 0.20); }}
            50% {{ transform: scale(1.03); box-shadow: 0 14px 30px rgba(0, 195, 255, 0.30); }}
            100% {{ transform: scale(1); box-shadow: 0 10px 24px rgba(0, 195, 255, 0.20); }}
          }}
          .note {{ margin-top: 1.3rem; font-size: 0.82rem; color: #88a2b9; text-align: center; }}
          .imlo-stamp {{
            position: fixed;
            left: 50%;
            bottom: 24px;
            transform: translateX(-50%);
            font-size: 0.78rem;
            letter-spacing: 0.1em;
            color: #6f8ca8;
            text-transform: uppercase;
            opacity: 0.7;
          }}
          .imlo-stamp strong {{ color: #7fd8ff; font-weight: 700; }}
        </style>
      </head>
      <body>
        <div class='shell'>
          <div class='card'>
          <div class='card-head'>
            <img src='/assets/logo.png' alt='FGC Trollhättan'>
          </div>
          <h1>Tournament Ops Portal</h1>
          <p>Check-ins, approvals and audit logs in one place. Built for fast TO flow at the venue.</p>
          <div class='chips'>
            <span class='chip'>Live Check-ins</span>
            <span class='chip'>Audit Trail</span>
            <span class='chip'>Event Controls</span>
          </div>
          <div class='actions'>
            <a class='btn' href='{login_url}'>Login with Start.gg</a>
          </div>
          <p class='note'>Only approved TOs can access this dashboard.</p>
          </div>
        </div>
        <div class='imlo-stamp'>Powered by <strong>IMLO</strong></div>
      </body>
    </html>
    """


@app.middleware("http")
async def require_dashboard_auth(request: Request, call_next):
    """Require authenticated session for dashboard pages and APIs."""
    path = request.url.path

    # Public routes
    if path in {"/auth/login", "/auth/callback", "/health"} or path.startswith("/assets/"):
        return await call_next(request)

    # Selection route requires session but no active event yet
    if path in {"/auth/select-event", "/auth/select-event/save", "/auth/logout", "/auth/me"}:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        session_data = get_session(session_id) if session_id else None
        if not session_data:
            login_path = "/auth/login" if IS_PROD else "/admin/auth/login"
            return RedirectResponse(url=login_path, status_code=302)
        update_session_activity(session_id)
        return await call_next(request)

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session_data = get_session(session_id) if session_id else None

    if not session_data:
        login_path = "/auth/login" if IS_PROD else "/admin/auth/login"
        # Nice UX on dashboard root: show a landing page instead of instant redirect.
        if path == "/":
            return HTMLResponse(_render_dashboard_landing_page(login_path), status_code=200)
        return RedirectResponse(url=login_path, status_code=302)

    # No active event? Redirect browser navigation to select-event page.
    # Skip Dash internal paths (/_dash-*) to avoid redirect loops — Dash
    # AJAX callbacks must be allowed through even without an active slug.
    if (
        not get_active_slug()
        and not path.startswith("/_dash-")
        and path not in {"/auth/select-event", "/auth/select-event/save", "/auth/logout", "/auth/me", "/_favicon.ico"}
    ):
        select_path = "/auth/select-event" if IS_PROD else "/admin/auth/select-event"
        return RedirectResponse(url=select_path, status_code=302)

    update_session_activity(session_id)
    return await call_next(request)


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

        # Step 2.5: Authorize — if an active event exists, verify TO access.
        # If no active event (e.g. after archive + clear), let the user
        # through — they'll set one up via Settings > Fetch Event Data,
        # which verifies TO access at that point.
        active_slug = get_active_slug()
        if active_slug:
            if not is_event_admin(access_token, active_slug):
                _audit_safe(
                    {
                        "user_id": user_info.get("id", ""),
                        "user_name": _display_name(
                            user_info.get("name", ""),
                            user_info.get("email", ""),
                            str(user_info.get("id", "")),
                        ),
                        "user_email": user_info.get("email", ""),
                    },
                    "auth_login_denied",
                    "dashboard_auth",
                    target_event=active_slug,
                    reason="not_event_admin",
                )
                logger.warning(
                    "Denied dashboard login for user '%s' (not TO for active event %s)",
                    _display_name(
                        user_info.get("name", ""),
                        user_info.get("email", ""),
                        str(user_info.get("id", "")),
                    ),
                    active_slug,
                )
                logout_path = "/auth/logout" if IS_PROD else "/admin/auth/logout"
                return HTMLResponse(
                    f"""
                    <h2>Not Authorized</h2>
                    <p>You are not TO/Admin for the active event: <strong>{html_mod.escape(active_slug)}</strong></p>
                    <p>Please contact Head TO to grant permissions.</p>
                    <p><a href='{logout_path}'>Back to login</a></p>
                    """,
                    status_code=403,
                )

        # Step 3: Create session
        session_id = create_session(user_info, access_token)
        if not session_id:
            return JSONResponse({"error": "Session creation failed"}, status_code=500)

        _audit_safe(
            {
                "user_id": user_info.get("id", ""),
                "user_name": _display_name(
                    user_info.get("name", ""),
                    user_info.get("email", ""),
                    str(user_info.get("id", "")),
                ),
                "user_email": user_info.get("email", ""),
            },
            "auth_login_success",
            "dashboard_auth",
            target_event=active_slug or "",
        )

        # Step 4: Set cookie and redirect to dashboard
        # Dev: /admin/ (nginx prefix), Prod: / (own domain)
        redirect_path = "/" if IS_PROD else "/admin/"
        response = HTMLResponse(
            _render_waiting_redirect_page(
                "Start.gg approved",
                "Signing you in and loading the dashboard...",
                redirect_path,
            ),
            status_code=200,
        )

        # Dash internal requests use root paths (/_dash-*) even in dev behind /admin,
        # so cookie path must include "/" to be sent on those requests.
        cookie_path = "/"
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            secure=IS_PROD,
            samesite="lax",
            max_age=8 * 60 * 60,  # 8 hours
            path=cookie_path,
        )
        logger.info(
            "✅ User '%s' logged in successfully",
            _display_name(
                user_info.get("name", ""),
                user_info.get("email", ""),
                str(user_info.get("id", "")),
            ),
        )
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
    session_data = get_session(session_id) if session_id else None
    if session_id:
        delete_session(session_id)

    if session_data:
        _audit_safe(
            {
                "user_id": session_data.get("user_id", ""),
                "user_name": _display_name(
                    session_data.get("user_name", ""),
                    session_data.get("user_email", ""),
                    str(session_data.get("user_id", "")),
                ),
                "user_email": session_data.get("user_email", ""),
            },
            "auth_logout",
            "dashboard_auth",
            target_event=get_active_slug() or "",
        )

    redirect_path = "/" if IS_PROD else "/admin/"
    response = RedirectResponse(url=redirect_path, status_code=302)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/admin/")
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
        "user_name": _display_name(
            session_data.get("user_name", ""),
            session_data.get("user_email", ""),
            str(session_data.get("user_id", "")),
        ),
        "user_email": session_data.get("user_email"),
    }


@app.get("/auth/select-event")
async def auth_select_event(request: Request):
    """Select active event when no active slug is configured yet."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session_data = get_session(session_id) if session_id else None
    if not session_data:
        login_path = "/auth/login" if IS_PROD else "/admin/auth/login"
        return RedirectResponse(url=login_path, status_code=302)

    if get_active_slug():
        return RedirectResponse(url=("/" if IS_PROD else "/admin/"), status_code=302)

    access_token = session_data.get("access_token", "")
    candidate_slugs = [s for s in (get_all_event_slugs() or []) if s and s != "__ALL__"]
    allowed = [s for s in candidate_slugs if is_event_admin(access_token, s)]

    logout_path = "/auth/logout" if IS_PROD else "/admin/auth/logout"
    save_path = "/auth/select-event/save" if IS_PROD else "/admin/auth/select-event/save"

    # Build HTML-safe option lists
    options = "".join(
        [
            (
                f"<option value='{html_mod.escape(slug)}' selected>"
                f"{html_mod.escape(slug)}</option>"
                if idx == 0
                else f"<option value='{html_mod.escape(slug)}'>"
                f"{html_mod.escape(slug)}</option>"
            )
            for idx, slug in enumerate(allowed)
        ]
    )
    datalist = "".join(
        [f"<option value='{html_mod.escape(slug)}'></option>" for slug in allowed]
    )

    # Adapt UI when no known events exist (e.g. after archive + clear)
    has_known_events = bool(allowed)
    if has_known_events:
        subtitle = "Pick one from the list or type a slug manually."
        slug_label = "Manual slug (optional)"
    else:
        subtitle = (
            "No active events found. Enter your Start.gg tournament slug or paste the full URL."
        )
        slug_label = "Tournament slug or Start.gg URL"

    # Hide the dropdown entirely when empty
    dropdown_html = f"""
                <div class='field'>
                  <label>Approved events</label>
                  <select name='event_slug'>
                    {options}
                  </select>
                </div>""" if has_known_events else ""

    page = f"""
        <html>
        <head>
          <meta charset='utf-8' />
          <meta name='viewport' content='width=device-width, initial-scale=1' />
          <title>Select Active Event</title>
          <style>
            :root {{
              --bg:#070b1a;
              --bg-card:#0f172a;
              --border:#1e293b;
              --text:#e2e8f0;
              --muted:#94a3b8;
              --accent:#00d4ff;
              --accent2:#f59e0b;
            }}
            * {{ box-sizing:border-box; }}
            body {{
              margin:0;
              min-height:100vh;
              font-family:'Segoe UI',Tahoma,sans-serif;
              background: radial-gradient(circle at 20% 0%, #121b3b 0%, var(--bg) 42%, #050814 100%);
              color:var(--text);
              display:flex;
              align-items:center;
              justify-content:center;
              padding:1.25rem;
            }}
            .card {{
              width:min(680px,96vw);
              background:linear-gradient(180deg, #0f172a, #0b1328);
              border:1px solid var(--border);
              border-radius:14px;
              padding:1.25rem;
              box-shadow:0 20px 48px rgba(0,0,0,.38);
            }}
            h1 {{ margin:0 0 .5rem 0; font-size:1.45rem; }}
            p {{ margin:.25rem 0 .85rem 0; color:var(--muted); }}
            .row {{ display:flex; gap:.7rem; flex-wrap:wrap; margin-top:.5rem; }}
            .field {{ flex:1 1 260px; }}
            label {{ display:block; margin-bottom:.3rem; color:var(--muted); font-size:.8rem; }}
            select,input {{
              width:100%;
              background:#0b1227;
              color:var(--text);
              border:1px solid #334155;
              border-radius:10px;
              padding:.64rem .7rem;
              font-size:.95rem;
            }}
            button {{
              margin-top:.9rem;
              background:linear-gradient(135deg, var(--accent), #22d3ee);
              color:#03111f;
              border:0;
              border-radius:10px;
              padding:.62rem .9rem;
              font-weight:700;
              cursor:pointer;
            }}
            .logout {{ margin-top:.9rem; display:inline-block; color:var(--muted); }}
            .help {{ font-size:.8rem; color:var(--muted); margin-top:.4rem; }}
          </style>
        </head>
        <body>
          <div class='card'>
            <h1>Select Active Event</h1>
            <p>{subtitle}</p>
            <form method='post' action='{save_path}'>
              <div class='row'>
                {dropdown_html}
                <div class='field'>
                  <label>{slug_label}</label>
                  <input name='event_slug_manual' list='allowed-event-slugs'
                         placeholder='fightbox-3 or https://www.start.gg/tournament/...'
                         autocomplete='off' />
                  <datalist id='allowed-event-slugs'>{datalist}</datalist>
                </div>
              </div>
              <div class='help'>Enter the slug from the Start.gg URL (e.g. &quot;fightbox-3&quot;) or paste the full URL. You must be TO/Admin for the tournament.</div>
              <button type='submit'>Set Active Event</button>
            </form>
            <a class='logout' href='{logout_path}'>Logout</a>
          </div>
        </body>
        </html>
        """

    return HTMLResponse(page)


@app.post("/auth/select-event/save")
async def auth_select_event_save(request: Request):
    """Persist selected active event slug from authorized user's allowed list."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    session_data = get_session(session_id) if session_id else None
    if not session_data:
        login_path = "/auth/login" if IS_PROD else "/admin/auth/login"
        return RedirectResponse(url=login_path, status_code=302)

    try:
        form = await request.form()
    except Exception as exc:
        logger.error("Failed to parse form data in select-event/save: %s", exc)
        form = {}
    selected_slug = str(form.get("event_slug", "")).strip()
    manual_slug = str(form.get("event_slug_manual", "")).strip()

    raw_input = (manual_slug or selected_slug).strip()
    event_slug = raw_input.lower()

    # Accept full Start.gg URLs in manual field, e.g.
    # https://www.start.gg/tournament/fightbox-2/events
    if raw_input.startswith("http://") or raw_input.startswith("https://"):
        try:
            parsed = urlparse(raw_input)
            match = re.search(r"/tournament/([^/]+)", parsed.path or "")
            if match:
                event_slug = match.group(1).strip().lower()
        except Exception:
            pass

    def _render_error_page(message: str) -> str:
        logout_path = "/auth/logout" if IS_PROD else "/admin/auth/logout"
        select_path = "/auth/select-event" if IS_PROD else "/admin/auth/select-event"
        return f"""
        <html><body style='font-family:Segoe UI,Tahoma,sans-serif;background:#070b1a;color:#e2e8f0;padding:1.25rem;'>
          <div style='max-width:620px;margin:0 auto;background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:1rem;'>
            <h3 style='margin:0 0 .5rem 0;'>Could not set active event</h3>
            <p style='color:#f59e0b;margin:0 0 .75rem 0;'>{message}</p>
            <p><a href='{select_path}' style='color:#00d4ff;'>Back to Select Event</a></p>
            <p><a href='{logout_path}' style='color:#94a3b8;'>Logout</a></p>
          </div>
        </body></html>
        """

    access_token = session_data.get("access_token", "")
    candidate_slugs = [s for s in (get_all_event_slugs() or []) if s and s != "__ALL__"]
    allowed = [s for s in candidate_slugs if is_event_admin(access_token, s)]

    has_access = False
    if event_slug:
        if event_slug in allowed:
            has_access = True
        else:
            # Fallback for manual slug not present in local candidate list.
            has_access = is_event_admin(access_token, event_slug)

    if not event_slug or not has_access:
        return HTMLResponse(
            _render_error_page(
                "Invalid event selection or missing TO access. "
                f"Requested: '{event_slug or '-'}'."
            ),
            status_code=403,
        )

    settings_with_id = get_active_settings_with_id() or {}
    record_id = settings_with_id.get("record_id")
    if not record_id:
        return HTMLResponse(_render_error_page("Could not find settings record."), status_code=500)

    old_slug = (settings_with_id.get("fields") or {}).get("active_event_slug", "")
    user_ctx = {
        "user_id": session_data.get("user_id", ""),
        "user_name": _display_name(
            session_data.get("user_name", ""),
            session_data.get("user_email", ""),
            str(session_data.get("user_id", "")),
        ),
        "user_email": session_data.get("user_email", ""),
    }

    # Try to reopen archived event (restores players to active_event_data).
    # If no archived data exists (new event), fall back to just setting the slug.
    reopened = False
    try:
        result = reopen_event(event_slug, restore_active=True, user=user_ctx)
        reopened = True
        logger.info(
            "Auto-reopened archived event '%s' (%d rows restored)",
            event_slug,
            result.get("restored_rows", 0),
        )
    except ValueError:
        # No archived data — new event, just set the slug.
        ok = update_settings(record_id, {"active_event_slug": event_slug})
        if not ok:
            return HTMLResponse(_render_error_page("Failed to set active event."), status_code=500)

    _audit_safe(
        user_ctx,
        "auth_select_active_event",
        "settings",
        target_event=event_slug,
        details=json.dumps(
            {
                "old_active_event_slug": old_slug,
                "new_active_event_slug": event_slug,
                "auto_reopened": reopened,
            }
        ),
    )

    return RedirectResponse(url=("/" if IS_PROD else "/admin/"), status_code=302)


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

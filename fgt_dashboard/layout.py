# layout.py
"""
FGC Dashboard Layout - Modern Esports Theme
"""
import os
import flask
from dash import html, dcc, dash_table
from shared.storage import (
    get_all_event_slugs,
    get_active_slug,
    get_checkins,
    get_active_settings,
    get_event_history,
    get_session,
)
import pandas as pd
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# SSE token for authenticated real-time updates
SSE_TOKEN = os.getenv("SSE_TOKEN", "")
SESSION_COOKIE_NAME = "fgc_session"
IS_PROD = os.getenv("ENV", "dev") == "prod"

# Color palette - Esports theme
COLORS = {
    "bg_dark": "#0a0a0f",
    "bg_card": "#12121a",
    "bg_card_hover": "#1a1a25",
    "accent_blue": "#00d4ff",
    "accent_purple": "#8b5cf6",
    "accent_green": "#10b981",
    "accent_red": "#ef4444",
    "accent_yellow": "#f59e0b",
    "text_primary": "#ffffff",
    "text_secondary": "#94a3b8",
    "text_muted": "#64748b",
    "border": "#1e293b",
}

# Common styles
STYLES = {
    "page": {
        "backgroundColor": COLORS["bg_dark"],
        "minHeight": "100vh",
        "fontFamily": "'Inter', 'Segoe UI', system-ui, sans-serif",
        "color": COLORS["text_primary"],
        "padding": "0",
        "margin": "0",
    },
    "header": {
        "background": f"linear-gradient(135deg, {COLORS['bg_card']} 0%, #1a1a2e 100%)",
        "borderBottom": f"1px solid {COLORS['border']}",
        "padding": "1.5rem 2rem",
        "marginBottom": "2rem",
    },
    "header_title": {
        "margin": "0",
        "fontSize": "1.75rem",
        "fontWeight": "700",
        "background": f"linear-gradient(90deg, {COLORS['accent_blue']}, {COLORS['accent_purple']})",
        "WebkitBackgroundClip": "text",
        "WebkitTextFillColor": "transparent",
        "backgroundClip": "text",
    },
    "header_subtitle": {
        "margin": "0.25rem 0 0 0",
        "fontSize": "0.875rem",
        "color": COLORS["text_secondary"],
    },
    "container": {
        "maxWidth": "1400px",
        "margin": "0 auto",
        "padding": "0 2rem 2rem 2rem",
    },
    "card": {
        "backgroundColor": COLORS["bg_card"],
        "borderRadius": "12px",
        "border": f"1px solid {COLORS['border']}",
        "padding": "1.5rem",
        "marginBottom": "1.5rem",
    },
    "stat_card": {
        "backgroundColor": COLORS["bg_card"],
        "borderRadius": "12px",
        "border": f"1px solid {COLORS['border']}",
        "padding": "1.25rem",
        "textAlign": "center",
        "flex": "1",
        "minWidth": "150px",
    },
    "stat_value": {
        "fontSize": "2.5rem",
        "fontWeight": "700",
        "margin": "0",
        "lineHeight": "1",
    },
    "stat_label": {
        "fontSize": "0.75rem",
        "color": COLORS["text_secondary"],
        "textTransform": "uppercase",
        "letterSpacing": "0.05em",
        "marginTop": "0.5rem",
    },
    "button_primary": {
        "backgroundColor": COLORS["accent_blue"],
        "color": "#000",
        "border": "none",
        "borderRadius": "8px",
        "padding": "0.75rem 1.5rem",
        "fontSize": "0.875rem",
        "fontWeight": "600",
        "cursor": "pointer",
        "transition": "all 0.2s",
    },
    "button_secondary": {
        "backgroundColor": "transparent",
        "color": COLORS["accent_blue"],
        "border": f"1px solid {COLORS['accent_blue']}",
        "borderRadius": "8px",
        "padding": "0.75rem 1.5rem",
        "fontSize": "0.875rem",
        "fontWeight": "600",
        "cursor": "pointer",
    },
    "input": {
        "backgroundColor": COLORS["bg_dark"],
        "color": COLORS["text_primary"],
        "border": f"1px solid {COLORS['border']}",
        "borderRadius": "8px",
        "padding": "0.75rem 1rem",
        "fontSize": "0.875rem",
        "width": "100%",
    },
    "section_title": {
        "fontSize": "1.125rem",
        "fontWeight": "600",
        "color": COLORS["text_primary"],
        "margin": "0 0 1rem 0",
        "display": "flex",
        "alignItems": "center",
        "gap": "0.5rem",
    },
    "badge": {
        "display": "inline-block",
        "padding": "0.25rem 0.75rem",
        "borderRadius": "9999px",
        "fontSize": "0.75rem",
        "fontWeight": "600",
        "textTransform": "uppercase",
    },
}


def _get_auth_state() -> dict:
    """
    Check current user's auth state from session cookie.

    Returns dict with:
        logged_in (bool), user_name (str), user_id (str), user_email (str)

    Safe to call on every page load - returns empty state on any failure.
    """
    try:
        cookie = flask.request.cookies.get(SESSION_COOKIE_NAME, "")
        if not cookie:
            return {"logged_in": False, "user_name": "", "user_id": "", "user_email": ""}

        session_data = get_session(cookie)
        if not session_data:
            return {"logged_in": False, "user_name": "", "user_id": "", "user_email": ""}

        return {
            "logged_in": True,
            "user_name": (
                session_data.get("user_name")
                or session_data.get("user_email")
                or (f"user-{session_data.get('user_id')}" if session_data.get("user_id") else "")
            ),
            "user_id": session_data.get("user_id", ""),
            "user_email": session_data.get("user_email", ""),
        }
    except Exception as e:
        logger.warning(f"Auth state check failed: {e}")
        return {"logged_in": False, "user_name": "", "user_id": "", "user_email": ""}


def _build_auth_ui(auth_state: dict) -> html.Div:
    """Build the login/logout component for the header."""
    login_path = "/auth/login" if IS_PROD else "/admin/auth/login"
    logout_path = "/auth/logout" if IS_PROD else "/admin/auth/logout"

    if auth_state.get("logged_in"):
        return html.Div(
            style={
                "display": "flex",
                "alignItems": "center",
                "gap": "0.75rem",
            },
            children=[
                html.Span(
                    auth_state.get("user_name", "User"),
                    style={
                        "color": COLORS["accent_green"],
                        "fontSize": "0.8rem",
                        "fontWeight": "600",
                    },
                ),
                html.A(
                    "Logout",
                    href=logout_path,
                    style={
                        "color": COLORS["text_muted"],
                        "fontSize": "0.75rem",
                        "textDecoration": "none",
                        "padding": "0.3rem 0.6rem",
                        "borderRadius": "4px",
                        "border": f"1px solid {COLORS['border']}",
                        "transition": "all 0.2s",
                    },
                ),
            ],
        )
    else:
        return html.A(
            "Login with Start.gg",
            href=login_path,
            style={
                "color": COLORS["accent_blue"],
                "fontSize": "0.8rem",
                "fontWeight": "600",
                "textDecoration": "none",
                "padding": "0.4rem 0.8rem",
                "borderRadius": "6px",
                "border": f"1px solid {COLORS['accent_blue']}",
                "transition": "all 0.2s",
            },
        )


def create_layout():
    """
    Build and return the layout for the FGC Check-in Dashboard.
    Modern esports-themed design with live stats and status tracking.
    """
    # Auth state (checked on every page load via session cookie)
    auth_state = _get_auth_state()
    today = datetime.now().date()
    year_start = today.replace(month=1, day=1)

    # Fetch data
    try:
        active_slug = get_active_slug()
    except Exception as e:
        logger.exception(f"Failed to fetch active slug: {e}")
        active_slug = None

    # Fetch settings for requirements
    try:
        settings = get_active_settings() or {}
    except Exception as e:
        logger.exception(f"Failed to fetch settings: {e}")
        settings = {}

    try:
        event_slugs = get_all_event_slugs() or []
    except Exception as e:
        logger.exception(f"Failed to fetch event slugs: {e}")
        event_slugs = []

    try:
        archived_events = get_event_history() or []
    except Exception as e:
        logger.exception(f"Failed to fetch archived events: {e}")
        archived_events = []

    archived_slugs = []
    reopen_dropdown_options = []
    for ev in archived_events:
        slug = ev.get("event_slug") if isinstance(ev, dict) else None
        if slug:
            archived_slugs.append(slug)
            display_name = ev.get("event_display_name") or slug.replace("-", " ").title()
            event_date = ev.get("event_date") or ""
            participants = ev.get("total_participants", 0)
            label = f"{display_name}"
            if event_date:
                label += f"  ({event_date})"
            if participants:
                label += f"  — {participants} players"
            reopen_dropdown_options.append({"label": label, "value": slug})
    archived_slugs = sorted(set(archived_slugs))

    if active_slug and active_slug not in event_slugs:
        event_slugs = [active_slug] + event_slugs

    try:
        df = pd.DataFrame(get_checkins(active_slug)) if active_slug else pd.DataFrame()
    except Exception as e:
        logger.exception(f"Failed to fetch initial checkins: {e}")
        df = pd.DataFrame()

    # Prepare table data
    if df.empty:
        columns = [{"name": "No participants yet", "id": "info"}]
        data = []
    else:
        columns = [
            {"name": str(col).replace("_", " ").title(), "id": str(col)} for col in df.columns
        ]
        data = df.to_dict("records")

    # Format event name for display - use event_display_name if available (has proper åäö)
    event_display_name = settings.get("event_display_name", "")
    event_display = (
        event_display_name
        if event_display_name
        else (active_slug.replace("-", " ").title() if active_slug else "No Event Selected")
    )

    def _fmt_dt_local(value):
        if not value:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%dT%H:%M")
        txt = str(value).strip()
        if not txt:
            return ""
        txt = txt.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(txt).strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return ""

    checkin_opened_local = _fmt_dt_local(settings.get("checkin_opened_at"))
    event_started_local = _fmt_dt_local(settings.get("event_started_at"))
    event_ended_local = _fmt_dt_local(settings.get("event_ended_at"))

    # Build visible columns based on active requirements
    default_columns = [
        "name",
        "tag",
        "status",
        "telephone",
        "startgg",
        "is_guest",
        "tournament_games_registered",
    ]
    if settings.get("require_payment") is True:
        default_columns.insert(3, "payment_valid")
    if settings.get("require_membership") is True:
        default_columns.insert(4 if "payment_valid" in default_columns else 3, "member")

    return html.Div(
        style=STYLES["page"],
        children=[
            # Data stores
            dcc.Store(id="sse-token-store", data=SSE_TOKEN),  # Token for SSE auth
            dcc.Store(id="events-map-store", data=[]),
            dcc.Store(id="visible-columns-store", data=default_columns),
            dcc.Store(
                id="active-filter", data="all"
            ),  # Current filter: all, pending, ready, no-payment
            dcc.Store(id="sse-trigger", data=0),  # Incremented by SSE events to trigger refresh
            dcc.Store(id="sse-status", data="disconnected"),  # SSE connection status
            dcc.Store(id="auth-store", data=auth_state),  # Current user auth state
            dcc.Store(
                id="requirements-store",
                data={
                    # Requirements are enabled only when explicitly True
                    "require_payment": settings.get("require_payment") is True,
                    "require_membership": settings.get("require_membership") is True,
                    "require_startgg": settings.get("require_startgg") is True,
                    "collect_acquisition_source": settings.get("collect_acquisition_source")
                    is True,
                },
            ),
            dcc.Interval(
                id="interval-refresh", interval=300 * 1000, n_intervals=0, disabled=True
            ),  # Fallback: 5 min, disabled by default
            # Header
            html.Header(
                style={**STYLES["header"], "position": "relative"},
                children=[
                    # Auth UI (top left)
                    html.Div(
                        style={"position": "absolute", "top": "1.5rem", "left": "2rem"},
                        children=[_build_auth_ui(auth_state)],
                    ),
                    # Centered logo
                    html.Div(
                        style={"display": "flex", "justifyContent": "center"},
                        children=[
                            html.Img(
                                src="/assets/logo.png", style={"height": "60px", "width": "auto"}
                            ),
                        ],
                    ),
                    # Connection status indicator (top right)
                    html.Div(
                        id="connection-indicator",
                        style={
                            "position": "absolute",
                            "top": "1.5rem",
                            "right": "2rem",
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "0.5rem",
                        },
                        children=[
                            html.Div(
                                id="connection-dot",
                                style={
                                    "width": "10px",
                                    "height": "10px",
                                    "backgroundColor": COLORS[
                                        "accent_yellow"
                                    ],  # Yellow = connecting
                                    "borderRadius": "50%",
                                    "boxShadow": f"0 0 10px {COLORS['accent_yellow']}",
                                    "transition": "all 0.3s ease",
                                },
                            ),
                            html.Span(
                                "Connecting...",
                                id="connection-text",
                                style={
                                    "color": COLORS["accent_yellow"],
                                    "fontSize": "0.75rem",
                                    "fontWeight": "600",
                                    "letterSpacing": "0.1em",
                                    "transition": "all 0.3s ease",
                                },
                            ),
                        ],
                    ),
                ],
            ),
            # SSE JavaScript loaded from assets/sse-client.js automatically by Dash
            # Main content
            html.Div(
                style=STYLES["container"],
                children=[
                    # Tabs
                    dcc.Tabs(
                        id="tabs",
                        value="tab-checkins",
                        style={"marginBottom": "1.5rem"},
                        colors={
                            "border": COLORS["border"],
                            "primary": COLORS["accent_blue"],
                            "background": COLORS["bg_card"],
                        },
                        children=[
                            dcc.Tab(
                                label="⚡ Live Check-ins",
                                value="tab-checkins",
                                style={
                                    "backgroundColor": COLORS["bg_card"],
                                    "color": COLORS["text_secondary"],
                                    "border": "none",
                                    "padding": "1rem 1.5rem",
                                },
                                selected_style={
                                    "backgroundColor": COLORS["bg_dark"],
                                    "color": COLORS["accent_blue"],
                                    "borderTop": f"2px solid {COLORS['accent_blue']}",
                                    "padding": "1rem 1.5rem",
                                },
                            ),
                            dcc.Tab(
                                label="📈 Insights",
                                value="tab-insights",
                                style={
                                    "backgroundColor": COLORS["bg_card"],
                                    "color": COLORS["text_secondary"],
                                    "border": "none",
                                    "padding": "1rem 1.5rem",
                                },
                                selected_style={
                                    "backgroundColor": COLORS["bg_dark"],
                                    "color": COLORS["accent_blue"],
                                    "borderTop": f"2px solid {COLORS['accent_blue']}",
                                    "padding": "1rem 1.5rem",
                                },
                            ),
                            dcc.Tab(
                                label="⚙️ Settings",
                                value="tab-settings",
                                style={
                                    "backgroundColor": COLORS["bg_card"],
                                    "color": COLORS["text_secondary"],
                                    "border": "none",
                                    "padding": "1rem 1.5rem",
                                },
                                selected_style={
                                    "backgroundColor": COLORS["bg_dark"],
                                    "color": COLORS["accent_blue"],
                                    "borderTop": f"2px solid {COLORS['accent_blue']}",
                                    "padding": "1rem 1.5rem",
                                },
                            ),
                        ],
                    ),
                    # Tab content container
                    html.Div(
                        id="tabs-content",
                        children=[
                            # ========== TAB 1: Live Check-ins ==========
                            html.Div(
                                id="tab-checkins-content",
                                children=[
                                    # Event selector row
                                    html.Div(
                                        style={
                                            "display": "flex",
                                            "gap": "1rem",
                                            "marginBottom": "1.5rem",
                                            "flexWrap": "wrap",
                                            "alignItems": "flex-end",
                                        },
                                        children=[
                                            html.Div(
                                                style={"flex": "1", "minWidth": "250px"},
                                                children=[
                                                    html.Label(
                                                        "Current Event",
                                                        style={
                                                            "fontSize": "0.75rem",
                                                            "color": COLORS["text_secondary"],
                                                            "marginBottom": "0.5rem",
                                                            "display": "block",
                                                        },
                                                    ),
                                                    dcc.Dropdown(
                                                        id="event-dropdown",
                                                        className="fgc-dropdown",
                                                        placeholder="Insert slug in Settings (Fetch Event Data)",
                                                        options=[
                                                            {
                                                                "label": "🔍 All Events (Debug)",
                                                                "value": "__ALL__",
                                                            },
                                                        ]
                                                        + [
                                                            # Use event_display_name for active slug (has proper åäö), fallback to slug title
                                                            {
                                                                "label": (
                                                                    event_display_name
                                                                    if s == active_slug
                                                                    and event_display_name
                                                                    else s.replace("-", " ").title()
                                                                ),
                                                                "value": s,
                                                            }
                                                            for s in event_slugs
                                                        ],
                                                        value=(
                                                            active_slug
                                                            if active_slug in event_slugs
                                                            else (
                                                                event_slugs[0]
                                                                if event_slugs
                                                                else None
                                                            )
                                                        ),
                                                        clearable=True,
                                                        style={
                                                            "backgroundColor": COLORS["bg_dark"]
                                                        },
                                                    ),
                                                ],
                                            ),
                                            html.Button(
                                                "Refresh",
                                                id="btn-refresh",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_secondary"],
                                                    "height": "38px",
                                                },
                                            ),
                                            html.Button(
                                                "Archive Event",
                                                id="btn-archive-event-quick",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_primary"],
                                                    "height": "38px",
                                                    "backgroundColor": COLORS["accent_yellow"],
                                                    "color": "#111827",
                                                },
                                            ),
                                            html.Button(
                                                "Clear Event",
                                                id="btn-clear-current-event",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_secondary"],
                                                    "height": "38px",
                                                    "borderColor": COLORS["accent_red"],
                                                    "color": COLORS["accent_red"],
                                                },
                                            ),
                                            html.Button(
                                                "Check-in Opened Now",
                                                id="btn-live-checkin-opened-now",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_secondary"],
                                                    "height": "38px",
                                                },
                                            ),
                                            html.Button(
                                                "Start Event Now",
                                                id="btn-live-event-started-now",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_secondary"],
                                                    "height": "38px",
                                                },
                                            ),
                                            html.Button(
                                                "End Event Now",
                                                id="btn-live-event-ended-now",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_secondary"],
                                                    "height": "38px",
                                                },
                                            ),
                                        ],
                                    ),
                                    html.Div(
                                        id="live-ops-feedback",
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.78rem",
                                            "marginBottom": "0.75rem",
                                        },
                                    ),
                                    html.Div(
                                        id="live-ops-status",
                                        style={
                                            "display": "flex",
                                            "gap": "0.6rem",
                                            "flexWrap": "wrap",
                                            "marginBottom": "0.9rem",
                                        },
                                    ),
                                    # Active requirements indicator
                                    html.Div(
                                        id="requirements-indicator",
                                        style={
                                            "display": "flex",
                                            "alignItems": "center",
                                            "gap": "0.75rem",
                                            "marginBottom": "1rem",
                                            "padding": "0.5rem 0.75rem",
                                            "backgroundColor": COLORS["bg_card"],
                                            "borderRadius": "8px",
                                            "border": f"1px solid {COLORS['border']}",
                                            "fontSize": "0.75rem",
                                        },
                                        children=[
                                            html.Span(
                                                "Active Requirements:",
                                                style={
                                                    "color": COLORS["text_muted"],
                                                    "fontWeight": "500",
                                                },
                                            ),
                                            html.Div(
                                                id="requirement-badges",
                                                style={
                                                    "display": "flex",
                                                    "gap": "0.5rem",
                                                    "flexWrap": "wrap",
                                                },
                                            ),
                                        ],
                                    ),
                                    # Stats cards (clickable as filters)
                                    html.Div(
                                        id="stats-cards",
                                        style={
                                            "display": "flex",
                                            "gap": "1rem",
                                            "marginBottom": "1.5rem",
                                            "flexWrap": "wrap",
                                        },
                                        children=[
                                            html.Div(
                                                id="filter-all",
                                                n_clicks=0,
                                                className="stat-card-live",
                                                style={
                                                    **STYLES["stat_card"],
                                                    "borderTop": f"3px solid {COLORS['accent_blue']}",
                                                    "cursor": "pointer",
                                                    "transition": "all 0.2s",
                                                },
                                                children=[
                                                    html.P(
                                                        str(len(data)),
                                                        id="stat-total",
                                                        style={
                                                            **STYLES["stat_value"],
                                                            "color": COLORS["accent_blue"],
                                                        },
                                                    ),
                                                    html.P("Total", style=STYLES["stat_label"]),
                                                ],
                                            ),
                                            html.Div(
                                                id="filter-ready",
                                                n_clicks=0,
                                                className="stat-card-live",
                                                style={
                                                    **STYLES["stat_card"],
                                                    "borderTop": f"3px solid {COLORS['accent_green']}",
                                                    "cursor": "pointer",
                                                    "transition": "all 0.2s",
                                                },
                                                children=[
                                                    html.P(
                                                        str(
                                                            len(
                                                                [
                                                                    d
                                                                    for d in data
                                                                    if d.get("status") == "Ready"
                                                                ]
                                                            )
                                                        ),
                                                        id="stat-ready",
                                                        style={
                                                            **STYLES["stat_value"],
                                                            "color": COLORS["accent_green"],
                                                        },
                                                    ),
                                                    html.P("Ready", style=STYLES["stat_label"]),
                                                ],
                                            ),
                                            html.Div(
                                                id="filter-pending",
                                                n_clicks=0,
                                                className="stat-card-live",
                                                style={
                                                    **STYLES["stat_card"],
                                                    "borderTop": f"3px solid {COLORS['accent_yellow']}",
                                                    "cursor": "pointer",
                                                    "transition": "all 0.2s",
                                                },
                                                children=[
                                                    html.P(
                                                        str(
                                                            len(
                                                                [
                                                                    d
                                                                    for d in data
                                                                    if d.get("status") == "Pending"
                                                                ]
                                                            )
                                                        ),
                                                        id="stat-pending",
                                                        style={
                                                            **STYLES["stat_value"],
                                                            "color": COLORS["accent_yellow"],
                                                        },
                                                    ),
                                                    html.P("Pending", style=STYLES["stat_label"]),
                                                ],
                                            ),
                                            html.Div(
                                                id="filter-no-payment",
                                                n_clicks=0,
                                                className="stat-card-live",
                                                style={
                                                    **STYLES["stat_card"],
                                                    "borderTop": f"3px solid {COLORS['accent_red']}",
                                                    "cursor": "pointer",
                                                    "transition": "all 0.2s",
                                                },
                                                children=[
                                                    html.P(
                                                        "0",
                                                        id="stat-attention",
                                                        style={
                                                            **STYLES["stat_value"],
                                                            "color": COLORS["accent_red"],
                                                        },
                                                    ),
                                                    html.P(
                                                        "No Payment", style=STYLES["stat_label"]
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                    # Needs attention section (collapsible)
                                    html.Div(
                                        id="needs-attention-section",
                                        style={
                                            **STYLES["card"],
                                            "borderLeft": f"4px solid {COLORS['accent_red']}",
                                            "display": "none",  # Hidden by default, shown by callback when needed
                                        },
                                        children=[
                                            # Header with toggle
                                            html.Div(
                                                style={
                                                    "display": "flex",
                                                    "justifyContent": "space-between",
                                                    "alignItems": "center",
                                                    "cursor": "pointer",
                                                },
                                                id="needs-attention-header",
                                                n_clicks=0,
                                                children=[
                                                    html.H3(
                                                        "🚨 Needs Attention",
                                                        style={
                                                            **STYLES["section_title"],
                                                            "color": COLORS["accent_red"],
                                                            "margin": "0",
                                                        },
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "alignItems": "center",
                                                            "gap": "0.5rem",
                                                        },
                                                        children=[
                                                            html.Span(
                                                                id="needs-attention-count",
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "accent_red"
                                                                    ],
                                                                    "color": "#fff",
                                                                    "padding": "0.25rem 0.6rem",
                                                                    "borderRadius": "12px",
                                                                    "fontSize": "0.8rem",
                                                                    "fontWeight": "600",
                                                                },
                                                            ),
                                                            html.Span(
                                                                "▼",
                                                                id="needs-attention-chevron",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.8rem",
                                                                    "transition": "transform 0.2s",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            # Collapsible content
                                            html.Div(
                                                id="needs-attention-list",
                                                children=[
                                                    html.P(
                                                        "No issues",
                                                        style={
                                                            "color": COLORS["text_muted"],
                                                            "margin": "0",
                                                        },
                                                    )
                                                ],
                                                style={"marginTop": "1rem"},
                                            ),
                                        ],
                                    ),
                                    # Main checkins table
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            # Header row with title and player count
                                            html.Div(
                                                style={
                                                    "display": "flex",
                                                    "justifyContent": "space-between",
                                                    "alignItems": "center",
                                                    "marginBottom": "1rem",
                                                    "gap": "0.8rem",
                                                    "flexWrap": "wrap",
                                                },
                                                children=[
                                                    html.H3(
                                                        "Player List",
                                                        style={
                                                            **STYLES["section_title"],
                                                            "margin": "0",
                                                        },
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "gap": "0.7rem",
                                                            "alignItems": "center",
                                                            "flexWrap": "wrap",
                                                        },
                                                        children=[
                                                            html.Span(
                                                                id="active-event-coverage",
                                                                children="",
                                                                style={
                                                                    "color": COLORS["accent_blue"],
                                                                    "fontSize": "0.82rem",
                                                                    "fontWeight": "600",
                                                                },
                                                            ),
                                                            html.Span(
                                                                id="active-event-coverage-source",
                                                                children="",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.74rem",
                                                                },
                                                            ),
                                                            html.Span(
                                                                id="player-count",
                                                                children=f"{len(data)} players",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.875rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="duplicate-warning",
                                                style={
                                                    "marginBottom": "0.75rem",
                                                    "display": "none",
                                                },
                                                children=[
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "justifyContent": "space-between",
                                                            "alignItems": "center",
                                                            "gap": "0.6rem",
                                                        },
                                                        children=[
                                                            html.Span(
                                                                id="duplicate-warning-text",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": "#f59e0b",
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Dismiss",
                                                                id="duplicate-warning-dismiss",
                                                                n_clicks=0,
                                                                style={
                                                                    "backgroundColor": "transparent",
                                                                    "border": "1px solid rgba(245, 158, 11, 0.45)",
                                                                    "color": "#fbbf24",
                                                                    "borderRadius": "6px",
                                                                    "padding": "0.2rem 0.55rem",
                                                                    "fontSize": "0.75rem",
                                                                    "cursor": "pointer",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Ul(
                                                        id="duplicate-warning-list",
                                                        style={
                                                            "margin": "0.35rem 0 0 1rem",
                                                            "color": "#fbbf24",
                                                            "fontSize": "0.82rem",
                                                        },
                                                    ),
                                                ],
                                            ),
                                            # Search and game filter row
                                            html.Div(
                                                style={
                                                    "display": "flex",
                                                    "gap": "1rem",
                                                    "marginBottom": "1rem",
                                                    "flexWrap": "wrap",
                                                    "alignItems": "center",
                                                },
                                                children=[
                                                    # Search field
                                                    dcc.Input(
                                                        id="search-input",
                                                        type="text",
                                                        placeholder="Search name or tag...",
                                                        style={
                                                            **STYLES["input"],
                                                            "maxWidth": "250px",
                                                            "flex": "1",
                                                        },
                                                        debounce=True,
                                                    ),
                                                    # Game filter dropdown
                                                    html.Div(
                                                        style={"minWidth": "140px"},
                                                        children=[
                                                            dcc.Dropdown(
                                                                id="game-filter",
                                                                className="fgc-dropdown",
                                                                options=[],  # Populated dynamically
                                                                value=None,
                                                                placeholder="All games",
                                                                clearable=True,
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ],
                                                                    "minWidth": "140px",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            dcc.Loading(
                                                type="circle",
                                                color=COLORS["accent_blue"],
                                                children=dash_table.DataTable(
                                                    id="checkins-table",
                                                    columns=columns,
                                                    data=data,
                                                    editable=False,
                                                    page_size=20,
                                                    sort_action="native",
                                                    row_selectable="multi",
                                                    style_table={"overflowX": "auto"},
                                                    style_header={
                                                        "backgroundColor": COLORS["bg_dark"],
                                                        "color": COLORS["text_primary"],
                                                        "fontWeight": "600",
                                                        "fontSize": "0.75rem",
                                                        "textTransform": "uppercase",
                                                        "letterSpacing": "0.05em",
                                                        "padding": "1rem",
                                                        "borderBottom": f"2px solid {COLORS['accent_blue']}",
                                                    },
                                                    style_cell={
                                                        "backgroundColor": COLORS["bg_card"],
                                                        "color": COLORS["text_primary"],
                                                        "border": "none",
                                                        "borderBottom": f"1px solid {COLORS['border']}",
                                                        "padding": "1rem",
                                                        "fontSize": "0.875rem",
                                                        "textAlign": "left",
                                                    },
                                                    style_data_conditional=[
                                                        # Ready status - green row highlight
                                                        {
                                                            "if": {
                                                                "filter_query": "{status} = 'Ready'"
                                                            },
                                                            "backgroundColor": "rgba(16, 185, 129, 0.1)",
                                                            "borderLeft": f"3px solid {COLORS['accent_green']}",
                                                        },
                                                        # Pending status - yellow row highlight
                                                        {
                                                            "if": {
                                                                "filter_query": "{status} = 'Pending'"
                                                            },
                                                            "backgroundColor": "rgba(245, 158, 11, 0.1)",
                                                            "borderLeft": f"3px solid {COLORS['accent_yellow']}",
                                                        },
                                                        # Icon cells: ✓ = green (clickable columns have cursor pointer)
                                                        {
                                                            "if": {
                                                                "filter_query": '{member} = "✓"',
                                                                "column_id": "member",
                                                            },
                                                            "color": COLORS["accent_green"],
                                                            "fontWeight": "600",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{startgg} = "✓"',
                                                                "column_id": "startgg",
                                                            },
                                                            "color": COLORS["accent_green"],
                                                            "fontWeight": "600",
                                                            "cursor": "pointer",
                                                            "textDecoration": "underline",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{is_guest} = "✓"',
                                                                "column_id": "is_guest",
                                                            },
                                                            "color": COLORS["accent_blue"],
                                                            "fontWeight": "600",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{payment_valid} = "✓"',
                                                                "column_id": "payment_valid",
                                                            },
                                                            "color": COLORS["accent_green"],
                                                            "fontWeight": "600",
                                                            "cursor": "pointer",
                                                            "textDecoration": "underline",
                                                        },
                                                        # Icon cells: ✗ = red (clickable columns have cursor pointer)
                                                        {
                                                            "if": {
                                                                "filter_query": '{member} = "✗"',
                                                                "column_id": "member",
                                                            },
                                                            "color": COLORS["accent_red"],
                                                            "fontWeight": "600",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{startgg} = "✗"',
                                                                "column_id": "startgg",
                                                            },
                                                            "color": COLORS["accent_red"],
                                                            "fontWeight": "600",
                                                            "cursor": "pointer",
                                                            "textDecoration": "underline",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{is_guest} = "✗"',
                                                                "column_id": "is_guest",
                                                            },
                                                            "color": COLORS["text_muted"],
                                                            "fontWeight": "600",
                                                        },
                                                        {
                                                            "if": {
                                                                "filter_query": '{payment_valid} = "✗"',
                                                                "column_id": "payment_valid",
                                                            },
                                                            "color": COLORS["accent_red"],
                                                            "fontWeight": "600",
                                                            "cursor": "pointer",
                                                            "textDecoration": "underline",
                                                        },
                                                        # Hover/active effect - keep text readable
                                                        {
                                                            "if": {"state": "active"},
                                                            "backgroundColor": "#1e293b",
                                                            "color": "#ffffff",
                                                        },
                                                        {
                                                            "if": {"state": "selected"},
                                                            "backgroundColor": "#1e293b",
                                                            "color": "#ffffff",
                                                        },
                                                        # Row striping
                                                        {
                                                            "if": {"row_index": "odd"},
                                                            "backgroundColor": COLORS["bg_dark"],
                                                        },
                                                    ],
                                                ),
                                            ),
                                        ],
                                    ),
                                    # Action buttons row
                                    html.Div(
                                        style={
                                            "display": "flex",
                                            "gap": "1rem",
                                            "marginTop": "1rem",
                                            "alignItems": "center",
                                            "flexWrap": "wrap",
                                        },
                                        children=[
                                            html.Button(
                                                "Multi-select: ON",
                                                id="btn-toggle-multiselect",
                                                n_clicks=0,
                                                style={
                                                    "backgroundColor": "transparent",
                                                    "color": COLORS["accent_blue"],
                                                    "border": f"1px solid {COLORS['accent_blue']}",
                                                    "borderRadius": "8px",
                                                    "padding": "0.45rem 0.75rem",
                                                    "fontSize": "0.8rem",
                                                    "fontWeight": "600",
                                                    "cursor": "pointer",
                                                },
                                            ),
                                            html.Button(
                                                "Delete Selected",
                                                id="btn-delete-selected",
                                                n_clicks=0,
                                                style={
                                                    "backgroundColor": COLORS["accent_red"],
                                                    "color": "#fff",
                                                    "border": "none",
                                                    "borderRadius": "8px",
                                                    "padding": "0.5rem 1rem",
                                                    "fontSize": "0.875rem",
                                                    "fontWeight": "600",
                                                    "cursor": "pointer",
                                                },
                                            ),
                                            html.Button(
                                                "Export Guests (CSV)",
                                                id="btn-export-guests",
                                                n_clicks=0,
                                                style={
                                                    "backgroundColor": COLORS["accent_blue"],
                                                    "color": "#fff",
                                                    "border": "none",
                                                    "borderRadius": "8px",
                                                    "padding": "0.5rem 1rem",
                                                    "fontSize": "0.875rem",
                                                    "fontWeight": "600",
                                                    "cursor": "pointer",
                                                },
                                            ),
                                            html.Span(
                                                "Click row to select • Click Payment/Start.gg/Member to toggle • Edit Name/Tag/Phone/Games inline",
                                                style={
                                                    "color": COLORS["text_muted"],
                                                    "fontSize": "0.75rem",
                                                },
                                            ),
                                        ],
                                    ),
                                    html.Div(
                                        style={"marginTop": "0.75rem"},
                                        children=[
                                            html.Button(
                                                "Manual Checkin Tools ▾",
                                                id="btn-toggle-manual-checkin",
                                                n_clicks=0,
                                                style={
                                                    "backgroundColor": "transparent",
                                                    "color": COLORS["text_secondary"],
                                                    "border": f"1px solid {COLORS['border']}",
                                                    "borderRadius": "8px",
                                                    "padding": "0.4rem 0.75rem",
                                                    "fontSize": "0.78rem",
                                                    "fontWeight": "600",
                                                    "cursor": "pointer",
                                                },
                                            ),
                                            html.Div(
                                                id="manual-checkin-panel",
                                                style={"display": "none", "marginTop": "0.6rem"},
                                                children=[
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "gap": "0.6rem",
                                                            "flexWrap": "wrap",
                                                            "alignItems": "center",
                                                        },
                                                        children=[
                                                            dcc.Input(
                                                                id="input-manual-name",
                                                                type="text",
                                                                placeholder="Manual check-in: Name",
                                                                style={
                                                                    **STYLES["input"],
                                                                    "maxWidth": "230px",
                                                                    "height": "40px",
                                                                    "fontSize": "0.82rem",
                                                                    "lineHeight": "1.2",
                                                                },
                                                            ),
                                                            dcc.Input(
                                                                id="input-manual-tag",
                                                                type="text",
                                                                placeholder="Tag (optional)",
                                                                style={
                                                                    **STYLES["input"],
                                                                    "maxWidth": "180px",
                                                                    "height": "40px",
                                                                    "fontSize": "0.82rem",
                                                                    "lineHeight": "1.2",
                                                                },
                                                            ),
                                                            dcc.Dropdown(
                                                                id="input-manual-games",
                                                                className="fgc-dropdown",
                                                                placeholder="Games...",
                                                                multi=True,
                                                                style={
                                                                    "minWidth": "180px",
                                                                    "maxWidth": "320px",
                                                                    "fontSize": "0.8rem",
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Add Manual Check-in",
                                                                id="btn-manual-checkin",
                                                                n_clicks=0,
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "accent_green"
                                                                    ],
                                                                    "color": "#fff",
                                                                    "border": "none",
                                                                    "borderRadius": "8px",
                                                                    "padding": "0.5rem 0.9rem",
                                                                    "fontSize": "0.8rem",
                                                                    "fontWeight": "600",
                                                                    "cursor": "pointer",
                                                                },
                                                            ),
                                                            html.Span(
                                                                "Use for Start.gg players who missed the kiosk check-in.",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.75rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Hr(
                                                        style={
                                                            "borderColor": COLORS["border"],
                                                            "margin": "0.7rem 0",
                                                            "opacity": "0.4",
                                                        }
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "gap": "0.6rem",
                                                            "flexWrap": "wrap",
                                                            "alignItems": "center",
                                                        },
                                                        children=[
                                                            html.Button(
                                                                "Re-check Start.gg",
                                                                id="btn-recheck-startgg",
                                                                n_clicks=0,
                                                                style={
                                                                    "backgroundColor": "#a78bfa",
                                                                    "color": "#fff",
                                                                    "border": "none",
                                                                    "borderRadius": "8px",
                                                                    "padding": "0.5rem 0.9rem",
                                                                    "fontSize": "0.8rem",
                                                                    "fontWeight": "600",
                                                                    "cursor": "pointer",
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Bulk Re-check All",
                                                                id="btn-bulk-recheck-startgg",
                                                                n_clicks=0,
                                                                style={
                                                                    "backgroundColor": "#7c3aed",
                                                                    "color": "#fff",
                                                                    "border": "none",
                                                                    "borderRadius": "8px",
                                                                    "padding": "0.5rem 0.9rem",
                                                                    "fontSize": "0.8rem",
                                                                    "fontWeight": "600",
                                                                    "cursor": "pointer",
                                                                },
                                                            ),
                                                            html.Span(
                                                                "Select row + Re-check, or Bulk all.",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.75rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Hr(
                                                        style={
                                                            "borderColor": COLORS["border"],
                                                            "margin": "0.7rem 0",
                                                            "opacity": "0.4",
                                                        }
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "gap": "0.5rem",
                                                            "flexWrap": "wrap",
                                                            "alignItems": "center",
                                                        },
                                                        children=[
                                                            html.Span(
                                                                "Columns:",
                                                                style={
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "fontSize": "0.75rem",
                                                                    "fontWeight": "600",
                                                                    "marginRight": "0.2rem",
                                                                },
                                                            ),
                                                            dcc.Checklist(
                                                                id="quick-column-toggle",
                                                                className="quick-col-toggle",
                                                                options=[
                                                                    opt
                                                                    for opt in [
                                                                        {
                                                                            "label": "Name",
                                                                            "value": "name",
                                                                        },
                                                                        {
                                                                            "label": "Tag",
                                                                            "value": "tag",
                                                                        },
                                                                        {
                                                                            "label": "Status",
                                                                            "value": "status",
                                                                        },
                                                                        (
                                                                            {
                                                                                "label": "Pay",
                                                                                "value": "payment_valid",
                                                                            }
                                                                            if settings.get(
                                                                                "require_payment"
                                                                            )
                                                                            is True
                                                                            else None
                                                                        ),
                                                                        {
                                                                            "label": "Phone",
                                                                            "value": "telephone",
                                                                        },
                                                                        (
                                                                            {
                                                                                "label": "Member",
                                                                                "value": "member",
                                                                            }
                                                                            if settings.get(
                                                                                "require_membership"
                                                                            )
                                                                            is True
                                                                            else None
                                                                        ),
                                                                        {
                                                                            "label": "Sgg",
                                                                            "value": "startgg",
                                                                        },
                                                                        {
                                                                            "label": "Guest",
                                                                            "value": "is_guest",
                                                                        },
                                                                        {
                                                                            "label": "Games",
                                                                            "value": "tournament_games_registered",
                                                                        },
                                                                        {
                                                                            "label": "Email",
                                                                            "value": "email",
                                                                        },
                                                                    ]
                                                                    if opt is not None
                                                                ],
                                                                value=[
                                                                    c
                                                                    for c in [
                                                                        "name",
                                                                        "tag",
                                                                        "status",
                                                                        "payment_valid",
                                                                        "telephone",
                                                                        "member",
                                                                        "startgg",
                                                                        "is_guest",
                                                                        "tournament_games_registered",
                                                                    ]
                                                                    if not (
                                                                        c == "payment_valid"
                                                                        and settings.get(
                                                                            "require_payment"
                                                                        )
                                                                        is not True
                                                                    )
                                                                    and not (
                                                                        c == "member"
                                                                        and settings.get(
                                                                            "require_membership"
                                                                        )
                                                                        is not True
                                                                    )
                                                                ],
                                                                inline=True,
                                                                style={
                                                                    "fontSize": "0.72rem",
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                },
                                                                inputStyle={
                                                                    "marginRight": "3px",
                                                                },
                                                                labelStyle={
                                                                    "marginRight": "0.6rem",
                                                                    "cursor": "pointer",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                    html.Div(
                                        id="recheck-startgg-feedback", style={"marginTop": "0.5rem"}
                                    ),
                                    # Feedback messages
                                    html.Div(
                                        id="payment-update-feedback", style={"marginTop": "0.5rem"}
                                    ),
                                    html.Div(
                                        id="manual-checkin-feedback", style={"marginTop": "0.5rem"}
                                    ),
                                    html.Div(id="delete-feedback", style={"marginTop": "0.5rem"}),
                                    html.Div(id="export-feedback", style={"marginTop": "0.5rem"}),
                                    # Download component for CSV export
                                    dcc.Download(id="download-guests-csv"),
                                    # Confirmation dialog for delete
                                    dcc.ConfirmDialog(
                                        id="confirm-delete-dialog",
                                        message="Are you sure you want to delete this player?",
                                    ),
                                    dcc.ConfirmDialog(
                                        id="confirm-clear-event-dialog",
                                        message="Clear current event selection?",
                                    ),
                                ],
                            ),
                            # ========== TAB 2: Insights ==========
                            html.Div(
                                id="tab-insights-content",
                                style={"display": "none"},
                                children=[
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.Div(
                                                id="insights-summary-title",
                                                style={
                                                    "color": COLORS["text_primary"],
                                                    "fontWeight": "600",
                                                    "marginBottom": "0.45rem",
                                                },
                                            ),
                                            html.Div(
                                                style={
                                                    "display": "flex",
                                                    "gap": "0.7rem",
                                                    "flexWrap": "wrap",
                                                    "alignItems": "flex-end",
                                                    "marginBottom": "1rem",
                                                },
                                                children=[
                                                    html.Div(
                                                        style={"flex": "1 1 260px", "minWidth": "260px"},
                                                        children=[
                                                            html.Label(
                                                                "Archived Events",
                                                                style={
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "marginBottom": "0.5rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Dropdown(
                                                                id="insights-event-dropdown",
                                                                className="fgc-dropdown",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                clearable=True,
                                                                placeholder="Select events (empty = all)",
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ]
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "160px"},
                                                        children=[
                                                            html.Label(
                                                                "Period",
                                                                style={
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "marginBottom": "0.5rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Dropdown(
                                                                id="insights-period-dropdown",
                                                                className="fgc-dropdown",
                                                                clearable=False,
                                                                value="custom",
                                                                options=[
                                                                    {
                                                                        "label": "Last 24h",
                                                                        "value": "day",
                                                                    },
                                                                    {
                                                                        "label": "Last 7 days",
                                                                        "value": "week",
                                                                    },
                                                                    {
                                                                        "label": "Last 30 days",
                                                                        "value": "month",
                                                                    },
                                                                    {
                                                                        "label": "Last 90 days",
                                                                        "value": "quarter",
                                                                    },
                                                                    {
                                                                        "label": "Last 365 days",
                                                                        "value": "year",
                                                                    },
                                                                    {
                                                                        "label": "Year to date",
                                                                        "value": "custom",
                                                                    },
                                                                    {
                                                                        "label": "All time",
                                                                        "value": "all",
                                                                    },
                                                                ],
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ]
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={"minWidth": "220px"},
                                                        children=[
                                                            html.Label(
                                                                "Series",
                                                                style={
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "marginBottom": "0.5rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Dropdown(
                                                                id="insights-series-dropdown",
                                                                className="fgc-dropdown",
                                                                options=[],
                                                                value=[],
                                                                multi=True,
                                                                clearable=True,
                                                                placeholder="Filter by series (optional)",
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ]
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-custom-range-wrap",
                                                        style={
                                                            "display": "block",
                                                            "minWidth": "290px",
                                                        },
                                                        children=[
                                                            html.Label(
                                                                "Date Range",
                                                                style={
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "marginBottom": "0.5rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            html.Div(
                                                                style={
                                                                    "display": "flex",
                                                                    "alignItems": "center",
                                                                    "gap": "0.35rem",
                                                                },
                                                                children=[
                                                                    dcc.Input(
                                                                        id="insights-date-start",
                                                                        type="date",
                                                                        className="fgc-date-input",
                                                                        value=year_start.isoformat(),
                                                                        style={"width": "130px"},
                                                                    ),
                                                                    html.Span(
                                                                        "to",
                                                                        style={
                                                                            "color": COLORS["text_muted"],
                                                                            "fontSize": "0.74rem",
                                                                        },
                                                                    ),
                                                                    dcc.Input(
                                                                        id="insights-date-end",
                                                                        type="date",
                                                                        className="fgc-date-input",
                                                                        value=today.isoformat(),
                                                                        style={"width": "130px"},
                                                                    ),
                                                                ],
                                                            ),
                                                        ],
                                                    ),
                                                    html.Button(
                                                        "Refresh",
                                                        id="btn-insights-refresh",
                                                        n_clicks=0,
                                                        style={
                                                            **STYLES["button_secondary"],
                                                            "height": "38px",
                                                        },
                                                    ),
                                                    html.Button(
                                                        "Export CSV",
                                                        id="btn-insights-export-csv",
                                                        n_clicks=0,
                                                        style={
                                                            **STYLES["button_secondary"],
                                                            "height": "38px",
                                                        },
                                                    ),
                                                    dcc.Download(id="insights-download"),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-empty-hint",
                                                style={
                                                    "color": COLORS["text_muted"],
                                                    "fontSize": "0.82rem",
                                                    "marginBottom": "0.75rem",
                                                },
                                            ),
                                            dcc.Tabs(
                                                id="insights-subtabs",
                                                value="players",
                                                style={"marginBottom": "0.9rem"},
                                                children=[
                                                    dcc.Tab(
                                                        label="Players",
                                                        value="players",
                                                        className="insights-subtab",
                                                        selected_className="insights-subtab--selected",
                                                    ),
                                                    dcc.Tab(
                                                        label="Games",
                                                        value="games",
                                                        className="insights-subtab",
                                                        selected_className="insights-subtab--selected",
                                                    ),
                                                    dcc.Tab(
                                                        label="Events",
                                                        value="events",
                                                        className="insights-subtab",
                                                        selected_className="insights-subtab--selected",
                                                    ),
                                                    dcc.Tab(
                                                        label="Earnings",
                                                        value="earnings",
                                                        className="insights-subtab",
                                                        selected_className="insights-subtab--selected",
                                                    ),
                                                    dcc.Tab(
                                                        label="Duplicates",
                                                        value="duplicates",
                                                        className="insights-subtab",
                                                        selected_className="insights-subtab--selected",
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-kpi-summary-row",
                                                children=[
                                                    html.Div(
                                                        id="insights-summary-core",
                                                        n_clicks=0,
                                                        className="stat-card-live insights-summary-card is-active",
                                                        children=[
                                                            html.P(
                                                                "Core Event",
                                                                className="insights-summary-title",
                                                            ),
                                                            html.P(
                                                                "-",
                                                                id="insights-summary-core-value",
                                                                className="insights-summary-value",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-summary-community",
                                                        n_clicks=0,
                                                        className="stat-card-live insights-summary-card",
                                                        children=[
                                                            html.P(
                                                                "Community Health",
                                                                className="insights-summary-title",
                                                            ),
                                                            html.P(
                                                                "-",
                                                                id="insights-summary-community-value",
                                                                className="insights-summary-value",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-summary-tournament",
                                                        n_clicks=0,
                                                        className="stat-card-live insights-summary-card",
                                                        children=[
                                                            html.P(
                                                                "Tournament Health",
                                                                className="insights-summary-title",
                                                            ),
                                                            html.P(
                                                                "-",
                                                                id="insights-summary-tournament-value",
                                                                className="insights-summary-value",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-summary-operations",
                                                        n_clicks=0,
                                                        className="stat-card-live insights-summary-card",
                                                        children=[
                                                            html.P(
                                                                "Operations",
                                                                className="insights-summary-title",
                                                            ),
                                                            html.P(
                                                                "-",
                                                                id="insights-summary-operations-value",
                                                                className="insights-summary-value",
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-summary-all",
                                                        n_clicks=0,
                                                        className="stat-card-live insights-summary-card insights-summary-all",
                                                        children=[
                                                            html.P(
                                                                "All",
                                                                className="insights-summary-title",
                                                            ),
                                                            html.P(
                                                                "Show every KPI group",
                                                                id="insights-summary-all-value",
                                                                className="insights-summary-value",
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-kpi-category-filter-wrap",
                                                style={"margin": "0.35rem 0 0.75rem"},
                                                children=[
                                                    dcc.RadioItems(
                                                        id="insights-kpi-category-filter",
                                                        options=[
                                                            {"label": "All", "value": "all"},
                                                            {"label": "Core", "value": "core"},
                                                            {
                                                                "label": "Community",
                                                                "value": "community",
                                                            },
                                                            {
                                                                "label": "Tournament",
                                                                "value": "tournament",
                                                            },
                                                            {
                                                                "label": "Operations",
                                                                "value": "operations",
                                                            },
                                                        ],
                                                        value="core",
                                                        inline=True,
                                                        labelStyle={
                                                            "marginRight": "0.75rem",
                                                            "fontSize": "0.75rem",
                                                            "color": COLORS["text_secondary"],
                                                        },
                                                        inputStyle={"marginRight": "0.3rem"},
                                                    ),
                                                    dcc.Checklist(
                                                        id="insights-kpi-auto-visibility-toggle",
                                                        options=[
                                                            {
                                                                "label": "Auto-hide not relevant KPIs",
                                                                "value": "auto",
                                                            }
                                                        ],
                                                        value=["auto"],
                                                        persistence=True,
                                                        persistence_type="local",
                                                        style={
                                                            "marginTop": "0.45rem",
                                                            "fontSize": "0.74rem",
                                                            "color": COLORS["text_muted"],
                                                        },
                                                        inputStyle={"marginRight": "0.3rem"},
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-kpi-grid",
                                                style={"gap": "0.75rem", "marginBottom": "0.65rem"},
                                                children=[
                                                    html.Div(
                                                        id="insights-kpi-label-core",
                                                        className="insights-kpi-section-label",
                                                        children="Core Event",
                                                        style={
                                                            "gridColumn": "1 / -1",
                                                            "fontSize": "0.7rem",
                                                            "color": COLORS["text_muted"],
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "marginBottom": "0.3rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="insights-card-total",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": f"3px solid #22d3ee",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0",
                                                                id="insights-kpi-total",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#22d3ee",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Participants",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-total-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-unique",
                                                                style={
                                                                    "color": "#a78bfa",
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.15rem",
                                                                    "fontWeight": "600",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-slots",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #06b6d4",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0",
                                                                id="insights-kpi-slots",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#06b6d4",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Checked-in Slots",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-slots-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-ready",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": f"3px solid {COLORS['accent_green']}",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0%",
                                                                id="insights-kpi-readyrate",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": COLORS["accent_green"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Ready Rate",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-readyrate-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-revenue",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #818cf8",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0 kr",
                                                                id="insights-kpi-revenue",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.6rem",
                                                                    "color": "#818cf8",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Total Revenue",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-revenue-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-kpi-label-community",
                                                        className="insights-kpi-section-label",
                                                        children="Community Health",
                                                        style={
                                                            "gridColumn": "1 / -1",
                                                            "fontSize": "0.7rem",
                                                            "color": COLORS["text_muted"],
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "marginTop": "0.2rem",
                                                            "marginBottom": "0.3rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="insights-card-new",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #34d399",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0",
                                                                id="insights-kpi-new",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#34d399",
                                                                },
                                                            ),
                                                            html.P(
                                                                "New Players",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-new-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-returning",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #fb923c",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0",
                                                                id="insights-kpi-returning",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#fb923c",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Returning",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-returning-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-coreplayers",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #22c55e",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0",
                                                                id="insights-kpi-coreplayers",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#22c55e",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Core Players",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-coreplayers-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-lifetime",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #38bdf8",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0.0",
                                                                id="insights-kpi-lifetime",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#38bdf8",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Player Lifetime",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-lifetime-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-growth",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #38bdf8",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "-",
                                                                id="insights-kpi-growth",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#38bdf8",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Growth Rate",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-growth-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-churn",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #f43f5e",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "-",
                                                                id="insights-kpi-churn",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#f43f5e",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Churn Rate",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-churn-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-retention",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": f"3px solid {COLORS['accent_red']}",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0%",
                                                                id="insights-kpi-retention",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": COLORS["accent_red"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Retention",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-retention-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-guest",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #2dd4bf",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0%",
                                                                id="insights-kpi-guestrate",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#2dd4bf",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Guest Share",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-guestrate-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-startgg",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": f"3px solid {COLORS['accent_yellow']}",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0%",
                                                                id="insights-kpi-startggrate",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": COLORS[
                                                                        "accent_yellow"
                                                                    ],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Start.gg Account Rate (excl guests)",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-startggrate-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-member",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": f"3px solid {COLORS['accent_purple']}",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0%",
                                                                id="insights-kpi-memberrate",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": COLORS[
                                                                        "accent_purple"
                                                                    ],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Member Rate",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-memberrate-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-kpi-label-tournament",
                                                        className="insights-kpi-section-label",
                                                        children="Tournament Health",
                                                        style={
                                                            "gridColumn": "1 / -1",
                                                            "fontSize": "0.7rem",
                                                            "color": COLORS["text_muted"],
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "marginTop": "0.2rem",
                                                            "marginBottom": "0.3rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="insights-card-avggames",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #f97316",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0.0",
                                                                id="insights-kpi-avggames",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#f97316",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Avg Games / Player",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-avggames-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-multigame",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #ec4899",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0.0%",
                                                                id="insights-kpi-multigame",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#ec4899",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Multi-Game Players",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-multigame-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-noshow",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #f87171",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0.0%",
                                                                id="insights-kpi-noshow",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#f87171",
                                                                },
                                                            ),
                                                            html.P(
                                                                "No-Show Rate",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-noshow-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-kpi-label-operations",
                                                        className="insights-kpi-section-label",
                                                        children="Operations",
                                                        style={
                                                            "gridColumn": "1 / -1",
                                                            "fontSize": "0.7rem",
                                                            "color": COLORS["text_muted"],
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "marginTop": "0.2rem",
                                                            "marginBottom": "0.3rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="insights-card-manual",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #94a3b8",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "0.0%",
                                                                id="insights-kpi-manual",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#94a3b8",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Manual Share",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-manual-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-checkinspeed",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #22d3ee",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "-",
                                                                id="insights-kpi-checkinspeed",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#22d3ee",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Check-in Speed",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-checkinspeed-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-card-duration",
                                                        n_clicks=0,
                                                        className="stat-card-live",
                                                        style={
                                                            **STYLES["stat_card"],
                                                            "minWidth": "0",
                                                            "padding": "0.8rem 0.65rem",
                                                            "borderTop": "3px solid #60a5fa",
                                                            "cursor": "pointer",
                                                        },
                                                        children=[
                                                            html.P(
                                                                "-",
                                                                id="insights-kpi-duration",
                                                                style={
                                                                    **STYLES["stat_value"],
                                                                    "fontSize": "1.85rem",
                                                                    "color": "#60a5fa",
                                                                },
                                                            ),
                                                            html.P(
                                                                "Tournament Duration",
                                                                style=STYLES["stat_label"],
                                                            ),
                                                            html.P(
                                                                "",
                                                                id="insights-kpi-duration-delta",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "0.35rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-kpi-help",
                                                style={
                                                    "color": COLORS["text_muted"],
                                                    "fontSize": "0.78rem",
                                                    "marginBottom": "0.9rem",
                                                },
                                            ),
                                            html.Div(
                                                id="insights-ops-live-note",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "fontSize": "0.76rem",
                                                    "marginBottom": "0.75rem",
                                                },
                                            ),
                                            html.Div(
                                                id="insights-view-players",
                                                children=[
                                                    html.Div(
                                                        id="insights-top-players-title",
                                                        style={
                                                            "color": COLORS["text_primary"],
                                                            "fontWeight": "600",
                                                            "marginBottom": "0.45rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "justifyContent": "space-between",
                                                            "alignItems": "center",
                                                            "gap": "0.75rem",
                                                            "marginBottom": "0.6rem",
                                                            "flexWrap": "wrap",
                                                        },
                                                        children=[
                                                            html.Div(
                                                                style={
                                                                    "minWidth": "260px",
                                                                },
                                                                children=dcc.Input(
                                                                    id="insights-top-players-search",
                                                                    type="text",
                                                                    placeholder="Search name or tag...",
                                                                    debounce=False,
                                                                    style={
                                                                        "backgroundColor": COLORS["bg_dark"],
                                                                        "color": COLORS["text_primary"],
                                                                        "border": f"1px solid {COLORS['border']}",
                                                                        "borderRadius": "6px",
                                                                        "padding": "0.45rem 0.6rem",
                                                                        "fontSize": "0.78rem",
                                                                        "height": "36px",
                                                                        "width": "100%",
                                                                    },
                                                                ),
                                                            ),
                                                            html.Div(
                                                                style={
                                                                    "display": "flex",
                                                                    "alignItems": "flex-end",
                                                                    "gap": "0.75rem",
                                                                    "flexWrap": "wrap",
                                                                },
                                                                children=[
                                                                    html.Div(
                                                                        style={
                                                                            "display": "flex",
                                                                            "flexDirection": "column",
                                                                            "gap": "0.2rem",
                                                                            "minWidth": "240px",
                                                                            "maxWidth": "240px",
                                                                            "width": "240px",
                                                                        },
                                                                        children=[
                                                                            html.Div(
                                                                                "Game filter",
                                                                                style={
                                                                                    "color": COLORS[
                                                                                        "text_muted"
                                                                                    ],
                                                                                    "fontSize": "0.68rem",
                                                                                    "textTransform": "uppercase",
                                                                                    "letterSpacing": "0.04em",
                                                                                },
                                                                            ),
                                                                            dcc.Dropdown(
                                                                                id="insights-top-players-game-filter",
                                                                                className="fgc-dropdown",
                                                                                options=[
                                                                                    {
                                                                                        "label": "All games",
                                                                                        "value": "all",
                                                                                    }
                                                                                ],
                                                                                value="all",
                                                                                clearable=False,
                                                                                style={
                                                                                    "fontSize": "0.78rem",
                                                                                    "width": "100%",
                                                                                },
                                                                            ),
                                                                        ],
                                                                    ),
                                                                    html.Div(
                                                                        style={
                                                                            "display": "flex",
                                                                            "flexDirection": "column",
                                                                            "gap": "0.2rem",
                                                                            "minWidth": "120px",
                                                                        },
                                                                        children=[
                                                                            html.Div(
                                                                                "Rows shown",
                                                                                style={
                                                                                    "color": COLORS[
                                                                                        "text_muted"
                                                                                    ],
                                                                                    "fontSize": "0.68rem",
                                                                                    "textTransform": "uppercase",
                                                                                    "letterSpacing": "0.04em",
                                                                                },
                                                                            ),
                                                                            dcc.Dropdown(
                                                                                id="insights-top-players-limit",
                                                                                className="fgc-dropdown",
                                                                                options=[
                                                                                    {
                                                                                        "label": "15",
                                                                                        "value": 15,
                                                                                    },
                                                                                    {
                                                                                        "label": "30",
                                                                                        "value": 30,
                                                                                    },
                                                                                    {
                                                                                        "label": "50",
                                                                                        "value": 50,
                                                                                    },
                                                                                    {
                                                                                        "label": "All",
                                                                                        "value": "all",
                                                                                    },
                                                                                ],
                                                                                value=15,
                                                                                clearable=False,
                                                                                style={"fontSize": "0.78rem"},
                                                                            ),
                                                                        ],
                                                                    ),
                                                                ],
                                                            ),
                                                        ],
                                                    ),
                                                    dash_table.DataTable(
                                                        id="insights-top-players-table",
                                                        columns=[
                                                            {"name": "#", "id": "rank"},
                                                            {"name": "Name", "id": "name"},
                                                            {"name": "Tag", "id": "tag"},
                                                            {
                                                                "name": "Events",
                                                                "id": "events_attended",
                                                            },
                                                        ],
                                                        data=[],
                                                        page_size=8,
                                                        sort_action="native",
                                                        style_table={
                                                            "overflowX": "auto",
                                                            "marginBottom": "1rem",
                                                        },
                                                        style_header={
                                                            "backgroundColor": COLORS["bg_dark"],
                                                            "color": COLORS["text_primary"],
                                                            "fontWeight": "600",
                                                            "fontSize": "0.72rem",
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "padding": "0.7rem",
                                                            "borderBottom": f"2px solid {COLORS['accent_green']}",
                                                        },
                                                        style_cell={
                                                            "backgroundColor": COLORS["bg_card"],
                                                            "color": COLORS["text_primary"],
                                                            "border": "none",
                                                            "borderBottom": f"1px solid {COLORS['border']}",
                                                            "padding": "0.62rem 0.8rem",
                                                            "fontSize": "0.78rem",
                                                            "textAlign": "left",
                                                        },
                                                        style_data_conditional=[
                                                            {
                                                                "if": {"row_index": "odd"},
                                                                "backgroundColor": COLORS[
                                                                    "bg_dark"
                                                                ],
                                                            },
                                                        ],
                                                    ),
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "justifyContent": "space-between",
                                                            "alignItems": "center",
                                                            "marginBottom": "0.45rem",
                                                            "gap": "0.6rem",
                                                            "flexWrap": "wrap",
                                                        },
                                                        children=[
                                                            html.Div(
                                                                "Player funnel",
                                                                style={
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "fontSize": "0.84rem",
                                                                },
                                                            ),
                                                            html.Div(
                                                                id="insights-player-funnel-note",
                                                                style={
                                                                    "color": COLORS["text_muted"],
                                                                    "fontSize": "0.72rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-player-funnel",
                                                        style={
                                                            "display": "grid",
                                                            "gridTemplateColumns": "repeat(3, minmax(160px, 1fr))",
                                                            "gap": "0.55rem",
                                                            "marginBottom": "0.9rem",
                                                        },
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-view-games",
                                                children=[
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "justifyContent": "flex-start",
                                                            "alignItems": "center",
                                                            "flexWrap": "wrap",
                                                            "gap": "0.75rem",
                                                            "marginBottom": "0.6rem",
                                                        },
                                                        children=[
                                                            html.Div(
                                                                id="insights-games-title",
                                                                style={
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "minWidth": "160px",
                                                                },
                                                            ),
                                                            html.Div(
                                                                id="insights-top-game",
                                                                style={
                                                                    "display": "none",
                                                                },
                                                            ),
                                                            html.Div(
                                                                id="insights-added-via-summary",
                                                                style={
                                                                    "display": "none",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    dcc.Dropdown(
                                                        id="insights-games-table-view",
                                                        className="games-table-view-dropdown",
                                                        options=[
                                                            {
                                                                "label": "Overview",
                                                                "value": "distribution",
                                                            },
                                                            {
                                                                "label": "Crossovers",
                                                                "value": "crossovers",
                                                            },
                                                            {
                                                                "label": "Trends",
                                                                "value": "trends",
                                                            },
                                                        ],
                                                        value="distribution",
                                                        clearable=False,
                                                        style={"maxWidth": "280px", "marginBottom": "0.55rem"},
                                                    ),
                                                    html.Div(id="insights-games-scroll-lock", style={"display": "none"}),
                                                    html.Div(
                                                        id="insights-games-popularity-wrap",
                                                        children=[
                                                            dash_table.DataTable(
                                                                id="insights-games-table",
                                                                columns=[
                                                                    {"name": "#", "id": "rank"},
                                                                    {"name": "Game", "id": "game"},
                                                                    {
                                                                        "name": "Check-ins",
                                                                        "id": "entries",
                                                                    },
                                                                    {
                                                                        "name": "Registered",
                                                                        "id": "registered",
                                                                    },
                                                                    {
                                                                        "name": "Sets played",
                                                                        "id": "sets_played",
                                                                    },
                                                                    {
                                                                        "name": "Games played",
                                                                        "id": "games_played",
                                                                    },
                                                                    {
                                                                        "name": "Run status",
                                                                        "id": "run_status",
                                                                    },
                                                                    {"name": "Share", "id": "share"},
                                                                ],
                                                                data=[],
                                                                page_size=8,
                                                                sort_action="native",
                                                                style_table={
                                                                    "overflowX": "auto",
                                                                    "height": "280px",
                                                                    "overflowY": "auto",
                                                                    "marginBottom": "1rem",
                                                                },
                                                                style_header={
                                                                    "backgroundColor": COLORS["bg_dark"],
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "fontSize": "0.72rem",
                                                                    "textTransform": "uppercase",
                                                                    "letterSpacing": "0.05em",
                                                                    "padding": "0.7rem",
                                                                    "borderBottom": f"2px solid {COLORS['accent_yellow']}",
                                                                },
                                                                style_cell={
                                                                    "backgroundColor": COLORS["bg_card"],
                                                                    "color": COLORS["text_primary"],
                                                                    "border": "none",
                                                                    "borderBottom": f"1px solid {COLORS['border']}",
                                                                    "padding": "0.62rem 0.8rem",
                                                                    "fontSize": "0.78rem",
                                                                    "textAlign": "left",
                                                                },
                                                                style_data_conditional=[
                                                                    {
                                                                        "if": {"row_index": "odd"},
                                                                        "backgroundColor": COLORS["bg_dark"],
                                                                    },
                                                                ],
                                                            ),
                                                            html.Div(
                                                                "Tip: scroll horizontally in table to see all columns.",
                                                                style={
                                                                    "color": COLORS["text_secondary"],
                                                                    "fontSize": "0.72rem",
                                                                    "marginTop": "-0.5rem",
                                                                    "marginBottom": "0.65rem",
                                                                },
                                                            ),
                                                            html.Div(
                                                                style={
                                                                    "display": "grid",
                                                                    "gridTemplateColumns": "minmax(260px, 360px) minmax(220px, 1fr)",
                                                                    "gap": "0.9rem",
                                                                    "alignItems": "stretch",
                                                                    "marginBottom": "0.9rem",
                                                                },
                                                                children=[
                                                                    dcc.Graph(
                                                                        id="insights-games-pie",
                                                                        config={"displayModeBar": False},
                                                                        style={"height": "260px"},
                                                                    ),
                                                                    html.Div(
                                                                        id="insights-games-pie-legend",
                                                                        style={
                                                                            "border": f"1px solid {COLORS['border']}",
                                                                            "borderRadius": "10px",
                                                                            "padding": "0.75rem 0.85rem",
                                                                            "backgroundColor": COLORS["bg_dark"],
                                                                        },
                                                                    ),
                                                                ],
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-games-crossover-wrap",
                                                        style={"display": "none"},
                                                        children=[
                                                            html.Div(
                                                                id="insights-crossover-title",
                                                                style={
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "marginBottom": "0.45rem",
                                                                    "minHeight": "1.2rem",
                                                                },
                                                            ),
                                                            html.Div(
                                                                id="insights-crossover-table-wrap",
                                                                children=[
                                                                    dash_table.DataTable(
                                                                        id="insights-crossover-table",
                                                                        columns=[
                                                                            {"name": "#", "id": "rank"},
                                                                            {"name": "Game A", "id": "game_a"},
                                                                            {"name": "Game B", "id": "game_b"},
                                                                            {
                                                                                "name": "Shared players",
                                                                                "id": "shared_players",
                                                                            },
                                                                            {"name": "Share", "id": "share"},
                                                                        ],
                                                                        data=[],
                                                                        page_size=8,
                                                                        style_table={
                                                                            "overflowX": "auto",
                                                                            "height": "280px",
                                                                            "overflowY": "auto",
                                                                            "marginBottom": "1rem",
                                                                        },
                                                                        style_header={
                                                                            "backgroundColor": COLORS["bg_dark"],
                                                                            "color": COLORS["text_primary"],
                                                                            "fontWeight": "600",
                                                                            "fontSize": "0.72rem",
                                                                            "textTransform": "uppercase",
                                                                            "letterSpacing": "0.05em",
                                                                            "padding": "0.7rem",
                                                                            "borderBottom": f"2px solid {COLORS['accent_blue']}",
                                                                        },
                                                                        style_cell={
                                                                            "backgroundColor": COLORS["bg_card"],
                                                                            "color": COLORS["text_primary"],
                                                                            "border": "none",
                                                                            "borderBottom": f"1px solid {COLORS['border']}",
                                                                            "padding": "0.62rem 0.8rem",
                                                                            "fontSize": "0.78rem",
                                                                            "textAlign": "left",
                                                                        },
                                                                        style_data_conditional=[
                                                                            {
                                                                                "if": {"row_index": "odd"},
                                                                                "backgroundColor": COLORS[
                                                                                    "bg_dark"
                                                                                ],
                                                                            },
                                                                        ],
                                                                    )
                                                                ],
                                                            ),
                                                            html.Div(
                                                                id="insights-crossover-heatmap-wrap",
                                                                children=[
                                                                    dcc.Graph(
                                                                        id="insights-crossover-heatmap",
                                                                        config={"displayModeBar": False},
                                                                        style={
                                                                            "height": "320px",
                                                                            "marginBottom": "0.9rem",
                                                                        },
                                                                    )
                                                                ],
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-games-trends-wrap",
                                                        style={"display": "none"},
                                                        children=[
                                                            html.Div(
                                                                "Game trends",
                                                                style={
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "marginBottom": "0.45rem",
                                                                    "minHeight": "1.2rem",
                                                                },
                                                            ),
                                                            html.Div(
                                                                id="insights-game-mover",
                                                                style={
                                                                    "color": COLORS["text_secondary"],
                                                                    "fontSize": "0.76rem",
                                                                    "marginBottom": "0.5rem",
                                                                },
                                                            ),
                                                            dcc.Graph(
                                                                id="insights-games-trend",
                                                                config={"displayModeBar": False},
                                                                style={
                                                                    "height": "260px",
                                                                    "marginBottom": "0.75rem",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-view-events",
                                                children=[
                                                    dcc.Dropdown(
                                                        id="insights-events-table-view",
                                                        className="games-table-view-dropdown",
                                                        options=[
                                                            {"label": "Event overview", "value": "overview"},
                                                            {
                                                                "label": "Operations/quality",
                                                                "value": "ops_quality",
                                                            },
                                                        ],
                                                        value="overview",
                                                        clearable=False,
                                                        style={"maxWidth": "280px", "marginBottom": "0.55rem"},
                                                    ),
                                                    html.Div(
                                                        id="insights-events-overview-wrap",
                                                        children=[
                                                            dash_table.DataTable(
                                                                id="insights-events-table",
                                                                columns=[
                                                                    {
                                                                        "name": "Event",
                                                                        "id": "event_display_name",
                                                                    },
                                                                    {"name": "Date", "id": "event_date"},
                                                                    {
                                                                        "name": "Participants (excl no-shows)",
                                                                        "id": "total_participants",
                                                                    },
                                                                    {
                                                                        "name": "Top game",
                                                                        "id": "top_game",
                                                                    },
                                                                    {
                                                                        "name": "Revenue",
                                                                        "id": "total_revenue",
                                                                    },
                                                                    {
                                                                        "name": "Retention %",
                                                                        "id": "retention_rate",
                                                                    },
                                                                    {
                                                                        "name": "Start.gg Accounts (excl guests) %",
                                                                        "id": "startgg_rate",
                                                                    },
                                                                    {
                                                                        "name": "Member %",
                                                                        "id": "member_rate",
                                                                    },
                                                                ],
                                                                data=[],
                                                                page_size=10,
                                                                sort_action="native",
                                                                style_table={"overflowX": "auto"},
                                                                style_header={
                                                                    "backgroundColor": COLORS["bg_dark"],
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "fontSize": "0.75rem",
                                                                    "textTransform": "uppercase",
                                                                    "letterSpacing": "0.05em",
                                                                    "padding": "0.75rem",
                                                                    "borderBottom": f"2px solid {COLORS['accent_blue']}",
                                                                },
                                                                style_cell={
                                                                    "backgroundColor": COLORS["bg_card"],
                                                                    "color": COLORS["text_primary"],
                                                                    "border": "none",
                                                                    "borderBottom": f"1px solid {COLORS['border']}",
                                                                    "padding": "0.55rem 0.75rem",
                                                                    "fontSize": "0.8rem",
                                                                    "textAlign": "left",
                                                                    "maxWidth": "220px",
                                                                    "overflow": "hidden",
                                                                    "textOverflow": "ellipsis",
                                                                },
                                                                style_data_conditional=[
                                                                    {
                                                                        "if": {"row_index": "odd"},
                                                                        "backgroundColor": COLORS[
                                                                            "bg_dark"
                                                                        ],
                                                                    },
                                                                ],
                                                            )
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="insights-events-ops-wrap",
                                                        style={"display": "none"},
                                                        children=[
                                                            dash_table.DataTable(
                                                                id="insights-events-ops-table",
                                                                columns=[
                                                                    {
                                                                        "name": "Event",
                                                                        "id": "event_display_name",
                                                                    },
                                                                    {"name": "Date", "id": "event_date"},
                                                                    {
                                                                        "name": "Checked-in/Reg",
                                                                        "id": "checked_in_vs_registered",
                                                                    },
                                                                    {"name": "No-shows", "id": "no_show_count"},
                                                                    {"name": "No-show %", "id": "no_show_rate"},
                                                                    {
                                                                        "name": "No-show band",
                                                                        "id": "no_show_band",
                                                                    },
                                                                    {
                                                                        "name": "Manual adds",
                                                                        "id": "manual_count",
                                                                    },
                                                                    {
                                                                        "name": "Manual %",
                                                                        "id": "manual_share",
                                                                    },
                                                                    {
                                                                        "name": "Attendance/Op quality",
                                                                        "id": "event_quality",
                                                                    },
                                                                    {"name": "Flag", "id": "ops_flag"},
                                                                ],
                                                                data=[],
                                                                page_size=10,
                                                                sort_action="native",
                                                                style_table={"overflowX": "auto"},
                                                                style_header={
                                                                    "backgroundColor": COLORS["bg_dark"],
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "fontSize": "0.75rem",
                                                                    "textTransform": "uppercase",
                                                                    "letterSpacing": "0.05em",
                                                                    "padding": "0.75rem",
                                                                    "borderBottom": f"2px solid {COLORS['accent_yellow']}",
                                                                },
                                                                style_cell={
                                                                    "backgroundColor": COLORS["bg_card"],
                                                                    "color": COLORS["text_primary"],
                                                                    "border": "none",
                                                                    "borderBottom": f"1px solid {COLORS['border']}",
                                                                    "padding": "0.55rem 0.75rem",
                                                                    "fontSize": "0.8rem",
                                                                    "textAlign": "left",
                                                                    "maxWidth": "220px",
                                                                    "overflow": "hidden",
                                                                    "textOverflow": "ellipsis",
                                                                },
                                                                style_data_conditional=[
                                                                    {
                                                                        "if": {"row_index": "odd"},
                                                                        "backgroundColor": COLORS[
                                                                            "bg_dark"
                                                                        ],
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": "{no_show_rate} >= 40",
                                                                            "column_id": "no_show_rate",
                                                                        },
                                                                        "color": "#fca5a5",
                                                                        "fontWeight": "700",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": "{no_show_rate} >= 30 && {no_show_rate} < 40",
                                                                            "column_id": "no_show_rate",
                                                                        },
                                                                        "color": "#fdba74",
                                                                        "fontWeight": "700",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": "{no_show_rate} >= 18 && {no_show_rate} < 30",
                                                                            "column_id": "no_show_rate",
                                                                        },
                                                                        "color": "#fde68a",
                                                                        "fontWeight": "600",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": "{no_show_rate} >= 8 && {no_show_rate} < 18",
                                                                            "column_id": "no_show_rate",
                                                                        },
                                                                        "color": "#93c5fd",
                                                                        "fontWeight": "600",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": "{no_show_rate} < 8",
                                                                            "column_id": "no_show_rate",
                                                                        },
                                                                        "color": "#86efac",
                                                                        "fontWeight": "600",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": '{event_quality} contains "Critical"',
                                                                            "column_id": "event_quality",
                                                                        },
                                                                        "color": "#fecaca",
                                                                        "fontWeight": "700",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": '{event_quality} contains "Watch"',
                                                                            "column_id": "event_quality",
                                                                        },
                                                                        "color": "#fdba74",
                                                                        "fontWeight": "700",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": '{event_quality} contains "Stable"',
                                                                            "column_id": "event_quality",
                                                                        },
                                                                        "color": "#93c5fd",
                                                                        "fontWeight": "600",
                                                                    },
                                                                    {
                                                                        "if": {
                                                                            "filter_query": '{event_quality} contains "Healthy"',
                                                                            "column_id": "event_quality",
                                                                        },
                                                                        "color": "#86efac",
                                                                        "fontWeight": "700",
                                                                    },
                                                                ],
                                                            )
                                                        ],
                                                    ),
                                                    html.Div(
                                                        "No-show bands: <8 low, 8-17 normal, 18-29 elevated, 30-39 high, 40+ critical. Attendance/Op quality scale: Critical [1/4], Watch [2/4], Stable [3/4], Healthy [4/4].",
                                                        style={
                                                            "color": COLORS["text_secondary"],
                                                            "fontSize": "0.72rem",
                                                            "marginTop": "0.38rem",
                                                            "marginBottom": "0.7rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        "Event trends",
                                                        style={
                                                            "color": COLORS["text_primary"],
                                                            "fontWeight": "600",
                                                            "marginBottom": "0.45rem",
                                                            "minHeight": "1.2rem",
                                                        },
                                                    ),
                                                    dcc.Graph(
                                                        id="insights-events-noshow-trend",
                                                        config={"displayModeBar": False},
                                                        style={"height": "230px", "marginBottom": "0.75rem"},
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                id="insights-view-earnings",
                                                children=[
                                                    dash_table.DataTable(
                                                        id="insights-earnings-table",
                                                        columns=[
                                                            {
                                                                "name": "Event",
                                                                "id": "event_display_name",
                                                            },
                                                            {"name": "Date", "id": "event_date"},
                                                            {
                                                                "name": "Participants (excl no-shows)",
                                                                "id": "total_participants",
                                                            },
                                                            {
                                                                "name": "Revenue",
                                                                "id": "total_revenue",
                                                            },
                                                            {
                                                                "name": "SEK / Player",
                                                                "id": "revenue_per_player",
                                                            },
                                                        ],
                                                        data=[],
                                                        page_size=10,
                                                        sort_action="native",
                                                        style_table={"overflowX": "auto"},
                                                        style_header={
                                                            "backgroundColor": COLORS["bg_dark"],
                                                            "color": COLORS["text_primary"],
                                                            "fontWeight": "600",
                                                            "fontSize": "0.75rem",
                                                            "textTransform": "uppercase",
                                                            "letterSpacing": "0.05em",
                                                            "padding": "0.75rem",
                                                            "borderBottom": f"2px solid {COLORS['accent_green']}",
                                                        },
                                                        style_cell={
                                                            "backgroundColor": COLORS["bg_card"],
                                                            "color": COLORS["text_primary"],
                                                            "border": "none",
                                                            "borderBottom": f"1px solid {COLORS['border']}",
                                                            "padding": "0.55rem 0.75rem",
                                                            "fontSize": "0.8rem",
                                                            "textAlign": "left",
                                                        },
                                                        style_data_conditional=[
                                                            {
                                                                "if": {"row_index": "odd"},
                                                                "backgroundColor": COLORS[
                                                                    "bg_dark"
                                                                ],
                                                            },
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            # -- Duplicates view --
                                            html.Div(
                                                id="insights-view-duplicates",
                                                style={"display": "none"},
                                                children=[
                                                    html.Div(
                                                        style={
                                                            "display": "flex",
                                                            "justifyContent": "space-between",
                                                            "alignItems": "center",
                                                            "marginBottom": "0.8rem",
                                                        },
                                                        children=[
                                                            html.Div(
                                                                id="duplicates-title",
                                                                children="Potential duplicates",
                                                                style={
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Scan for duplicates",
                                                                id="btn-scan-duplicates",
                                                                n_clicks=0,
                                                                style={
                                                                    "backgroundColor": COLORS[
                                                                        "accent_blue"
                                                                    ],
                                                                    "color": "#fff",
                                                                    "border": "none",
                                                                    "padding": "0.4rem 0.9rem",
                                                                    "borderRadius": "6px",
                                                                    "cursor": "pointer",
                                                                    "fontSize": "0.78rem",
                                                                    "fontWeight": "500",
                                                                },
                                                            ),
                                                        ],
                                                    ),
                                                    html.Div(
                                                        id="duplicates-feedback",
                                                        style={
                                                            "color": COLORS["text_secondary"],
                                                            "fontSize": "0.8rem",
                                                            "marginBottom": "0.6rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="duplicates-list",
                                                        children=[],
                                                    ),
                                                    # Merge history section
                                                    html.Hr(
                                                        style={
                                                            "borderColor": COLORS["border"],
                                                            "margin": "1.5rem 0 1rem",
                                                        }
                                                    ),
                                                    html.Div(
                                                        "Merge history",
                                                        style={
                                                            "color": COLORS["text_primary"],
                                                            "fontWeight": "600",
                                                            "marginBottom": "0.6rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        id="merge-history-list",
                                                        children=[
                                                            html.Span(
                                                                "No merges yet.",
                                                                style={
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "fontSize": "0.8rem",
                                                                },
                                                            )
                                                        ],
                                                    ),
                                                    # Hidden stores for merge confirmation
                                                    dcc.Store(id="merge-keep-uuid"),
                                                    dcc.Store(id="merge-remove-uuid"),
                                                    dcc.ConfirmDialog(
                                                        id="merge-confirm-dialog",
                                                        message="",
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                            # ========== TAB 3: Settings ==========
                            html.Div(
                                id="tab-settings-content",
                                style={"display": "none"},
                                children=[
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3(
                                                "Event Configuration", style=STYLES["section_title"]
                                            ),
                                            html.Div(
                                                style={"marginBottom": "1.5rem"},
                                                children=[
                                                    html.Label(
                                                        "Start.gg Tournament Link",
                                                        style={
                                                            "fontSize": "0.875rem",
                                                            "color": COLORS["text_secondary"],
                                                            "marginBottom": "0.5rem",
                                                            "display": "block",
                                                        },
                                                    ),
                                                    dcc.Input(
                                                        id="input-startgg-link",
                                                        type="text",
                                                        placeholder="https://www.start.gg/tournament/your-tournament",
                                                        style={
                                                            **STYLES["input"],
                                                            "minHeight": "44px",
                                                            "lineHeight": "1.4",
                                                            "boxSizing": "border-box",
                                                        },
                                                    ),
                                                ],
                                            ),
                                            html.Button(
                                                "Fetch Event Data",
                                                id="btn-fetch-event",
                                                n_clicks=0,
                                                style=STYLES["button_primary"],
                                            ),
                                            html.Div(
                                                id="settings-output",
                                                style={
                                                    "marginTop": "1rem",
                                                    "padding": "1rem",
                                                    "borderRadius": "8px",
                                                    "backgroundColor": COLORS["bg_dark"],
                                                },
                                            ),
                                        ],
                                    ),
                                    # Check-in Requirements
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3(
                                                "Check-in Requirements",
                                                style=STYLES["section_title"],
                                            ),
                                            html.P(
                                                "Choose which requirements players must meet to be marked as 'Ready'.",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            # Require Payment
                                            html.Div(
                                                style={
                                                    "marginBottom": "1rem",
                                                    "display": "flex",
                                                    "alignItems": "center",
                                                    "gap": "0.75rem",
                                                },
                                                children=[
                                                    dcc.Checklist(
                                                        id="require-payment-toggle",
                                                        options=[{"label": "", "value": True}],
                                                        value=(
                                                            [True]
                                                            if settings.get("require_payment")
                                                            is True
                                                            else []
                                                        ),
                                                        style={"display": "inline-block"},
                                                        inputStyle={
                                                            "width": "18px",
                                                            "height": "18px",
                                                            "cursor": "pointer",
                                                        },
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Span(
                                                                "Require Payment",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Player must pay to be marked as Ready",
                                                                style={
                                                                    "margin": "0",
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS["text_muted"],
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            # Require Membership
                                            html.Div(
                                                style={
                                                    "marginBottom": "1rem",
                                                    "display": "flex",
                                                    "alignItems": "center",
                                                    "gap": "0.75rem",
                                                },
                                                children=[
                                                    dcc.Checklist(
                                                        id="require-membership-toggle",
                                                        options=[{"label": "", "value": True}],
                                                        value=(
                                                            [True]
                                                            if settings.get("require_membership")
                                                            is True
                                                            else []
                                                        ),
                                                        style={"display": "inline-block"},
                                                        inputStyle={
                                                            "width": "18px",
                                                            "height": "18px",
                                                            "cursor": "pointer",
                                                        },
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Span(
                                                                "Require Membership (eBas)",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Player must be a Sverok member",
                                                                style={
                                                                    "margin": "0",
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS["text_muted"],
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            # Require Start.gg
                                            html.Div(
                                                style={
                                                    "marginBottom": "1.5rem",
                                                    "display": "flex",
                                                    "alignItems": "center",
                                                    "gap": "0.75rem",
                                                },
                                                children=[
                                                    dcc.Checklist(
                                                        id="require-startgg-toggle",
                                                        options=[{"label": "", "value": True}],
                                                        value=(
                                                            [True]
                                                            if settings.get("require_startgg")
                                                            is True
                                                            else []
                                                        ),
                                                        style={"display": "inline-block"},
                                                        inputStyle={
                                                            "width": "18px",
                                                            "height": "18px",
                                                            "cursor": "pointer",
                                                        },
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Span(
                                                                "Require Start.gg Registration",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Player must be registered in the tournament",
                                                                style={
                                                                    "margin": "0",
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS["text_muted"],
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            html.Hr(
                                                style={
                                                    "border": "none",
                                                    "borderTop": f"1px solid {COLORS['border']}",
                                                    "margin": "1.5rem 0",
                                                }
                                            ),
                                            # Offer Membership (optional)
                                            html.Div(
                                                style={
                                                    "marginBottom": "1.5rem",
                                                    "display": "flex",
                                                    "alignItems": "center",
                                                    "gap": "0.75rem",
                                                },
                                                children=[
                                                    dcc.Checklist(
                                                        id="offer-membership-toggle",
                                                        options=[{"label": "", "value": True}],
                                                        value=(
                                                            [True]
                                                            if settings.get("offer_membership")
                                                            is True
                                                            else []
                                                        ),
                                                        style={"display": "inline-block"},
                                                        inputStyle={
                                                            "width": "18px",
                                                            "height": "18px",
                                                            "cursor": "pointer",
                                                        },
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Span(
                                                                "Offer Membership (optional)",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Show 'Become a member' on Ready page even when not required",
                                                                style={
                                                                    "margin": "0",
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS["text_muted"],
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            html.Div(
                                                style={
                                                    "marginBottom": "1.5rem",
                                                    "display": "flex",
                                                    "alignItems": "center",
                                                    "gap": "0.75rem",
                                                },
                                                children=[
                                                    dcc.Checklist(
                                                        id="collect-acquisition-source-toggle",
                                                        options=[{"label": "", "value": True}],
                                                        value=(
                                                            [True]
                                                            if settings.get(
                                                                "collect_acquisition_source"
                                                            )
                                                            is True
                                                            else []
                                                        ),
                                                        style={"display": "inline-block"},
                                                        inputStyle={
                                                            "width": "18px",
                                                            "height": "18px",
                                                            "cursor": "pointer",
                                                        },
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Span(
                                                                "Collect Acquisition Source (optional)",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Ask how the player found the event (friend/discord/start.gg/etc).",
                                                                style={
                                                                    "margin": "0",
                                                                    "fontSize": "0.75rem",
                                                                    "color": COLORS["text_muted"],
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            html.Button(
                                                "Save Requirements",
                                                id="btn-save-requirements",
                                                n_clicks=0,
                                                style=STYLES["button_primary"],
                                            ),
                                            html.Div(
                                                id="requirements-save-feedback",
                                                style={"marginTop": "1rem"},
                                            ),
                                        ],
                                    ),
                                    # Payment Settings
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3(
                                                "Payment Settings", style=STYLES["section_title"]
                                            ),
                                            html.P(
                                                "Configure Swish payment details for this event.",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            # Price per game
                                            html.Div(
                                                style={"marginBottom": "1rem"},
                                                children=[
                                                    html.Label(
                                                        "Price per game (kr)",
                                                        style={
                                                            "fontWeight": "600",
                                                            "color": COLORS["text_primary"],
                                                            "marginBottom": "0.5rem",
                                                            "display": "block",
                                                        },
                                                    ),
                                                    dcc.Input(
                                                        id="input-price-per-game",
                                                        type="number",
                                                        value=settings.get(
                                                            "swish_expected_per_game", 25
                                                        ),
                                                        min=0,
                                                        step=5,
                                                        style={
                                                            "width": "120px",
                                                            "padding": "0.5rem",
                                                            "borderRadius": "6px",
                                                            "border": f"1px solid {COLORS['border']}",
                                                            "backgroundColor": COLORS["bg_dark"],
                                                            "color": COLORS["text_primary"],
                                                        },
                                                    ),
                                                ],
                                            ),
                                            # Swish number
                                            html.Div(
                                                style={"marginBottom": "1rem"},
                                                children=[
                                                    html.Label(
                                                        "Swish number",
                                                        style={
                                                            "fontWeight": "600",
                                                            "color": COLORS["text_primary"],
                                                            "marginBottom": "0.5rem",
                                                            "display": "block",
                                                        },
                                                    ),
                                                    dcc.Input(
                                                        id="input-swish-number",
                                                        type="text",
                                                        value=settings.get(
                                                            "swish_number", "123-456 78 90"
                                                        ),
                                                        style={
                                                            "width": "200px",
                                                            "padding": "0.5rem",
                                                            "borderRadius": "6px",
                                                            "border": f"1px solid {COLORS['border']}",
                                                            "backgroundColor": COLORS["bg_dark"],
                                                            "color": COLORS["text_primary"],
                                                        },
                                                    ),
                                                ],
                                            ),
                                            html.Button(
                                                "Save Payment Settings",
                                                id="btn-save-payment-settings",
                                                n_clicks=0,
                                                style=STYLES["button_primary"],
                                            ),
                                            html.Div(
                                                id="payment-settings-feedback",
                                                style={"marginTop": "1rem"},
                                            ),
                                        ],
                                    ),
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3(
                                                "Operations Timing", style=STYLES["section_title"]
                                            ),
                                            html.P(
                                                "Track check-in opening and tournament start/end for live operations KPIs.",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            html.Div(
                                                style={
                                                    "display": "grid",
                                                    "gridTemplateColumns": "repeat(auto-fit, minmax(230px, 1fr))",
                                                    "gap": "0.8rem",
                                                },
                                                children=[
                                                    html.Div(
                                                        children=[
                                                            html.Label(
                                                                "Check-in opened at",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                    "marginBottom": "0.35rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Input(
                                                                id="input-checkin-opened-at",
                                                                type="text",
                                                                placeholder="YYYY-MM-DDTHH:MM",
                                                                value=checkin_opened_local,
                                                                style={
                                                                    "width": "100%",
                                                                    "padding": "0.5rem",
                                                                    "borderRadius": "6px",
                                                                    "border": f"1px solid {COLORS['border']}",
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ],
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Set now",
                                                                id="btn-set-checkin-opened-now",
                                                                n_clicks=0,
                                                                style={
                                                                    **STYLES["button_secondary"],
                                                                    "marginTop": "0.45rem",
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Label(
                                                                "Event started at",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                    "marginBottom": "0.35rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Input(
                                                                id="input-event-started-at",
                                                                type="text",
                                                                placeholder="YYYY-MM-DDTHH:MM",
                                                                value=event_started_local,
                                                                style={
                                                                    "width": "100%",
                                                                    "padding": "0.5rem",
                                                                    "borderRadius": "6px",
                                                                    "border": f"1px solid {COLORS['border']}",
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ],
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Set now",
                                                                id="btn-set-event-started-now",
                                                                n_clicks=0,
                                                                style={
                                                                    **STYLES["button_secondary"],
                                                                    "marginTop": "0.45rem",
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        children=[
                                                            html.Label(
                                                                "Event ended at",
                                                                style={
                                                                    "fontWeight": "600",
                                                                    "color": COLORS["text_primary"],
                                                                    "marginBottom": "0.35rem",
                                                                    "display": "block",
                                                                },
                                                            ),
                                                            dcc.Input(
                                                                id="input-event-ended-at",
                                                                type="text",
                                                                placeholder="YYYY-MM-DDTHH:MM",
                                                                value=event_ended_local,
                                                                style={
                                                                    "width": "100%",
                                                                    "padding": "0.5rem",
                                                                    "borderRadius": "6px",
                                                                    "border": f"1px solid {COLORS['border']}",
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ],
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Set now",
                                                                id="btn-set-event-ended-now",
                                                                n_clicks=0,
                                                                style={
                                                                    **STYLES["button_secondary"],
                                                                    "marginTop": "0.45rem",
                                                                },
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                            ),
                                            html.Button(
                                                "Save Timing",
                                                id="btn-save-ops-timing",
                                                n_clicks=0,
                                                style={
                                                    **STYLES["button_primary"],
                                                    "marginTop": "0.95rem",
                                                },
                                            ),
                                            html.Div(
                                                id="ops-timing-feedback",
                                                style={"marginTop": "0.8rem"},
                                            ),
                                        ],
                                    ),
                                    # Archive Event
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3("Event Archive", style=STYLES["section_title"]),
                                            html.P(
                                                "Archive current event data to historical tables (event_archive + event_stats).",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            dcc.Checklist(
                                                id="archive-clear-active-toggle",
                                                options=[
                                                    {
                                                        "label": " Clear active check-ins after archive",
                                                        "value": "clear",
                                                    }
                                                ],
                                                value=[],
                                                style={
                                                    "marginBottom": "1rem",
                                                    "color": COLORS["text_secondary"],
                                                },
                                                inputStyle={"marginRight": "0.5rem"},
                                            ),
                                            html.Button(
                                                "Archive Current Event",
                                                id="btn-archive-event",
                                                n_clicks=0,
                                                style=STYLES["button_primary"],
                                            ),
                                            html.P(
                                                "Tip: quick archive button is available next to Refresh in Live Check-ins.",
                                                style={
                                                    "marginTop": "0.75rem",
                                                    "color": COLORS["text_muted"],
                                                    "fontSize": "0.82rem",
                                                },
                                            ),
                                            html.Div(
                                                id="archive-feedback", style={"marginTop": "1rem"}
                                            ),
                                            html.Hr(
                                                style={
                                                    "border": "none",
                                                    "borderTop": f"1px solid {COLORS['border']}",
                                                    "margin": "1.25rem 0",
                                                }
                                            ),
                                            html.H3("Reopen Event", style=STYLES["section_title"]),
                                            html.P(
                                                "Reopen an archived event to restore check-in data.",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            html.Label(
                                                "Archived events",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "fontSize": "0.85rem",
                                                    "marginBottom": "0.3rem",
                                                    "display": "block",
                                                },
                                            ),
                                            dcc.Dropdown(
                                                id="reopen-event-selector",
                                                options=reopen_dropdown_options,
                                                placeholder="Search archived events...",
                                                searchable=True,
                                                clearable=True,
                                                style={
                                                    "marginBottom": "0.75rem",
                                                    "backgroundColor": COLORS["bg_card"],
                                                },
                                                className="dash-dropdown-dark",
                                            ),
                                            dcc.Checklist(
                                                id="reopen-restore-active-toggle",
                                                options=[
                                                    {
                                                        "label": " Restore active check-ins from archive",
                                                        "value": "restore",
                                                    }
                                                ],
                                                value=["restore"],
                                                style={
                                                    "marginBottom": "1rem",
                                                    "color": COLORS["text_secondary"],
                                                },
                                                inputStyle={"marginRight": "0.5rem"},
                                            ),
                                            html.Button(
                                                "Reopen Selected Event",
                                                id="btn-reopen-event",
                                                n_clicks=0,
                                                style=STYLES["button_secondary"],
                                            ),
                                            html.Div(
                                                id="reopen-feedback", style={"marginTop": "1rem"}
                                            ),
                                        ],
                                    ),
                                    # Hidden elements to satisfy callback dependencies
                                    html.Div(
                                        style={"display": "none"},
                                        children=[
                                            dcc.Dropdown(
                                                id="game-dropdown",
                                                className="fgc-dropdown",
                                                options=[],
                                                value=None,
                                            ),
                                            html.Div(id="game-help"),
                                        ],
                                    ),
                                    # Column visibility settings
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.H3("Table Columns", style=STYLES["section_title"]),
                                            html.P(
                                                "Choose which columns to display in the check-ins table.",
                                                style={
                                                    "color": COLORS["text_secondary"],
                                                    "marginBottom": "1rem",
                                                },
                                            ),
                                            dcc.Dropdown(
                                                id="column-visibility-dropdown",
                                                className="fgc-dropdown",
                                                options=[
                                                    opt
                                                    for opt in [
                                                        {"label": "Name", "value": "name"},
                                                        {"label": "Tag", "value": "tag"},
                                                        {"label": "Status", "value": "status"},
                                                        (
                                                            {
                                                                "label": "Payment",
                                                                "value": "payment_valid",
                                                            }
                                                            if settings.get("require_payment")
                                                            is True
                                                            else None
                                                        ),
                                                        {"label": "Phone", "value": "telephone"},
                                                        (
                                                            {"label": "Member", "value": "member"}
                                                            if settings.get("require_membership")
                                                            is True
                                                            else None
                                                        ),
                                                        {"label": "Start.gg", "value": "startgg"},
                                                        {"label": "Guest", "value": "is_guest"},
                                                        {
                                                            "label": "Games",
                                                            "value": "tournament_games_registered",
                                                        },
                                                        {"label": "Email", "value": "email"},
                                                        {"label": "UUID", "value": "UUID"},
                                                        {"label": "Created", "value": "created"},
                                                    ]
                                                    if opt is not None
                                                ],
                                                value=[
                                                    c
                                                    for c in [
                                                        "name",
                                                        "tag",
                                                        "status",
                                                        "payment_valid",
                                                        "telephone",
                                                        "member",
                                                        "startgg",
                                                        "is_guest",
                                                        "tournament_games_registered",
                                                    ]
                                                    if not (
                                                        c == "payment_valid"
                                                        and settings.get("require_payment")
                                                        is not True
                                                    )
                                                    and not (
                                                        c == "member"
                                                        and settings.get("require_membership")
                                                        is not True
                                                    )
                                                ],
                                                multi=True,
                                                clearable=False,
                                                style={"backgroundColor": COLORS["bg_dark"]},
                                            ),
                                        ],
                                    ),
                                    # Advanced (collapsible)
                                    html.Div(
                                        style=STYLES["card"],
                                        children=[
                                            html.Details(
                                                open=False,
                                                children=[
                                                    html.Summary(
                                                        "Advanced",
                                                        style={
                                                            "cursor": "pointer",
                                                            "fontWeight": "700",
                                                            "color": COLORS["text_primary"],
                                                            "fontSize": "1rem",
                                                        },
                                                    ),
                                                    html.Div(
                                                        style={"marginTop": "1rem"},
                                                        children=[
                                                            html.Div(
                                                                id="dev-tools-advanced-container",
                                                                style={"display": "none", "marginBottom": "1.1rem"},
                                                                children=[
                                                                    html.Details(
                                                                        open=False,
                                                                        children=[
                                                                            html.Summary(
                                                                                "Dev",
                                                                                style={
                                                                                    "cursor": "pointer",
                                                                                    "fontWeight": "700",
                                                                                    "color": COLORS[
                                                                                        "text_primary"
                                                                                    ],
                                                                                    "fontSize": "0.95rem",
                                                                                },
                                                                            ),
                                                                            html.Div(
                                                                                style={
                                                                                    "marginTop": "0.8rem",
                                                                                    "padding": "0.9rem",
                                                                                    "borderRadius": "8px",
                                                                                    "border": f"1px dashed {COLORS['border']}",
                                                                                    "backgroundColor": "rgba(30, 41, 59, 0.28)",
                                                                                },
                                                                                children=[
                                                                                    html.P(
                                                                                        "Owner-only developer tools for local testing.",
                                                                                        style={
                                                                                            "margin": "0 0 0.6rem 0",
                                                                                            "fontSize": "0.8rem",
                                                                                            "color": COLORS[
                                                                                                "text_muted"
                                                                                            ],
                                                                                        },
                                                                                    ),
                                                                                    dcc.Checklist(
                                                                                        id="dev-tools-visible-toggle",
                                                                                        options=[
                                                                                            {
                                                                                                "label": "Show Dev Tools",
                                                                                                "value": "show",
                                                                                            }
                                                                                        ],
                                                                                        value=[],
                                                                                        persistence=True,
                                                                                        persistence_type="local",
                                                                                        style={
                                                                                            "marginBottom": "0.6rem",
                                                                                            "color": COLORS[
                                                                                                "text_primary"
                                                                                            ],
                                                                                        },
                                                                                    ),
                                                                                    html.Div(
                                                                                        id="dev-tools-panel",
                                                                                        style={"display": "none"},
                                                                                        children=[
                                                                                            html.Div(
                                                                                                style={
                                                                                                    "display": "grid",
                                                                                                    "gridTemplateColumns": "repeat(auto-fit, minmax(220px, 1fr))",
                                                                                                    "gap": "0.6rem",
                                                                                                },
                                                                                                children=[
                                                                                                    dcc.Input(
                                                                                                        id="input-mock-event-slug",
                                                                                                        type="text",
                                                                                                        value="mock-acq-source",
                                                                                                        placeholder="mock-event-slug",
                                                                                                        style=STYLES[
                                                                                                            "input"
                                                                                                        ],
                                                                                                    ),
                                                                                                    dcc.Input(
                                                                                                        id="input-mock-event-name",
                                                                                                        type="text",
                                                                                                        value="Mock Acquisition Source Test",
                                                                                                        placeholder="Mock event name",
                                                                                                        style=STYLES[
                                                                                                            "input"
                                                                                                        ],
                                                                                                    ),
                                                                                                ],
                                                                                            ),
                                                                                            html.Button(
                                                                                                "Use Mock Event",
                                                                                                id="btn-use-mock-event",
                                                                                                n_clicks=0,
                                                                                                style={
                                                                                                    **STYLES[
                                                                                                        "button_secondary"
                                                                                                    ],
                                                                                                    "marginTop": "0.75rem",
                                                                                                },
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ],
                                                                            ),
                                                                        ],
                                                                    ),
                                                                ],
                                                            ),
                                                            html.Div(
                                                                id="recompute-dev-container",
                                                                style={"display": "none"},
                                                                children=[
                                                                    html.H3(
                                                                        "Recompute Event Stats",
                                                                        style={
                                                                            **STYLES["section_title"],
                                                                            "color": COLORS["accent_blue"],
                                                                        },
                                                                    ),
                                                                    html.P(
                                                                        "Rebuild KPI stats for selected event from archived rows (safe repair).",
                                                                        style={
                                                                            "color": COLORS["text_secondary"],
                                                                            "marginBottom": "0.75rem",
                                                                        },
                                                                    ),
                                                                    html.Div(
                                                                        style={
                                                                            "display": "flex",
                                                                            "gap": "0.65rem",
                                                                            "flexWrap": "wrap",
                                                                        },
                                                                        children=[
                                                                            html.Button(
                                                                                "Recompute Selected Event",
                                                                                id="btn-recompute-event-stats",
                                                                                n_clicks=0,
                                                                                style=STYLES["button_secondary"],
                                                                            ),
                                                                        ],
                                                                    ),
                                                                    html.Div(
                                                                        id="recompute-event-feedback",
                                                                        style={"marginTop": "0.75rem"},
                                                                    ),
                                                                    html.Hr(
                                                                        style={
                                                                            "border": "none",
                                                                            "borderTop": f"1px solid {COLORS['border']}",
                                                                            "margin": "1.1rem 0",
                                                                        }
                                                                    ),
                                                                ],
                                                            ),
                                                            html.H3(
                                                                "Data Integrity Scan",
                                                                style={
                                                                    **STYLES["section_title"],
                                                                    "color": COLORS["accent_green"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Scan archived events for KPI mismatches (funnel/no-show/payment consistency).",
                                                                style={
                                                                    "color": COLORS["text_secondary"],
                                                                    "marginBottom": "0.75rem",
                                                                },
                                                            ),
                                                            html.Button(
                                                                "Scan Archived Events",
                                                                id="btn-scan-event-integrity",
                                                                n_clicks=0,
                                                                style=STYLES["button_secondary"],
                                                            ),
                                                            html.Div(
                                                                id="scan-integrity-feedback",
                                                                style={"marginTop": "0.75rem"},
                                                            ),
                                                            dash_table.DataTable(
                                                                id="integrity-scan-table",
                                                                columns=[
                                                                    {"name": "Event", "id": "event_slug"},
                                                                    {"name": "Warnings", "id": "warnings_count"},
                                                                    {"name": "Details", "id": "warnings_text"},
                                                                    {"name": "Archived At", "id": "archived_at"},
                                                                ],
                                                                data=[],
                                                                page_size=8,
                                                                sort_action="native",
                                                                style_table={
                                                                    "overflowX": "auto",
                                                                    "marginTop": "0.65rem",
                                                                },
                                                                style_header={
                                                                    "backgroundColor": COLORS["bg_dark"],
                                                                    "color": COLORS["text_primary"],
                                                                    "fontWeight": "600",
                                                                    "fontSize": "0.73rem",
                                                                    "textTransform": "uppercase",
                                                                    "letterSpacing": "0.04em",
                                                                    "padding": "0.7rem",
                                                                    "borderBottom": f"2px solid {COLORS['accent_green']}",
                                                                },
                                                                style_cell={
                                                                    "backgroundColor": COLORS["bg_card"],
                                                                    "color": COLORS["text_primary"],
                                                                    "border": "none",
                                                                    "borderBottom": f"1px solid {COLORS['border']}",
                                                                    "padding": "0.55rem 0.7rem",
                                                                    "fontSize": "0.79rem",
                                                                    "textAlign": "left",
                                                                    "maxWidth": "420px",
                                                                    "whiteSpace": "normal",
                                                                    "height": "auto",
                                                                },
                                                                style_data_conditional=[
                                                                    {
                                                                        "if": {"row_index": "odd"},
                                                                        "backgroundColor": COLORS["bg_dark"],
                                                                    }
                                                                ],
                                                            ),
                                                            html.Hr(
                                                                style={
                                                                    "border": "none",
                                                                    "borderTop": f"1px solid {COLORS['border']}",
                                                                    "margin": "1.1rem 0",
                                                                }
                                                            ),
                                                            html.H3(
                                                                "Delete Archived Event",
                                                                style={
                                                                    **STYLES["section_title"],
                                                                    "color": COLORS["accent_red"],
                                                                },
                                                            ),
                                                            html.P(
                                                                "Permanently delete this event from history (event_archive + event_stats)."
                                                                " Active check-ins are not touched.",
                                                                style={
                                                                    "color": COLORS[
                                                                        "text_secondary"
                                                                    ],
                                                                    "marginBottom": "0.75rem",
                                                                },
                                                            ),
                                                            dcc.Dropdown(
                                                                id="delete-archive-event-dropdown",
                                                                className="fgc-dropdown",
                                                                options=[
                                                                    {
                                                                        "label": s.replace(
                                                                            "-", " "
                                                                        ).title(),
                                                                        "value": s,
                                                                    }
                                                                    for s in archived_slugs
                                                                ],
                                                                placeholder="Select archived event to delete",
                                                                value=(
                                                                    archived_slugs[0]
                                                                    if archived_slugs
                                                                    else None
                                                                ),
                                                                clearable=False,
                                                                style={"marginBottom": "0.75rem"},
                                                            ),
                                                            dcc.Textarea(
                                                                id="input-delete-event-reason",
                                                                placeholder="Reason for deletion (required, shown in audit log)",
                                                                style={
                                                                    "width": "100%",
                                                                    "minHeight": "72px",
                                                                    "padding": "0.6rem",
                                                                    "borderRadius": "8px",
                                                                    "border": f"1px solid {COLORS['border']}",
                                                                    "backgroundColor": COLORS[
                                                                        "bg_dark"
                                                                    ],
                                                                    "color": COLORS["text_primary"],
                                                                },
                                                            ),
                                                            html.Div(
                                                                style={"marginTop": "0.75rem"},
                                                                children=[
                                                                    html.Button(
                                                                        "Delete Archived Event",
                                                                        id="btn-delete-event-history",
                                                                        n_clicks=0,
                                                                        style={
                                                                            "backgroundColor": COLORS[
                                                                                "accent_red"
                                                                            ],
                                                                            "color": "#fff",
                                                                            "border": "none",
                                                                            "borderRadius": "8px",
                                                                            "padding": "0.6rem 1rem",
                                                                            "fontSize": "0.875rem",
                                                                            "fontWeight": "600",
                                                                            "cursor": "pointer",
                                                                        },
                                                                    ),
                                                                ],
                                                            ),
                                                            html.Div(
                                                                id="delete-event-feedback",
                                                                style={"marginTop": "0.75rem"},
                                                            ),
                                                            dcc.ConfirmDialog(
                                                                id="confirm-delete-event-dialog",
                                                                message="Are you sure you want to permanently delete this event from history?",
                                                            ),
                                                            html.Hr(
                                                                style={
                                                                    "border": "none",
                                                                    "borderTop": f"1px solid {COLORS['border']}",
                                                                    "margin": "1.1rem 0",
                                                                }
                                                            ),
                                                            html.Details(
                                                                open=False,
                                                                children=[
                                                                    html.Summary(
                                                                        "Audit Log",
                                                                        style={
                                                                            "cursor": "pointer",
                                                                            "fontWeight": "700",
                                                                            "color": COLORS[
                                                                                "text_primary"
                                                                            ],
                                                                            "fontSize": "0.95rem",
                                                                        },
                                                                    ),
                                                                    html.Div(
                                                                        style={
                                                                            "marginTop": "0.9rem"
                                                                        },
                                                                        children=[
                                                                            html.P(
                                                                                "Track all administrative actions performed in the system.",
                                                                                style={
                                                                                    "color": COLORS[
                                                                                        "text_secondary"
                                                                                    ],
                                                                                    "marginBottom": "0.85rem",
                                                                                    "fontSize": "0.86rem",
                                                                                    "lineHeight": "1.45",
                                                                                },
                                                                            ),
                                                                            html.Div(
                                                                                style={
                                                                                    "display": "flex",
                                                                                    "gap": "0.85rem",
                                                                                    "marginBottom": "1.1rem",
                                                                                    "flexWrap": "wrap",
                                                                                    "alignItems": "flex-end",
                                                                                },
                                                                                children=[
                                                                                    html.Div(
                                                                                        style={
                                                                                            "minWidth": "180px",
                                                                                            "flex": "1",
                                                                                        },
                                                                                        children=[
                                                                                            html.Label(
                                                                                                "Action",
                                                                                                style={
                                                                                                    "fontSize": "0.72rem",
                                                                                                    "color": COLORS[
                                                                                                        "text_secondary"
                                                                                                    ],
                                                                                                    "marginBottom": "0.45rem",
                                                                                                    "letterSpacing": "0.04em",
                                                                                                    "textTransform": "uppercase",
                                                                                                    "display": "block",
                                                                                                },
                                                                                            ),
                                                                                            dcc.Dropdown(
                                                                                                id="audit-filter-action",
                                                                                                className="fgc-dropdown",
                                                                                                options=[],
                                                                                                value=None,
                                                                                                placeholder="All actions",
                                                                                                clearable=True,
                                                                                                style={
                                                                                                    "backgroundColor": COLORS[
                                                                                                        "bg_dark"
                                                                                                    ]
                                                                                                },
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                    html.Div(
                                                                                        style={
                                                                                            "minWidth": "180px",
                                                                                            "flex": "1",
                                                                                        },
                                                                                        children=[
                                                                                            html.Label(
                                                                                                "User",
                                                                                                style={
                                                                                                    "fontSize": "0.72rem",
                                                                                                    "color": COLORS[
                                                                                                        "text_secondary"
                                                                                                    ],
                                                                                                    "marginBottom": "0.45rem",
                                                                                                    "letterSpacing": "0.04em",
                                                                                                    "textTransform": "uppercase",
                                                                                                    "display": "block",
                                                                                                },
                                                                                            ),
                                                                                            dcc.Dropdown(
                                                                                                id="audit-filter-user",
                                                                                                className="fgc-dropdown",
                                                                                                options=[],
                                                                                                value=None,
                                                                                                placeholder="All users",
                                                                                                clearable=True,
                                                                                                style={
                                                                                                    "backgroundColor": COLORS[
                                                                                                        "bg_dark"
                                                                                                    ]
                                                                                                },
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                    html.Button(
                                                                                        "Refresh",
                                                                                        id="btn-audit-refresh",
                                                                                        n_clicks=0,
                                                                                        style={
                                                                                            **STYLES[
                                                                                                "button_secondary"
                                                                                            ],
                                                                                            "height": "40px",
                                                                                            "padding": "0 1rem",
                                                                                        },
                                                                                    ),
                                                                                ],
                                                                            ),
                                                                            dcc.Loading(
                                                                                type="circle",
                                                                                color=COLORS[
                                                                                    "accent_blue"
                                                                                ],
                                                                                children=dash_table.DataTable(
                                                                                    id="audit-log-table",
                                                                                    columns=[
                                                                                        {
                                                                                            "name": "Time",
                                                                                            "id": "timestamp",
                                                                                        },
                                                                                        {
                                                                                            "name": "User",
                                                                                            "id": "user_name",
                                                                                        },
                                                                                        {
                                                                                            "name": "Category",
                                                                                            "id": "action_category",
                                                                                        },
                                                                                        {
                                                                                            "name": "Action",
                                                                                            "id": "action",
                                                                                        },
                                                                                        {
                                                                                            "name": "Table",
                                                                                            "id": "target_table",
                                                                                        },
                                                                                        {
                                                                                            "name": "Event",
                                                                                            "id": "target_event",
                                                                                        },
                                                                                        {
                                                                                            "name": "Player",
                                                                                            "id": "target_player",
                                                                                        },
                                                                                        {
                                                                                            "name": "Reason",
                                                                                            "id": "reason",
                                                                                        },
                                                                                    ],
                                                                                    data=[],
                                                                                    page_size=25,
                                                                                    sort_action="native",
                                                                                    style_table={
                                                                                        "overflowX": "auto",
                                                                                        "border": f"1px solid {COLORS['border']}",
                                                                                        "borderRadius": "10px",
                                                                                    },
                                                                                    style_header={
                                                                                        "backgroundColor": COLORS[
                                                                                            "bg_dark"
                                                                                        ],
                                                                                        "color": COLORS[
                                                                                            "text_primary"
                                                                                        ],
                                                                                        "fontWeight": "600",
                                                                                        "fontSize": "0.72rem",
                                                                                        "textTransform": "uppercase",
                                                                                        "letterSpacing": "0.05em",
                                                                                        "padding": "0.85rem 0.95rem",
                                                                                        "borderBottom": f"2px solid {COLORS['accent_purple']}",
                                                                                    },
                                                                                    style_cell={
                                                                                        "backgroundColor": COLORS[
                                                                                            "bg_card"
                                                                                        ],
                                                                                        "color": COLORS[
                                                                                            "text_primary"
                                                                                        ],
                                                                                        "border": "none",
                                                                                        "borderBottom": f"1px solid {COLORS['border']}",
                                                                                        "padding": "0.62rem 0.9rem",
                                                                                        "fontSize": "0.8rem",
                                                                                        "lineHeight": "1.35",
                                                                                        "textAlign": "left",
                                                                                        "maxWidth": "200px",
                                                                                        "overflow": "hidden",
                                                                                        "textOverflow": "ellipsis",
                                                                                    },
                                                                                    style_cell_conditional=[
                                                                                        {
                                                                                            "if": {
                                                                                                "column_id": "action_category"
                                                                                            },
                                                                                            "width": "120px",
                                                                                            "minWidth": "120px",
                                                                                            "maxWidth": "120px",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "column_id": "action"
                                                                                            },
                                                                                            "width": "260px",
                                                                                            "minWidth": "220px",
                                                                                            "maxWidth": "320px",
                                                                                        },
                                                                                    ],
                                                                                    style_data_conditional=[
                                                                                        {
                                                                                            "if": {
                                                                                                "row_index": "odd"
                                                                                            },
                                                                                            "backgroundColor": COLORS[
                                                                                                "bg_dark"
                                                                                            ],
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Auth'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": "#93c5fd",
                                                                                            "fontWeight": "700",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Settings'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": "#86efac",
                                                                                            "fontWeight": "700",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Check-ins'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": "#fcd34d",
                                                                                            "fontWeight": "700",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Archive'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": "#f9a8d4",
                                                                                            "fontWeight": "700",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Integrations'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": "#67e8f9",
                                                                                            "fontWeight": "700",
                                                                                        },
                                                                                        {
                                                                                            "if": {
                                                                                                "filter_query": "{action_category} = 'Other'",
                                                                                                "column_id": "action_category",
                                                                                            },
                                                                                            "color": COLORS[
                                                                                                "text_secondary"
                                                                                            ],
                                                                                            "fontWeight": "600",
                                                                                        },
                                                                                    ],
                                                                                    tooltip_data=[],
                                                                                    tooltip_duration=None,
                                                                                ),
                                                                            ),
                                                                            html.Div(
                                                                                id="audit-log-count",
                                                                                style={
                                                                                    "color": COLORS[
                                                                                        "text_muted"
                                                                                    ],
                                                                                    "fontSize": "0.72rem",
                                                                                    "marginTop": "0.75rem",
                                                                                    "letterSpacing": "0.03em",
                                                                                },
                                                                                children="0 entries",
                                                                            ),
                                                                        ],
                                                                    ),
                                                                ],
                                                            ),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            # Footer
            html.Footer(
                style={
                    "textAlign": "center",
                    "padding": "2rem",
                    "color": COLORS["text_muted"],
                    "fontSize": "0.75rem",
                    "borderTop": f"1px solid {COLORS['border']}",
                    "marginTop": "2rem",
                },
                children=[
                    html.P(
                        [
                            "Powered by ",
                            html.Strong("IMLO"),
                        ],
                        style={"margin": "0"},
                    ),
                ],
            ),
        ],
    )

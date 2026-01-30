# layout.py
"""
FGC Dashboard Layout - Modern Esports Theme
"""
import os
from dash import html, dcc, dash_table
from shared.airtable_api import get_all_event_slugs, get_active_slug, get_checkins, get_active_settings
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# SSE token for authenticated real-time updates
SSE_TOKEN = os.getenv("SSE_TOKEN", "")

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


def create_layout():
    """
    Build and return the layout for the FGC Check-in Dashboard.
    Modern esports-themed design with live stats and status tracking.
    """
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
        columns = [{"name": str(col).replace("_", " ").title(), "id": str(col)} for col in df.columns]
        data = df.to_dict("records")

    # Format event name for display - use event_display_name if available (has proper åäö)
    event_display_name = settings.get("event_display_name", "")
    event_display = event_display_name if event_display_name else (
        active_slug.replace("-", " ").title() if active_slug else "No Event Selected"
    )

    # Build visible columns based on active requirements
    default_columns = ["name", "tag", "status", "telephone", "startgg", "is_guest", "tournament_games_registered"]
    if settings.get("require_payment") is True:
        default_columns.insert(3, "payment_valid")
    if settings.get("require_membership") is True:
        default_columns.insert(4 if "payment_valid" in default_columns else 3, "member")

    return html.Div(style=STYLES["page"], children=[
        # Data stores
        dcc.Store(id="sse-token-store", data=SSE_TOKEN),  # Token for SSE auth
        dcc.Store(id="events-map-store", data=[]),
        dcc.Store(id="visible-columns-store", data=default_columns),
        dcc.Store(id="active-filter", data="all"),  # Current filter: all, pending, ready, no-payment
        dcc.Store(id="sse-trigger", data=0),  # Incremented by SSE events to trigger refresh
        dcc.Store(id="sse-status", data="disconnected"),  # SSE connection status
        dcc.Store(id="requirements-store", data={
            # Airtable checkbox: checked = True, unchecked = field missing (None)
            # Use "is True" so that unchecked (None) = requirement OFF
            "require_payment": settings.get("require_payment") is True,
            "require_membership": settings.get("require_membership") is True,
            "require_startgg": settings.get("require_startgg") is True,
        }),
        dcc.Interval(id="interval-refresh", interval=300 * 1000, n_intervals=0, disabled=True),  # Fallback: 5 min, disabled by default

        # Header
        html.Header(style={**STYLES["header"], "position": "relative"}, children=[
            # Centered logo
            html.Div(style={"display": "flex", "justifyContent": "center"}, children=[
                html.Img(src="/assets/logo.png", style={"height": "60px", "width": "auto"}),
            ]),
            # Connection status indicator (top right)
            html.Div(id="connection-indicator", style={"position": "absolute", "top": "1.5rem", "right": "2rem", "display": "flex", "alignItems": "center", "gap": "0.5rem"}, children=[
                html.Div(id="connection-dot", style={
                    "width": "10px",
                    "height": "10px",
                    "backgroundColor": COLORS["accent_yellow"],  # Yellow = connecting
                    "borderRadius": "50%",
                    "boxShadow": f"0 0 10px {COLORS['accent_yellow']}",
                    "transition": "all 0.3s ease",
                }),
                html.Span("Connecting...", id="connection-text", style={
                    "color": COLORS["accent_yellow"],
                    "fontSize": "0.75rem",
                    "fontWeight": "600",
                    "letterSpacing": "0.1em",
                    "transition": "all 0.3s ease",
                }),
            ]),
        ]),

        # SSE JavaScript loaded from assets/sse-client.js automatically by Dash

        # Main content
        html.Div(style=STYLES["container"], children=[

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
                        style={"backgroundColor": COLORS["bg_card"], "color": COLORS["text_secondary"], "border": "none", "padding": "1rem 1.5rem"},
                        selected_style={"backgroundColor": COLORS["bg_dark"], "color": COLORS["accent_blue"], "borderTop": f"2px solid {COLORS['accent_blue']}", "padding": "1rem 1.5rem"},
                    ),
                    dcc.Tab(
                        label="⚙️ Settings",
                        value="tab-settings",
                        style={"backgroundColor": COLORS["bg_card"], "color": COLORS["text_secondary"], "border": "none", "padding": "1rem 1.5rem"},
                        selected_style={"backgroundColor": COLORS["bg_dark"], "color": COLORS["accent_blue"], "borderTop": f"2px solid {COLORS['accent_blue']}", "padding": "1rem 1.5rem"},
                    ),
                ],
            ),

            # Tab content container
            html.Div(id="tabs-content", children=[

                # ========== TAB 1: Live Check-ins ==========
                html.Div(id="tab-checkins-content", children=[

                    # Event selector row
                    html.Div(style={"display": "flex", "gap": "1rem", "marginBottom": "1.5rem", "flexWrap": "wrap", "alignItems": "flex-end"}, children=[
                        html.Div(style={"flex": "1", "minWidth": "250px"}, children=[
                            html.Label("Current Event", style={"fontSize": "0.75rem", "color": COLORS["text_secondary"], "marginBottom": "0.5rem", "display": "block"}),
                            dcc.Dropdown(
                                id="event-dropdown",
                                options=[
                                    {"label": "🔍 All Events (Debug)", "value": "__ALL__"},
                                ] + [
                                    # Use event_display_name for active slug (has proper åäö), fallback to slug title
                                    {"label": event_display_name if s == active_slug and event_display_name else s.replace("-", " ").title(), "value": s}
                                    for s in event_slugs
                                ],
                                value=active_slug if active_slug in event_slugs else (event_slugs[0] if event_slugs else None),
                                clearable=False,
                                style={"backgroundColor": COLORS["bg_dark"]},
                            ),
                        ]),
                        html.Button("Refresh", id="btn-refresh", n_clicks=0, style={**STYLES["button_secondary"], "height": "38px"}),
                    ]),

                    # Active requirements indicator
                    html.Div(id="requirements-indicator", style={
                        "display": "flex",
                        "alignItems": "center",
                        "gap": "0.75rem",
                        "marginBottom": "1rem",
                        "padding": "0.5rem 0.75rem",
                        "backgroundColor": COLORS["bg_card"],
                        "borderRadius": "8px",
                        "border": f"1px solid {COLORS['border']}",
                        "fontSize": "0.75rem",
                    }, children=[
                        html.Span("Active Requirements:", style={"color": COLORS["text_muted"], "fontWeight": "500"}),
                        html.Div(id="requirement-badges", style={"display": "flex", "gap": "0.5rem", "flexWrap": "wrap"}),
                    ]),

                    # Stats cards (clickable as filters)
                    html.Div(id="stats-cards", style={"display": "flex", "gap": "1rem", "marginBottom": "1.5rem", "flexWrap": "wrap"}, children=[
                        html.Div(id="filter-all", n_clicks=0, style={
                            **STYLES["stat_card"],
                            "borderTop": f"3px solid {COLORS['accent_blue']}",
                            "cursor": "pointer",
                            "transition": "all 0.2s",
                        }, children=[
                            html.P(str(len(data)), id="stat-total", style={**STYLES["stat_value"], "color": COLORS["accent_blue"]}),
                            html.P("Total", style=STYLES["stat_label"]),
                        ]),
                        html.Div(id="filter-ready", n_clicks=0, style={
                            **STYLES["stat_card"],
                            "borderTop": f"3px solid {COLORS['accent_green']}",
                            "cursor": "pointer",
                            "transition": "all 0.2s",
                        }, children=[
                            html.P(str(len([d for d in data if d.get("status") == "Ready"])), id="stat-ready", style={**STYLES["stat_value"], "color": COLORS["accent_green"]}),
                            html.P("Ready", style=STYLES["stat_label"]),
                        ]),
                        html.Div(id="filter-pending", n_clicks=0, style={
                            **STYLES["stat_card"],
                            "borderTop": f"3px solid {COLORS['accent_yellow']}",
                            "cursor": "pointer",
                            "transition": "all 0.2s",
                        }, children=[
                            html.P(str(len([d for d in data if d.get("status") == "Pending"])), id="stat-pending", style={**STYLES["stat_value"], "color": COLORS["accent_yellow"]}),
                            html.P("Pending", style=STYLES["stat_label"]),
                        ]),
                        html.Div(id="filter-no-payment", n_clicks=0, style={
                            **STYLES["stat_card"],
                            "borderTop": f"3px solid {COLORS['accent_red']}",
                            "cursor": "pointer",
                            "transition": "all 0.2s",
                        }, children=[
                            html.P("0", id="stat-attention", style={**STYLES["stat_value"], "color": COLORS["accent_red"]}),
                            html.P("No Payment", style=STYLES["stat_label"]),
                        ]),
                    ]),

                    # Needs attention section (collapsible)
                    html.Div(id="needs-attention-section", style={
                        **STYLES["card"],
                        "borderLeft": f"4px solid {COLORS['accent_red']}",
                        "display": "none",  # Hidden by default, shown by callback when needed
                    }, children=[
                        # Header with toggle
                        html.Div(style={
                            "display": "flex",
                            "justifyContent": "space-between",
                            "alignItems": "center",
                            "cursor": "pointer",
                        }, id="needs-attention-header", n_clicks=0, children=[
                            html.H3("🚨 Needs Attention", style={**STYLES["section_title"], "color": COLORS["accent_red"], "margin": "0"}),
                            html.Div(style={"display": "flex", "alignItems": "center", "gap": "0.5rem"}, children=[
                                html.Span(id="needs-attention-count", style={
                                    "backgroundColor": COLORS["accent_red"],
                                    "color": "#fff",
                                    "padding": "0.25rem 0.6rem",
                                    "borderRadius": "12px",
                                    "fontSize": "0.8rem",
                                    "fontWeight": "600",
                                }),
                                html.Span("▼", id="needs-attention-chevron", style={
                                    "color": COLORS["text_muted"],
                                    "fontSize": "0.8rem",
                                    "transition": "transform 0.2s",
                                }),
                            ]),
                        ]),
                        # Collapsible content
                        html.Div(id="needs-attention-list", children=[
                            html.P("No issues", style={"color": COLORS["text_muted"], "margin": "0"})
                        ], style={"marginTop": "1rem"})
                    ]),

                    # Main checkins table
                    html.Div(style=STYLES["card"], children=[
                        # Header row with title and player count
                        html.Div(style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "1rem"}, children=[
                            html.H3("Player List", style={**STYLES["section_title"], "margin": "0"}),
                            html.Span(id="player-count", children=f"{len(data)} players", style={"color": COLORS["text_muted"], "fontSize": "0.875rem"}),
                        ]),

                        # Search and game filter row
                        html.Div(style={"display": "flex", "gap": "1rem", "marginBottom": "1rem", "flexWrap": "wrap", "alignItems": "center"}, children=[
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
                            html.Div(style={"minWidth": "140px"}, children=[
                                dcc.Dropdown(
                                    id="game-filter",
                                    options=[],  # Populated dynamically
                                    value=None,
                                    placeholder="All games",
                                    clearable=True,
                                    style={"backgroundColor": COLORS["bg_dark"], "minWidth": "140px"},
                                ),
                            ]),
                        ]),
                        dcc.Loading(
                            type="circle",
                            color=COLORS["accent_blue"],
                            children=dash_table.DataTable(
                                id="checkins-table",
                                columns=columns,
                                data=data,
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
                                        "if": {"filter_query": "{status} = 'Ready'"},
                                        "backgroundColor": "rgba(16, 185, 129, 0.1)",
                                        "borderLeft": f"3px solid {COLORS['accent_green']}",
                                    },
                                    # Pending status - yellow row highlight
                                    {
                                        "if": {"filter_query": "{status} = 'Pending'"},
                                        "backgroundColor": "rgba(245, 158, 11, 0.1)",
                                        "borderLeft": f"3px solid {COLORS['accent_yellow']}",
                                    },
                                    # Icon cells: ✓ = green (clickable columns have cursor pointer)
                                    {"if": {"filter_query": '{member} = "✓"', "column_id": "member"}, "color": COLORS["accent_green"], "fontWeight": "600"},
                                    {"if": {"filter_query": '{startgg} = "✓"', "column_id": "startgg"}, "color": COLORS["accent_green"], "fontWeight": "600", "cursor": "pointer", "textDecoration": "underline"},
                                    {"if": {"filter_query": '{is_guest} = "✓"', "column_id": "is_guest"}, "color": COLORS["accent_blue"], "fontWeight": "600"},
                                    {"if": {"filter_query": '{payment_valid} = "✓"', "column_id": "payment_valid"}, "color": COLORS["accent_green"], "fontWeight": "600", "cursor": "pointer", "textDecoration": "underline"},
                                    # Icon cells: ✗ = red (clickable columns have cursor pointer)
                                    {"if": {"filter_query": '{member} = "✗"', "column_id": "member"}, "color": COLORS["accent_red"], "fontWeight": "600"},
                                    {"if": {"filter_query": '{startgg} = "✗"', "column_id": "startgg"}, "color": COLORS["accent_red"], "fontWeight": "600", "cursor": "pointer", "textDecoration": "underline"},
                                    {"if": {"filter_query": '{is_guest} = "✗"', "column_id": "is_guest"}, "color": COLORS["text_muted"], "fontWeight": "600"},
                                    {"if": {"filter_query": '{payment_valid} = "✗"', "column_id": "payment_valid"}, "color": COLORS["accent_red"], "fontWeight": "600", "cursor": "pointer", "textDecoration": "underline"},
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
                    ]),

                    # Action buttons row
                    html.Div(style={"display": "flex", "gap": "1rem", "marginTop": "1rem", "alignItems": "center", "flexWrap": "wrap"}, children=[
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
                            }
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
                            }
                        ),
                        html.Span("Click row to select • Click Payment/Start.gg/Guest to toggle", style={"color": COLORS["text_muted"], "fontSize": "0.75rem"}),
                    ]),

                    # Feedback messages
                    html.Div(id="payment-update-feedback", style={"marginTop": "0.5rem"}),
                    html.Div(id="delete-feedback", style={"marginTop": "0.5rem"}),
                    html.Div(id="export-feedback", style={"marginTop": "0.5rem"}),

                    # Download component for CSV export
                    dcc.Download(id="download-guests-csv"),

                    # Confirmation dialog for delete
                    dcc.ConfirmDialog(
                        id="confirm-delete-dialog",
                        message="Are you sure you want to delete this player?",
                    ),
                ]),

                # ========== TAB 2: Settings ==========
                html.Div(id="tab-settings-content", style={"display": "none"}, children=[
                    html.Div(style=STYLES["card"], children=[
                        html.H3("Event Configuration", style=STYLES["section_title"]),

                        html.Div(style={"marginBottom": "1.5rem"}, children=[
                            html.Label("Start.gg Tournament Link", style={"fontSize": "0.875rem", "color": COLORS["text_secondary"], "marginBottom": "0.5rem", "display": "block"}),
                            dcc.Input(
                                id="input-startgg-link",
                                type="text",
                                placeholder="https://www.start.gg/tournament/your-tournament",
                                style=STYLES["input"],
                            ),
                        ]),

                        html.Button("Fetch Event Data", id="btn-fetch-event", n_clicks=0, style=STYLES["button_primary"]),
                        html.Div(id="settings-output", style={"marginTop": "1rem", "padding": "1rem", "borderRadius": "8px", "backgroundColor": COLORS["bg_dark"]}),
                    ]),

                    # Check-in Requirements
                    html.Div(style=STYLES["card"], children=[
                        html.H3("Check-in Requirements", style=STYLES["section_title"]),
                        html.P("Choose which requirements players must meet to be marked as 'Ready'.", style={"color": COLORS["text_secondary"], "marginBottom": "1rem"}),

                        # Require Payment
                        html.Div(style={"marginBottom": "1rem", "display": "flex", "alignItems": "center", "gap": "0.75rem"}, children=[
                            dcc.Checklist(
                                id="require-payment-toggle",
                                options=[{"label": "", "value": True}],
                                value=[True] if settings.get("require_payment") is True else [],
                                style={"display": "inline-block"},
                                inputStyle={"width": "18px", "height": "18px", "cursor": "pointer"},
                            ),
                            html.Div(children=[
                                html.Span("Require Payment", style={"fontWeight": "600", "color": COLORS["text_primary"]}),
                                html.P("Player must pay to be marked as Ready", style={"margin": "0", "fontSize": "0.75rem", "color": COLORS["text_muted"]}),
                            ]),
                        ]),

                        # Require Membership
                        html.Div(style={"marginBottom": "1rem", "display": "flex", "alignItems": "center", "gap": "0.75rem"}, children=[
                            dcc.Checklist(
                                id="require-membership-toggle",
                                options=[{"label": "", "value": True}],
                                value=[True] if settings.get("require_membership") is True else [],
                                style={"display": "inline-block"},
                                inputStyle={"width": "18px", "height": "18px", "cursor": "pointer"},
                            ),
                            html.Div(children=[
                                html.Span("Require Membership (eBas)", style={"fontWeight": "600", "color": COLORS["text_primary"]}),
                                html.P("Player must be a Sverok member", style={"margin": "0", "fontSize": "0.75rem", "color": COLORS["text_muted"]}),
                            ]),
                        ]),

                        # Require Start.gg
                        html.Div(style={"marginBottom": "1.5rem", "display": "flex", "alignItems": "center", "gap": "0.75rem"}, children=[
                            dcc.Checklist(
                                id="require-startgg-toggle",
                                options=[{"label": "", "value": True}],
                                value=[True] if settings.get("require_startgg") is True else [],
                                style={"display": "inline-block"},
                                inputStyle={"width": "18px", "height": "18px", "cursor": "pointer"},
                            ),
                            html.Div(children=[
                                html.Span("Require Start.gg Registration", style={"fontWeight": "600", "color": COLORS["text_primary"]}),
                                html.P("Player must be registered in the tournament", style={"margin": "0", "fontSize": "0.75rem", "color": COLORS["text_muted"]}),
                            ]),
                        ]),

                        html.Hr(style={"border": "none", "borderTop": f"1px solid {COLORS['border']}", "margin": "1.5rem 0"}),

                        # Offer Membership (optional)
                        html.Div(style={"marginBottom": "1.5rem", "display": "flex", "alignItems": "center", "gap": "0.75rem"}, children=[
                            dcc.Checklist(
                                id="offer-membership-toggle",
                                options=[{"label": "", "value": True}],
                                value=[True] if settings.get("offer_membership") is True else [],
                                style={"display": "inline-block"},
                                inputStyle={"width": "18px", "height": "18px", "cursor": "pointer"},
                            ),
                            html.Div(children=[
                                html.Span("Offer Membership (optional)", style={"fontWeight": "600", "color": COLORS["text_primary"]}),
                                html.P("Show 'Become a member' on Ready page even when not required", style={"margin": "0", "fontSize": "0.75rem", "color": COLORS["text_muted"]}),
                            ]),
                        ]),

                        html.Button("Save Requirements", id="btn-save-requirements", n_clicks=0, style=STYLES["button_primary"]),
                        html.Div(id="requirements-save-feedback", style={"marginTop": "1rem"}),
                    ]),

                    # Payment Settings
                    html.Div(style=STYLES["card"], children=[
                        html.H3("Payment Settings", style=STYLES["section_title"]),
                        html.P("Configure Swish payment details for this event.", style={"color": COLORS["text_secondary"], "marginBottom": "1rem"}),

                        # Price per game
                        html.Div(style={"marginBottom": "1rem"}, children=[
                            html.Label("Price per game (kr)", style={"fontWeight": "600", "color": COLORS["text_primary"], "marginBottom": "0.5rem", "display": "block"}),
                            dcc.Input(
                                id="input-price-per-game",
                                type="number",
                                value=settings.get("swish_expected_per_game", 25),
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
                        ]),

                        # Swish number
                        html.Div(style={"marginBottom": "1rem"}, children=[
                            html.Label("Swish number", style={"fontWeight": "600", "color": COLORS["text_primary"], "marginBottom": "0.5rem", "display": "block"}),
                            dcc.Input(
                                id="input-swish-number",
                                type="text",
                                value=settings.get("swish_number", "123-456 78 90"),
                                style={
                                    "width": "200px",
                                    "padding": "0.5rem",
                                    "borderRadius": "6px",
                                    "border": f"1px solid {COLORS['border']}",
                                    "backgroundColor": COLORS["bg_dark"],
                                    "color": COLORS["text_primary"],
                                },
                            ),
                        ]),

                        html.Button("Save Payment Settings", id="btn-save-payment-settings", n_clicks=0, style=STYLES["button_primary"]),
                        html.Div(id="payment-settings-feedback", style={"marginTop": "1rem"}),
                    ]),

                    # Hidden elements to satisfy callback dependencies
                    html.Div(style={"display": "none"}, children=[
                        dcc.Dropdown(id="game-dropdown", options=[], value=None),
                        html.Div(id="game-help"),
                    ]),

                    # Column visibility settings
                    html.Div(style=STYLES["card"], children=[
                        html.H3("Table Columns", style=STYLES["section_title"]),
                        html.P("Choose which columns to display in the check-ins table.", style={"color": COLORS["text_secondary"], "marginBottom": "1rem"}),
                        dcc.Dropdown(
                            id="column-visibility-dropdown",
                            options=[opt for opt in [
                                {"label": "Name", "value": "name"},
                                {"label": "Tag", "value": "tag"},
                                {"label": "Status", "value": "status"},
                                {"label": "Payment", "value": "payment_valid"} if settings.get("require_payment") is True else None,
                                {"label": "Phone", "value": "telephone"},
                                {"label": "Member", "value": "member"} if settings.get("require_membership") is True else None,
                                {"label": "Start.gg", "value": "startgg"},
                                {"label": "Guest", "value": "is_guest"},
                                {"label": "Games", "value": "tournament_games_registered"},
                                {"label": "Email", "value": "email"},
                                {"label": "UUID", "value": "UUID"},
                                {"label": "Created", "value": "created"},
                            ] if opt is not None],
                            value=[c for c in ["name", "tag", "status", "payment_valid", "telephone", "member", "startgg", "is_guest", "tournament_games_registered"]
                                   if not (c == "payment_valid" and settings.get("require_payment") is not True)
                                   and not (c == "member" and settings.get("require_membership") is not True)],
                            multi=True,
                            clearable=False,
                            style={"backgroundColor": COLORS["bg_dark"]},
                        ),
                    ]),
                ]),
            ]),
        ]),

        # Footer
        html.Footer(style={
            "textAlign": "center",
            "padding": "2rem",
            "color": COLORS["text_muted"],
            "fontSize": "0.75rem",
            "borderTop": f"1px solid {COLORS['border']}",
            "marginTop": "2rem",
        }, children=[
            html.P("FGC Trollhättan Check-in System", style={"margin": "0"}),
        ]),
    ])

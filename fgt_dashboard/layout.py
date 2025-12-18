# layout.py
"""
FGC Dashboard Layout - Modern Esports Theme
"""
from dash import html, dcc, dash_table
from shared.airtable_api import get_all_event_slugs, get_active_slug, get_checkins
import pandas as pd
import logging

logger = logging.getLogger(__name__)

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

    # Format event name for display
    event_display = active_slug.replace("-", " ").title() if active_slug else "No Event Selected"

    return html.Div(style=STYLES["page"], children=[
        # Data stores
        dcc.Store(id="events-map-store", data=[]),
        dcc.Store(id="visible-columns-store", data=["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"]),
        dcc.Interval(id="interval-refresh", interval=30 * 1000, n_intervals=0),

        # Header
        html.Header(style={**STYLES["header"], "position": "relative"}, children=[
            # Centered logo
            html.Div(style={"display": "flex", "justifyContent": "center"}, children=[
                html.Img(src="/admin/assets/logo.png", style={"height": "60px", "width": "auto"}),
            ]),
            # LIVE indicator (top right)
            html.Div(style={"position": "absolute", "top": "1.5rem", "right": "2rem", "display": "flex", "alignItems": "center", "gap": "0.5rem"}, children=[
                html.Div(style={
                    "width": "10px",
                    "height": "10px",
                    "backgroundColor": COLORS["accent_green"],
                    "borderRadius": "50%",
                    "boxShadow": f"0 0 10px {COLORS['accent_green']}",
                }),
                html.Span("LIVE", style={
                    "color": COLORS["accent_green"],
                    "fontSize": "0.75rem",
                    "fontWeight": "600",
                    "letterSpacing": "0.1em",
                }),
            ]),
        ]),

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
                                options=[{"label": s.replace("-", " ").title(), "value": s} for s in event_slugs],
                                value=active_slug if active_slug in event_slugs else (event_slugs[0] if event_slugs else None),
                                clearable=False,
                                style={"backgroundColor": COLORS["bg_dark"]},
                            ),
                        ]),
                        html.Button("Refresh", id="btn-refresh", n_clicks=0, style={**STYLES["button_secondary"], "height": "38px"}),
                    ]),

                    # Stats cards
                    html.Div(id="stats-cards", style={"display": "flex", "gap": "1rem", "marginBottom": "1.5rem", "flexWrap": "wrap"}, children=[
                        html.Div(style={**STYLES["stat_card"], "borderTop": f"3px solid {COLORS['accent_blue']}"}, children=[
                            html.P(str(len(data)), id="stat-total", style={**STYLES["stat_value"], "color": COLORS["accent_blue"]}),
                            html.P("Total Players", style=STYLES["stat_label"]),
                        ]),
                        html.Div(style={**STYLES["stat_card"], "borderTop": f"3px solid {COLORS['accent_green']}"}, children=[
                            html.P(str(len([d for d in data if d.get("status") == "Ready"])), id="stat-ready", style={**STYLES["stat_value"], "color": COLORS["accent_green"]}),
                            html.P("Ready", style=STYLES["stat_label"]),
                        ]),
                        html.Div(style={**STYLES["stat_card"], "borderTop": f"3px solid {COLORS['accent_yellow']}"}, children=[
                            html.P(str(len([d for d in data if d.get("status") == "Pending"])), id="stat-pending", style={**STYLES["stat_value"], "color": COLORS["accent_yellow"]}),
                            html.P("Pending", style=STYLES["stat_label"]),
                        ]),
                        html.Div(style={**STYLES["stat_card"], "borderTop": f"3px solid {COLORS['accent_red']}"}, children=[
                            html.P("0", id="stat-attention", style={**STYLES["stat_value"], "color": COLORS["accent_red"]}),
                            html.P("Need Attention", style=STYLES["stat_label"]),
                        ]),
                    ]),

                    # Needs attention section
                    html.Div(id="needs-attention-section", style={
                        **STYLES["card"],
                        "borderLeft": f"4px solid {COLORS['accent_red']}",
                        "display": "none",  # Hidden by default, shown by callback when needed
                    }, children=[
                        html.H3("🚨 Needs Attention", style={**STYLES["section_title"], "color": COLORS["accent_red"]}),
                        html.Div(id="needs-attention-list", children=[
                            html.P("No issues", style={"color": COLORS["text_muted"], "margin": "0"})
                        ])
                    ]),

                    # Main checkins table
                    html.Div(style=STYLES["card"], children=[
                        html.Div(style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "marginBottom": "1rem"}, children=[
                            html.H3("Player List", style={**STYLES["section_title"], "margin": "0"}),
                            html.Span(f"{len(data)} players", style={"color": COLORS["text_muted"], "fontSize": "0.875rem"}),
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
                                filter_action="native",
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
                                style_filter={
                                    "backgroundColor": COLORS["bg_dark"],
                                    "color": COLORS["text_primary"],
                                    "border": f"1px solid {COLORS['border']}",
                                },
                                style_data_conditional=[
                                    # Ready status - green highlight
                                    {
                                        "if": {"filter_query": "{status} = 'Ready'"},
                                        "backgroundColor": "rgba(16, 185, 129, 0.1)",
                                        "borderLeft": f"3px solid {COLORS['accent_green']}",
                                    },
                                    # Pending status - yellow highlight
                                    {
                                        "if": {"filter_query": "{status} = 'Pending'"},
                                        "backgroundColor": "rgba(245, 158, 11, 0.1)",
                                        "borderLeft": f"3px solid {COLORS['accent_yellow']}",
                                    },
                                    # Hover effect
                                    {
                                        "if": {"state": "active"},
                                        "backgroundColor": COLORS["bg_card_hover"],
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
                    html.Div(style={"display": "flex", "gap": "1rem", "marginTop": "1rem", "alignItems": "center"}, children=[
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
                        html.Span("Click a row to select, then delete", style={"color": COLORS["text_muted"], "fontSize": "0.75rem"}),
                    ]),

                    # Feedback messages
                    html.Div(id="payment-update-feedback", style={"marginTop": "0.5rem"}),
                    html.Div(id="delete-feedback", style={"marginTop": "0.5rem"}),
                ]),

                # ========== TAB 2: Settings ==========
                html.Div(id="tab-settings-content", style={"display": "none"}, children=[
                    html.Div(style=STYLES["card"], children=[
                        html.H3("🎮 Event Configuration", style=STYLES["section_title"]),

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

                    html.Div(style=STYLES["card"], children=[
                        html.H3("🕹️ Active Games", style=STYLES["section_title"]),
                        html.P("Select which games are active for the current event.", style={"color": COLORS["text_secondary"], "marginBottom": "1rem"}),
                        dcc.Dropdown(
                            id="game-dropdown",
                            options=[],
                            value=None,
                            clearable=False,
                            multi=True,
                            style={"backgroundColor": COLORS["bg_dark"]},
                        ),
                        html.Div(id="game-help", style={"marginTop": "0.5rem", "color": COLORS["text_muted"], "fontSize": "0.875rem"}),
                    ]),

                    # Column visibility settings
                    html.Div(style=STYLES["card"], children=[
                        html.H3("📋 Table Columns", style=STYLES["section_title"]),
                        html.P("Choose which columns to display in the check-ins table.", style={"color": COLORS["text_secondary"], "marginBottom": "1rem"}),
                        dcc.Dropdown(
                            id="column-visibility-dropdown",
                            options=[
                                {"label": "Name", "value": "name"},
                                {"label": "Tag", "value": "tag"},
                                {"label": "Telephone", "value": "telephone"},
                                {"label": "Email", "value": "email"},
                                {"label": "Member", "value": "member"},
                                {"label": "Start.gg", "value": "startgg"},
                                {"label": "Payment Valid", "value": "payment_valid"},
                                {"label": "Status", "value": "status"},
                                {"label": "UUID", "value": "UUID"},
                                {"label": "Created", "value": "created"},
                            ],
                            value=["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"],  # Default TO view
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

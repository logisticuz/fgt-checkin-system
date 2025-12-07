# layout.py
from dash import html, dcc, dash_table
from shared.airtable_api import get_all_event_slugs, get_active_slug, get_checkins
import pandas as pd
import logging

logger = logging.getLogger(__name__)

def create_layout():
    """
    Build and return the layout for the FGC Check-in Dashboard.
    Data is fetched from Airtable via shared.airtable_api.
    """
    # Resolve active slug and all available slugs
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

    if not active_slug:
        logger.warning("No active_slug found – layout will render with empty table.")
    else:
        logger.info(f"Active slug: {active_slug}")

    if not event_slugs:
        logger.warning("No event slugs retrieved – dropdown will be empty.")

    # Ensure the active slug is selectable even if not present yet in data
    if active_slug and active_slug not in event_slugs:
        event_slugs = [active_slug] + event_slugs

    # Prefetch checkins for the initial view (if we have an active event)
    try:
        df = pd.DataFrame(get_checkins(active_slug)) if active_slug else pd.DataFrame()
    except Exception as e:
        logger.exception(f"Failed to fetch initial checkins for '{active_slug}': {e}")
        df = pd.DataFrame()

    if df.empty:
        logger.info("No checkins found – rendering empty table.")
        columns = [{"name": "No participants", "id": "info"}]
        data = []
    else:
        columns = [{"name": str(col), "id": str(col)} for col in df.columns]
        data = df.to_dict("records")
        logger.info(f"Loaded {len(df)} checkins for initial render.")

    return html.Div([
        # Store for events map (name -> {id, slug}) populated by callbacks
        dcc.Store(id="events-map-store", data=[]),

        dcc.Tabs(id="tabs", value="tab-checkins", children=[
            dcc.Tab(label="Live Check-ins", value="tab-checkins"),
            dcc.Tab(label="Settings", value="tab-settings"),
        ]),

        html.Div(id="tabs-content", children=[
            # Tab 1 – Live check-ins
            html.Div(id="tab-checkins-content", children=[
                html.H2("FGC Check-in Dashboard"),
                html.P(f"Active event: {active_slug}" if active_slug else "No active event found"),
                dcc.Dropdown(
                    id="event-dropdown",
                    options=[{"label": s, "value": s} for s in event_slugs],
                    value=active_slug if active_slug in event_slugs else (event_slugs[0] if event_slugs else None),
                    clearable=False
                ),
                html.Br(),
                dcc.Interval(id="interval-refresh", interval=10 * 1000, n_intervals=0),
                dcc.Loading(
                    type="default",
                    children=dash_table.DataTable(
                        id="checkins-table",
                        columns=columns,
                        data=data,
                        page_size=10,
                        style_table={"overflowX": "auto"},
                        style_data_conditional=[
                            {
                                "if": {"column_id": "info"},
                                "textAlign": "center",
                                "color": "gray",
                                "fontStyle": "italic",
                            }
                        ],
                    ),
                ),
            ]),

            # Tab 2 – Settings
            html.Div(id="tab-settings-content", children=[
                html.H2("Event Settings"),

                html.Label("Start.gg Event Link"),
                dcc.Input(
                    id="input-startgg-link",
                    type="text",
                    placeholder="Paste Start.gg tournament link",
                    style={"width": "100%"}
                ),
                html.Br(), html.Br(),
                html.Button("Fetch Event Data", id="btn-fetch-event", n_clicks=0),
                html.Div(id="settings-output", style={"marginTop": "1rem", "color": "green"}),

                html.Hr(),
                html.Label("Visible games (from Airtable default_game)"),
                dcc.Dropdown(
                    id="game-dropdown",
                    options=[],   # populated by callback
                    value=None,   # populated by callback
                    clearable=False
                ),
                html.Div(id="game-help", style={"marginTop": "0.5rem", "color": "#666"}),
            ]),
        ]),
    ])

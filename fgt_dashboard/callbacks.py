# callbacks.py
from dash.dependencies import Input, Output, State
from dash import no_update, html, ctx
from shared.airtable_api import (
    get_checkins,
    get_active_settings,
    get_active_settings_with_id,
    update_settings,
    update_checkin,
    delete_checkin,
)
import pandas as pd
import requests
import os
import logging
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Airtable & Start.gg API config
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
BASE_ID = os.getenv("AIRTABLE_BASE_ID")
SETTINGS_TABLE = "settings"
CHECKINS_TABLE = "active_event_data"
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")

def register_callbacks(app):
    """
    Register all Dash callbacks for:
    - Live check-ins table updates
    - Admin "Fetch Event Data" (Start.gg -> Airtable settings)
    - Populate game dropdown from Airtable (default_game) and expose events map
    """

    # ---------------------------------------------------------------------
    # Live check-ins table update (with column filtering, search, and quick filters)
    # ---------------------------------------------------------------------
    @app.callback(
        Output("checkins-table", "data"),
        Output("checkins-table", "columns"),
        Output("player-count", "children"),
        Output("game-filter", "options"),
        Input("event-dropdown", "value"),
        Input("interval-refresh", "n_intervals"),
        Input("btn-refresh", "n_clicks"),
        Input("sse-trigger", "data"),
        Input("visible-columns-store", "data"),
        Input("active-filter", "data"),
        Input("search-input", "value"),
        Input("game-filter", "value"),
    )
    def update_table(selected_slug, _interval, _clicks, _sse_trigger, visible_columns, active_filter, search_query, game_filter):
        """
        Refresh the check-ins table when:
        - user selects a different event slug
        - the interval timer ticks
        - visible columns change
        - filter or search changes
        """
        if not selected_slug:
            logger.warning("No event slug selected – skipping table update.")
            return no_update, no_update, no_update, no_update

        # Default columns if none specified
        if not visible_columns:
            visible_columns = ["name", "tag", "telephone", "member", "startgg", "is_guest", "payment_valid", "status"]

        # Handle special "__ALL__" value for debugging
        is_all_events = selected_slug == "__ALL__"
        logger.info(f"Updating check-ins table for: {'ALL EVENTS' if is_all_events else selected_slug}")

        try:
            if is_all_events:
                data = get_checkins(include_all=True) or []
            else:
                data = get_checkins(selected_slug) or []
            if not isinstance(data, list) or not data:
                logger.info(f"No check-ins found for slug: {selected_slug}")
                return [], [{"name": "No participants", "id": "info"}], "0 players", []

            df = pd.DataFrame(data)
            total_count = len(df)

            # Game name shortening map
            GAME_SHORT_NAMES = {
                "STREET FIGHTER 6 TOURNAMENT": "SF6",
                "STREET FIGHTER 6": "SF6",
                "TEKKEN 8 TOURNAMENT": "T8",
                "TEKKEN 8": "T8",
                "SMASH SINGLES": "SSBU",
                "SUPER SMASH BROS": "SSBU",
            }

            def shorten_game(name):
                """Shorten game name using mapping, case-insensitive."""
                return GAME_SHORT_NAMES.get(name.upper().strip(), name) if name else ""

            # Extract unique games for filter dropdown (before shortening)
            all_games = set()
            if "tournament_games_registered" in df.columns:
                for val in df["tournament_games_registered"]:
                    if isinstance(val, list):
                        for g in val:
                            if g:
                                all_games.add(shorten_game(g))
                    elif val:
                        all_games.add(shorten_game(val))
            game_options = [{"label": g, "value": g} for g in sorted(all_games)]

            # Format multi-select fields: shorten names + join with comma
            if "tournament_games_registered" in df.columns:
                df["tournament_games_registered"] = df["tournament_games_registered"].apply(
                    lambda x: ", ".join(shorten_game(g) for g in x) if isinstance(x, list) else shorten_game(x) if x else ""
                )

            # Store original boolean values for filtering before converting to icons
            for col in ["member", "startgg", "payment_valid", "is_guest"]:
                if col in df.columns:
                    df[f"_{col}_bool"] = df[col].apply(lambda x: x is True or str(x).lower() == "true")

            # Convert booleans to icons ✓/✗
            for col in ["member", "startgg", "payment_valid", "is_guest"]:
                if col in df.columns:
                    df[col] = df[col].apply(lambda x: "✓" if x is True or str(x).lower() == "true" else "✗")

            # Apply quick filter
            if active_filter == "pending":
                df = df[df["status"] == "Pending"]
            elif active_filter == "ready":
                df = df[df["status"] == "Ready"]
            elif active_filter == "no-payment":
                if "_payment_valid_bool" in df.columns:
                    df = df[df["_payment_valid_bool"] == False]

            # Apply search filter (case-insensitive on name and tag)
            if search_query:
                search_lower = search_query.lower()
                mask = pd.Series([False] * len(df), index=df.index)
                if "name" in df.columns:
                    mask = mask | df["name"].str.lower().str.contains(search_lower, na=False)
                if "tag" in df.columns:
                    mask = mask | df["tag"].str.lower().str.contains(search_lower, na=False)
                df = df[mask]

            # Apply game filter
            if game_filter and "tournament_games_registered" in df.columns:
                df = df[df["tournament_games_registered"].str.contains(game_filter, case=False, na=False)]

            filtered_count = len(df)

            # Drop helper columns before output
            helper_cols = [c for c in df.columns if c.startswith("_")]
            df = df.drop(columns=helper_cols, errors="ignore")

            # Include record_id in data for delete/update operations, but NOT in visible columns
            visible_cols = [c for c in visible_columns if c in df.columns]
            # Keep record_id in data but filter it from column list
            all_cols = ["record_id"] + visible_cols if "record_id" in df.columns else visible_cols
            df_filtered = df[[c for c in all_cols if c in df.columns]]

            # Column header display names (shorter/cleaner)
            COLUMN_HEADERS = {
                "name": "Name",
                "tag": "Tag",
                "status": "Status",
                "payment_valid": "Payment",
                "telephone": "Phone",
                "member": "Member",
                "startgg": "Start.gg",
                "is_guest": "Guest",
                "tournament_games_registered": "Games",
                "email": "Email",
                "UUID": "UUID",
                "created": "Created",
            }

            # Create column definitions - exclude record_id from display
            cols = []
            for c in visible_cols:
                if c in df_filtered.columns:
                    header = COLUMN_HEADERS.get(c, str(c).replace("_", " ").title())
                    col_def = {"name": header, "id": str(c), "reorderable": True}
                    cols.append(col_def)

            # Player count text
            if filtered_count == total_count:
                count_text = f"{total_count} players"
            else:
                count_text = f"{filtered_count} of {total_count} players"

            return df_filtered.to_dict("records"), cols, count_text, game_options

        except Exception as e:
            logger.exception(f"Error fetching check-ins for slug '{selected_slug}': {e}")
            return [], [{"name": "Error fetching data", "id": "error"}], "Error", []

    # ---------------------------------------------------------------------
    # Sync column visibility dropdown to store
    # ---------------------------------------------------------------------
    @app.callback(
        Output("visible-columns-store", "data"),
        Input("column-visibility-dropdown", "value"),
        prevent_initial_call=True,
    )
    def update_column_visibility(selected_columns):
        """Store the selected columns when TO changes visibility settings."""
        if not selected_columns:
            return ["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"]
        return selected_columns

    # ---------------------------------------------------------------------
    # Quick filter buttons - update active filter store
    # ---------------------------------------------------------------------
    @app.callback(
        Output("active-filter", "data"),
        Input("filter-all", "n_clicks"),
        Input("filter-pending", "n_clicks"),
        Input("filter-ready", "n_clicks"),
        Input("filter-no-payment", "n_clicks"),
        prevent_initial_call=True,
    )
    def update_active_filter(all_clicks, pending_clicks, ready_clicks, no_payment_clicks):
        """Update the active filter based on which button was clicked."""
        triggered = ctx.triggered_id
        if triggered == "filter-pending":
            return "pending"
        elif triggered == "filter-ready":
            return "ready"
        elif triggered == "filter-no-payment":
            return "no-payment"
        return "all"

    # ---------------------------------------------------------------------
    # Update stat card styles based on active filter
    # ---------------------------------------------------------------------
    @app.callback(
        Output("filter-all", "style"),
        Output("filter-ready", "style"),
        Output("filter-pending", "style"),
        Output("filter-no-payment", "style"),
        Input("active-filter", "data"),
    )
    def update_stat_card_styles(active_filter):
        """Highlight the active stat card filter with subtle indicator."""
        base_style = {
            "backgroundColor": "#12121a",
            "borderRadius": "12px",
            "border": "1px solid #1e293b",
            "padding": "1.25rem",
            "textAlign": "center",
            "flex": "1",
            "minWidth": "150px",
            "cursor": "pointer",
            "transition": "all 0.2s",
        }

        # Define colors for each card
        colors = {
            "all": "#00d4ff",      # accent_blue
            "ready": "#10b981",    # accent_green
            "pending": "#f59e0b",  # accent_yellow
            "no-payment": "#ef4444",  # accent_red
        }

        styles = {}
        for key in ["all", "ready", "pending", "no-payment"]:
            color = colors[key]
            if active_filter == key:
                # Active: scale up + soft glow
                styles[key] = {
                    **base_style,
                    "borderTop": f"3px solid {color}",
                    "transform": "scale(1.05)",
                    "boxShadow": f"0 4px 20px {color}50",
                }
            else:
                # Inactive: original style, full brightness
                styles[key] = {
                    **base_style,
                    "borderTop": f"3px solid {color}",
                }

        return styles["all"], styles["ready"], styles["pending"], styles["no-payment"]

    # ---------------------------------------------------------------------
    # Admin: Fetch event data from Start.gg and update Airtable settings
    # ---------------------------------------------------------------------
    @app.callback(
        Output("settings-output", "children"),
        Output("event-dropdown", "options"),
        Output("event-dropdown", "value"),
        Input("btn-fetch-event", "n_clicks"),
        State("input-startgg-link", "value"),
    )
    def fetch_event_data(n_clicks, link):
        """
        Admin action:
        1) Extract tournament slug from Start.gg URL
        2) Fetch tournament + events via GraphQL
        3) PATCH Airtable 'settings' row (is_active=TRUE()) with:
           - active_event_slug
           - event_date (ISO YYYY-MM-DD, UTC)
           - events_json (compact list)
           - default_game (multi-select)
           - startgg_event_url, tournament_name, timezone
        Behavior:
           - First time: default_game = ALL events
           - Later: keep TO's selections that still exist, drop removed, add new
        """
        if not n_clicks or not link:
            return no_update, no_update, no_update

        # Guards for env
        if not STARTGG_API_KEY:
            return "❌ Missing STARTGG_API_KEY.", no_update, no_update
        if not AIRTABLE_API_KEY or not BASE_ID:
            return "❌ Missing Airtable env (AIRTABLE_API_KEY/BASE_ID).", no_update, no_update

        # 1) Extract slug robustly
        try:
            parsed = urlparse(link.strip())
            m = re.search(r"/tournament/([^/]+)", parsed.path or "")
            if not m:
                logger.warning(f"Invalid Start.gg link: {link}")
                return "❌ Invalid Start.gg tournament link.", no_update, no_update
            slug = m.group(1)
            logger.info(f"Extracted slug: {slug}")
        except Exception:
            return "❌ Invalid URL format.", no_update, no_update

        # 2) Query Start.gg (GraphQL)
        gql = {
            "query": """
            query T($slug: String!) {
              tournament(slug: $slug) {
                id
                name
                startAt
                timezone
                events { id name slug startAt }
              }
            }
            """,
            "variables": {"slug": slug},
        }
        headers_startgg = {
            "Authorization": f"Bearer {STARTGG_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                "https://api.start.gg/gql/alpha",
                json=gql,
                headers=headers_startgg,
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            if "errors" in payload:
                logger.error(f"Start.gg errors: {payload['errors']}")
                return f"❌ Start.gg error: {payload['errors']}", no_update, no_update
            tournament = payload.get("data", {}).get("tournament")
            if not tournament:
                return "❌ Tournament not found.", no_update, no_update
        except Exception as e:
            logger.exception("Start.gg request failed")
            return f"❌ Start.gg request failed: {e}", no_update, no_update

        # Convert Unix -> ISO date (UTC)
        start_iso = None
        if tournament.get("startAt"):
            try:
                start_iso = datetime.fromtimestamp(
                    int(tournament["startAt"]), tz=timezone.utc
                ).date().isoformat()
            except Exception:
                start_iso = None

        # Build compact events array
        events = tournament.get("events") or []
        events_compact = [
            {"id": e.get("id"), "name": e.get("name"), "slug": e.get("slug"), "startAt": e.get("startAt")}
            for e in events if isinstance(e, dict)
        ]
        fetched_names = [e.get("name") for e in events if isinstance(e, dict) and e.get("name")]

        # 3) Find active settings row using shared airtable_api
        settings_data = get_active_settings_with_id()
        if not settings_data:
            return "❌ No settings record found in Airtable.", no_update, no_update
        settings_id = settings_data["record_id"]
        current_fields = settings_data.get("fields", {}) or {}

        # 4) Preserve TO selection & merge with new games
        current_selected = current_fields.get("default_game") or []  # list[str]
        prev_names = set()
        try:
            prev_raw = current_fields.get("events_json")
            if prev_raw:
                parsed_prev = prev_raw if isinstance(prev_raw, (list, dict)) else json.loads(prev_raw)
                prev_list = parsed_prev.get("events") if isinstance(parsed_prev, dict) else parsed_prev
                if isinstance(prev_list, list):
                    prev_names = {e.get("name") for e in prev_list if isinstance(e, dict) and e.get("name")}
        except Exception:
            prev_names = set()

        fetched_set = set(fetched_names)
        # Keep user's current picks that still exist
        keep = [n for n in current_selected if n in fetched_set]
        # Add truly new games
        new_candidates = [n for n in fetched_names if n not in prev_names and n not in keep]

        if not current_selected:
            # First time: select all
            new_selected = fetched_names
        else:
            # Preserve order: keep (user order) + new (Start.gg order)
            seen = set(keep)
            new_selected = keep + [n for n in new_candidates if n not in seen]

        # 5) Patch Airtable settings using shared airtable_api
        patch_fields = {
            "active_event_slug": slug,
            "event_display_name": tournament.get("name", ""),  # Store proper tournament name with åäö
            "is_active": True,
        }
        result = update_settings(settings_id, patch_fields)
        if not result:
            return "❌ Airtable update failed", no_update, no_update

        logger.info(f"Updated settings in Airtable for slug: {slug}")

        # Build new dropdown options with the new slug
        from shared.airtable_api import get_all_event_slugs
        all_slugs = get_all_event_slugs() or []
        if slug not in all_slugs:
            all_slugs = [slug] + all_slugs

        # Use tournament name (with proper åäö) for the active slug
        tournament_name = tournament.get("name", "")
        dropdown_options = [
            {"label": tournament_name if s == slug and tournament_name else s.replace("-", " ").title(), "value": s}
            for s in all_slugs
        ]

        return f"✅ Updated {tournament_name} • {len(events)} events", dropdown_options, slug

    # ---------------------------------------------------------------------
    # Helper: read active settings.fields from Airtable (uses shared airtable_api)
    # ---------------------------------------------------------------------
    def _get_active_settings_fields():
        result = get_active_settings_with_id()
        return result.get("fields") if result else None

    # ---------------------------------------------------------------------
    # Populate game-dropdown from Airtable (default_game) + mapping store
    # ---------------------------------------------------------------------
    @app.callback(
        Output("game-dropdown", "options"),
        Output("game-dropdown", "value"),
        Output("game-help", "children"),
        Output("events-map-store", "data"),
        Input("btn-fetch-event", "n_clicks"),
        Input("interval-refresh", "n_intervals"),
        Input("event-dropdown", "value"),
        prevent_initial_call=False,
    )
    def fill_game_dropdown(_n_clicks, _ticks, _selected_slug):
        """
        Populate the game dropdown from Airtable (default_game multi-select).
        Also provide name->(id, slug) mapping through events-map-store (from events_json).
        """
        fields = _get_active_settings_fields()
        if not fields:
            return [], None, "⚠️ Could not read active settings from Airtable.", []

        # 1) Options from default_game (multi-select)
        names = fields.get("default_game") or []  # list[str]
        options = [{"label": n, "value": n} for n in names]

        # 2) Mapping from events_json
        mapping = []
        raw = fields.get("events_json")
        if raw:
            try:
                parsed = raw if isinstance(raw, (list, dict)) else json.loads(raw)
                evs = parsed.get("events") if isinstance(parsed, dict) else parsed
                if isinstance(evs, list):
                    mapping = [
                        {"name": e.get("name"), "id": e.get("id"), "slug": e.get("slug")}
                        for e in evs if isinstance(e, dict)
                    ]
            except Exception:
                logger.warning("Failed to parse events_json")

        # 3) Default value: pick the first option if any
        value = options[0]["value"] if options else None

        # 4) Help text
        help_text = "Games populated from Airtable (default_game)."

        return options, value, help_text, mapping

    # -------------------------------------------------------------------------
    # Populate "Needs Attention" section with players missing requirements
    # -------------------------------------------------------------------------
    @app.callback(
        Output("needs-attention-list", "children"),
        Output("needs-attention-count", "children"),
        Input("checkins-table", "data"),
        Input("requirements-store", "data"),  # Task 2.2: Respect requirements
    )
    def update_needs_attention(table_data, requirements):
        """
        Build a list of players who are missing ACTIVE requirements only.
        Shows what each player is missing with icons to help TOs prioritize assistance.
        Disabled requirements never appear in "Needs Attention" (Task 2.2).
        """
        from dash import html

        if not table_data:
            return html.P("No players checked in yet.", style={"color": "#888"}), "0"

        # Get active requirements (default ON unless explicitly False)
        requirements = requirements or {}
        require_membership = requirements.get("require_membership") is not False
        require_payment = requirements.get("require_payment") is not False
        require_startgg = requirements.get("require_startgg") is not False

        # Helper to check if value indicates "OK" (includes icon ✓ or boolean true)
        def is_ok(val):
            return val == "✓" or val is True or str(val).lower() == "true"

        needs_help = []
        for row in table_data:
            missing = []

            # Only check membership if it's a required field
            if require_membership:
                member_val = row.get("member", "")
                if not is_ok(member_val):
                    missing.append({"field": "Membership", "icon": "🎫"})

            # Only check payment if it's a required field
            if require_payment:
                payment_val = row.get("payment_valid", "")
                if not is_ok(payment_val):
                    missing.append({"field": "Payment", "icon": "💳"})

            # Only check start.gg if it's a required field
            if require_startgg:
                startgg_val = row.get("startgg", "")
                if not is_ok(startgg_val):
                    missing.append({"field": "Start.gg", "icon": "🎮"})

            if missing:
                name = row.get("name") or row.get("tag") or "Unknown"
                tag = row.get("tag", "")
                needs_help.append({
                    "name": name,
                    "tag": tag,
                    "missing": missing,
                    "record_id": row.get("record_id", ""),
                })

        if not needs_help:
            return html.P("✅ All players are ready!", style={"color": "#10b981", "fontWeight": "600"}), "0"

        # Badge styles matching Active Requirements (same colors/styling)
        badge_styles = {
            "Payment": {
                "backgroundColor": "rgba(16, 185, 129, 0.2)",
                "color": "#10b981",
                "border": "1px solid rgba(16, 185, 129, 0.3)",
            },
            "Membership": {
                "backgroundColor": "rgba(139, 92, 246, 0.2)",
                "color": "#8b5cf6",
                "border": "1px solid rgba(139, 92, 246, 0.3)",
            },
            "Start.gg": {
                "backgroundColor": "rgba(0, 212, 255, 0.2)",
                "color": "#00d4ff",
                "border": "1px solid rgba(0, 212, 255, 0.3)",
            },
        }
        badge_base = {
            "padding": "0.25rem 0.5rem",
            "borderRadius": "4px",
            "fontSize": "0.75rem",
            "fontWeight": "600",
            "marginLeft": "0.25rem",
        }

        # Build list of players needing help with badge-styled requirements
        items = []
        for player in needs_help:
            display_name = player["name"]
            if player["tag"] and player["tag"] != player["name"]:
                display_name = f"{player['name']} ({player['tag']})"

            # Create badges for each missing requirement
            missing_badges = []
            for m in player["missing"]:
                field = m["field"]
                icon = m["icon"]
                style = {**badge_base, **badge_styles.get(field, badge_styles["Payment"])}
                missing_badges.append(html.Span(f"{icon} {field.upper()}", style=style))

            items.append(
                html.Div([
                    # Player name with bullet point
                    html.Div([
                        html.Span("•", style={"color": "#ef4444", "marginRight": "0.5rem", "fontSize": "1.2rem"}),
                        html.Span(display_name, style={"color": "#fff", "fontWeight": "500"}),
                    ], style={"display": "flex", "alignItems": "center", "marginBottom": "0.25rem"}),
                    # Missing badges on second line, indented
                    html.Div(missing_badges, style={
                        "display": "flex",
                        "gap": "0.5rem",
                        "flexWrap": "wrap",
                        "marginLeft": "1rem",
                    }),
                ], style={
                    "padding": "0.75rem",
                    "marginBottom": "0.5rem",
                    "backgroundColor": "rgba(239, 68, 68, 0.05)",
                    "borderRadius": "8px",
                    "borderLeft": "3px solid #ef4444",
                })
            )

        return items, str(len(needs_help))

    # -------------------------------------------------------------------------
    # Toggle "Needs Attention" collapse state
    # -------------------------------------------------------------------------
    @app.callback(
        Output("needs-attention-list", "style"),
        Output("needs-attention-chevron", "children"),
        Input("needs-attention-header", "n_clicks"),
        State("needs-attention-list", "style"),
        prevent_initial_call=True,
    )
    def toggle_needs_attention(n_clicks, current_style):
        """Toggle the collapsed state of the Needs Attention section."""
        current_style = current_style or {}
        is_hidden = current_style.get("display") == "none"

        if is_hidden:
            # Expand
            return {"marginTop": "1rem"}, "▼"
        else:
            # Collapse
            return {"display": "none"}, "▶"

    # -------------------------------------------------------------------------
    # Tab switching - show/hide content based on selected tab
    # -------------------------------------------------------------------------
    @app.callback(
        Output("tab-checkins-content", "style"),
        Output("tab-settings-content", "style"),
        Input("tabs", "value"),
    )
    def switch_tabs(selected_tab):
        """
        Toggle visibility of tab content based on selected tab.
        """
        if selected_tab == "tab-settings":
            return {"display": "none"}, {"display": "block"}
        else:
            return {"display": "block"}, {"display": "none"}

    # -------------------------------------------------------------------------
    # Reactive Stats - update stat cards when table data changes
    # -------------------------------------------------------------------------
    @app.callback(
        Output("stat-total", "children"),
        Output("stat-ready", "children"),
        Output("stat-pending", "children"),
        Output("stat-attention", "children"),
        Output("needs-attention-section", "style"),
        Input("checkins-table", "data"),
        Input("requirements-store", "data"),  # Task 2.2: Respect requirements
    )
    def update_stats(table_data, requirements):
        """
        Update stats cards reactively when check-ins table data changes.
        Also shows/hides the needs-attention section.
        Respects configurable requirements (Task 2.2).
        """
        if not table_data:
            hidden_style = {"display": "none"}
            return "0", "0", "0", "0", hidden_style

        # Get active requirements (default ON unless explicitly False)
        requirements = requirements or {}
        require_membership = requirements.get("require_membership") is not False
        require_payment = requirements.get("require_payment") is not False
        require_startgg = requirements.get("require_startgg") is not False

        # Helper to check if value indicates "OK" (includes icon ✓ or boolean true)
        def is_ok(val):
            return val == "✓" or val is True or str(val).lower() == "true"

        total = len(table_data)
        ready = len([d for d in table_data if d.get("status") == "Ready"])
        pending = len([d for d in table_data if d.get("status") == "Pending"])

        # Count players needing attention (only for ACTIVE requirements)
        needs_attention = 0
        for row in table_data:
            missing_something = False
            # Only check membership if required
            if require_membership and not is_ok(row.get("member", "")):
                missing_something = True
            # Only check payment if required
            if require_payment and not is_ok(row.get("payment_valid", "")):
                missing_something = True
            # Only check start.gg if required
            if require_startgg and not is_ok(row.get("startgg", "")):
                missing_something = True
            if missing_something:
                needs_attention += 1

        # Show needs-attention section if there are players needing help
        if needs_attention > 0:
            visible_style = {
                "backgroundColor": "#12121a",
                "borderRadius": "12px",
                "border": "1px solid #1e293b",
                "padding": "1.5rem",
                "marginBottom": "1.5rem",
                "borderLeft": "4px solid #ef4444",
                "display": "block",
            }
        else:
            visible_style = {"display": "none"}

        return str(total), str(ready), str(pending), str(needs_attention), visible_style

    # -------------------------------------------------------------------------
    # TO Toggle Fields - toggle payment_valid, startgg, is_guest by clicking cell
    # -------------------------------------------------------------------------
    @app.callback(
        Output("payment-update-feedback", "children"),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("checkins-table", "active_cell"),
        State("checkins-table", "data"),
        State("event-dropdown", "value"),
        prevent_initial_call=True,
    )
    def toggle_field(active_cell, table_data, selected_slug):
        """
        When TO clicks on a cell in toggleable columns (payment_valid, startgg, is_guest), toggle it.
        If all required fields are OK, set status to Ready.
        """
        if not active_cell or not table_data:
            return no_update, no_update

        row_idx = active_cell.get("row")
        col_id = active_cell.get("column_id")

        # Only trigger on toggleable columns (is_guest is set automatically by system)
        # member added as workaround until eBas Register → Airtable update is implemented
        toggleable_columns = ["payment_valid", "startgg", "member"]
        if col_id not in toggleable_columns:
            return no_update, no_update

        if row_idx is None or row_idx >= len(table_data):
            return no_update, no_update

        row = table_data[row_idx]
        record_id = row.get("record_id")
        player_name = row.get("name") or row.get("tag") or "Unknown"

        if not record_id:
            return html.Span("❌ No record_id found for this player.", style={"color": "#ef4444"}), no_update

        # Helper to check if value is truthy
        def is_checked(val):
            return val == "✓" or val is True or str(val).lower() == "true"

        # Toggle the clicked field
        current_val = row.get(col_id)
        new_val = not is_checked(current_val)

        # Build update dict
        update_data = {col_id: new_val}

        # Fetch configurable requirements from settings
        # Airtable checkbox: checked = True, unchecked = field missing (None)
        # Use "is True" so that unchecked (None) = requirement OFF
        settings = get_active_settings() or {}
        require_payment = settings.get("require_payment") is True
        require_membership = settings.get("require_membership") is True
        require_startgg = settings.get("require_startgg") is True

        # Determine current field values (with the toggled value updated)
        if col_id == "payment_valid":
            payment_val = new_val
            member_val = is_checked(row.get("member", ""))
            startgg_val = is_checked(row.get("startgg", ""))
        elif col_id == "startgg":
            payment_val = is_checked(row.get("payment_valid", ""))
            member_val = is_checked(row.get("member", ""))
            startgg_val = new_val
        elif col_id == "member":
            payment_val = is_checked(row.get("payment_valid", ""))
            member_val = new_val
            startgg_val = is_checked(row.get("startgg", ""))
        else:  # is_guest - doesn't affect Ready status
            payment_val = is_checked(row.get("payment_valid", ""))
            member_val = is_checked(row.get("member", ""))
            startgg_val = is_checked(row.get("startgg", ""))

        # Check each requirement (skip if not required)
        payment_ok = (not require_payment) or payment_val
        member_ok = (not require_membership) or member_val
        startgg_ok = (not require_startgg) or startgg_val

        new_status = "Ready" if (payment_ok and member_ok and startgg_ok) else "Pending"
        update_data["status"] = new_status

        # Update Airtable
        result = update_checkin(record_id, update_data)

        if result:
            # Update local table data for immediate feedback
            table_data[row_idx][col_id] = "✓" if new_val else "✗"
            table_data[row_idx]["status"] = new_status

            # Broadcast SSE to notify status pages
            try:
                requests.post(
                    "http://backend:8000/api/notify/update",
                    json={
                        "type": "field_updated",
                        "record_id": record_id,
                        "field": col_id,
                        "value": new_val,
                        "player_name": player_name,
                    },
                    timeout=2,
                )
            except Exception as e:
                logger.warning(f"Failed to broadcast SSE: {e}")

            # Feedback message based on column
            col_labels = {"payment_valid": "Payment", "startgg": "Start.gg", "member": "Member", "is_guest": "Guest"}
            col_label = col_labels.get(col_id, col_id)
            status_emoji = "✅" if new_val else "⏸️"

            feedback = html.Span(
                f"{status_emoji} {player_name}: {col_label} {'✓' if new_val else '✗'}, status={new_status}",
                style={"color": "#10b981" if new_val else "#f59e0b"}
            )
            return feedback, table_data
        else:
            logger.error(f"Failed to update {col_id} for {player_name}")
            return html.Span(f"❌ Failed to update {col_id}", style={"color": "#ef4444"}), no_update

    # -------------------------------------------------------------------------
    # Delete selected player - Step 1: Show confirmation dialog
    # -------------------------------------------------------------------------
    @app.callback(
        Output("confirm-delete-dialog", "displayed"),
        Output("confirm-delete-dialog", "message"),
        Output("delete-feedback", "children", allow_duplicate=True),
        Input("btn-delete-selected", "n_clicks"),
        State("checkins-table", "selected_rows"),
        State("checkins-table", "data"),
        prevent_initial_call=True,
    )
    def show_delete_confirmation(n_clicks, selected_rows, table_data):
        """Show confirmation dialog before deleting player(s)."""
        if not n_clicks or not selected_rows or not table_data:
            return False, "", html.Span("⚠️ No row selected.", style={"color": "#f59e0b"}) if n_clicks else no_update

        # Build list of names to delete
        names = []
        for row_idx in selected_rows:
            if row_idx is not None and row_idx < len(table_data):
                row = table_data[row_idx]
                player_name = row.get("name") or row.get("tag") or "Unknown"
                player_tag = row.get("tag") or ""
                display_name = f"{player_name} ({player_tag})" if player_tag and player_tag != player_name else player_name
                names.append(display_name)

        if not names:
            return False, "", html.Span("⚠️ No row selected.", style={"color": "#f59e0b"})

        if len(names) == 1:
            message = f"Are you sure you want to delete {names[0]}?\n\nThis action cannot be undone."
        else:
            message = f"Are you sure you want to delete {len(names)} players?\n\n• " + "\n• ".join(names) + "\n\nThis action cannot be undone."

        return True, message, no_update

    # -------------------------------------------------------------------------
    # Delete selected player - Step 2: Execute delete after confirmation
    # -------------------------------------------------------------------------
    @app.callback(
        Output("delete-feedback", "children"),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("confirm-delete-dialog", "submit_n_clicks"),
        State("checkins-table", "selected_rows"),
        State("checkins-table", "data"),
        prevent_initial_call=True,
    )
    def delete_selected_player(submit_n_clicks, selected_rows, table_data):
        """Delete the selected player(s) from Airtable after confirmation."""
        if not submit_n_clicks or not selected_rows or not table_data:
            return no_update, no_update

        deleted_names = []
        failed_names = []
        rows_to_remove = set()

        for row_idx in selected_rows:
            if row_idx is None or row_idx >= len(table_data):
                continue

            row = table_data[row_idx]
            record_id = row.get("record_id")
            player_name = row.get("name") or row.get("tag") or "Unknown"

            if not record_id:
                failed_names.append(player_name)
                continue

            # Delete from Airtable
            if delete_checkin(record_id):
                deleted_names.append(player_name)
                rows_to_remove.add(row_idx)
            else:
                logger.error(f"Failed to delete {player_name}")
                failed_names.append(player_name)

        # Remove deleted rows from table data
        table_data = [r for i, r in enumerate(table_data) if i not in rows_to_remove]

        # Build feedback message
        if deleted_names and not failed_names:
            if len(deleted_names) == 1:
                feedback = html.Span(f"🗑️ Deleted: {deleted_names[0]}", style={"color": "#ef4444"})
            else:
                feedback = html.Span(f"🗑️ Deleted {len(deleted_names)} players", style={"color": "#ef4444"})
        elif failed_names and not deleted_names:
            feedback = html.Span(f"❌ Failed to delete: {', '.join(failed_names)}", style={"color": "#ef4444"})
        elif deleted_names and failed_names:
            feedback = html.Span(f"🗑️ Deleted {len(deleted_names)}, ❌ Failed: {len(failed_names)}", style={"color": "#f59e0b"})
        else:
            feedback = html.Span("⚠️ No rows to delete", style={"color": "#f59e0b"})

        return feedback, table_data

    # -------------------------------------------------------------------------
    # Export Guests CSV - download guest players for bulk Start.gg import
    # -------------------------------------------------------------------------
    @app.callback(
        Output("download-guests-csv", "data"),
        Output("export-feedback", "children"),
        Input("btn-export-guests", "n_clicks"),
        State("checkins-table", "data"),
        prevent_initial_call=True,
    )
    def export_guests_csv(n_clicks, table_data):
        """
        Export guests (players without Start.gg accounts) as CSV.
        Useful for TOs to bulk-add players to Start.gg.
        """
        if not n_clicks or not table_data:
            return no_update, no_update

        # Helper to check if value indicates guest
        def is_guest(val):
            return val == "✓" or val is True or str(val).lower() == "true"

        # Filter for guests only
        guests = [row for row in table_data if is_guest(row.get("is_guest", ""))]

        if not guests:
            return no_update, html.Span(
                "⚠️ No guests found to export.",
                style={"color": "#f59e0b"}
            )

        # Build DataFrame with relevant columns for Start.gg import
        export_data = []
        for g in guests:
            export_data.append({
                "Name": g.get("name", ""),
                "Tag": g.get("tag", ""),
                "Email": g.get("email", ""),
                "Phone": g.get("telephone", ""),
                "Games": g.get("tournament_games_registered", ""),
            })

        df = pd.DataFrame(export_data)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"guests_export_{timestamp}.csv"

        feedback = html.Span(
            f"✅ Exported {len(guests)} guests to CSV",
            style={"color": "#10b981"}
        )

        return dict(content=df.to_csv(index=False), filename=filename), feedback

    # -------------------------------------------------------------------------
    # Save Check-in Requirements - update settings in Airtable
    # -------------------------------------------------------------------------
    @app.callback(
        Output("requirements-save-feedback", "children"),
        Output("requirements-store", "data"),
        Input("btn-save-requirements", "n_clicks"),
        State("require-payment-toggle", "value"),
        State("require-membership-toggle", "value"),
        State("require-startgg-toggle", "value"),
        State("requirements-store", "data"),
        prevent_initial_call=True,
    )
    def save_requirements(n_clicks, req_payment, req_membership, req_startgg, current_store):
        """Save check-in requirement settings to Airtable."""
        if not n_clicks:
            return no_update, no_update

        # Get the active settings record ID
        settings_data = get_active_settings_with_id()
        if not settings_data:
            return html.Span("❌ No active settings found", style={"color": "#ef4444"}), no_update

        record_id = settings_data.get("record_id")
        if not record_id:
            return html.Span("❌ Could not find settings record", style={"color": "#ef4444"}), no_update

        # Prepare update data (checklist returns [True] if checked, [] if not)
        update_data = {
            "require_payment": bool(req_payment),
            "require_membership": bool(req_membership),
            "require_startgg": bool(req_startgg),
        }

        # Update Airtable
        result = update_settings(record_id, update_data)

        if result:
            # Build summary of what's enabled
            enabled = []
            if req_payment:
                enabled.append("Payment")
            if req_membership:
                enabled.append("Membership")
            if req_startgg:
                enabled.append("Start.gg")

            if enabled:
                summary = f"Requiring: {', '.join(enabled)}"
            else:
                summary = "No requirements (all players auto-Ready)"

            # Update the store with new values
            new_store = {
                "require_payment": bool(req_payment),
                "require_membership": bool(req_membership),
                "require_startgg": bool(req_startgg),
            }

            return html.Span(f"✅ Saved! {summary}", style={"color": "#10b981"}), new_store
        else:
            return html.Span("❌ Failed to save settings", style={"color": "#ef4444"}), no_update

    # -------------------------------------------------------------------------
    # Render requirement badges based on requirements-store
    # -------------------------------------------------------------------------
    @app.callback(
        Output("requirement-badges", "children"),
        Input("requirements-store", "data"),
    )
    def render_requirement_badges(requirements):
        """Render badges showing which requirements are active."""
        if not requirements:
            return html.Span("Loading...", style={"color": "#64748b"})

        badges = []
        badge_style_active = {
            "padding": "0.25rem 0.5rem",
            "borderRadius": "4px",
            "fontSize": "0.7rem",
            "fontWeight": "600",
            "textTransform": "uppercase",
        }
        badge_style_inactive = {
            **badge_style_active,
            "backgroundColor": "rgba(100, 116, 139, 0.2)",
            "color": "#64748b",
            "textDecoration": "line-through",
        }

        # Default to True if not explicitly False
        req_payment = requirements.get("require_payment", True)
        req_membership = requirements.get("require_membership", True)
        req_startgg = requirements.get("require_startgg", True)

        # Payment badge
        if req_payment:
            badges.append(html.Span("💳 Payment", style={
                **badge_style_active,
                "backgroundColor": "rgba(16, 185, 129, 0.2)",
                "color": "#10b981",
            }))
        else:
            badges.append(html.Span("💳 Payment", style=badge_style_inactive))

        # Membership badge
        if req_membership:
            badges.append(html.Span("🎫 Membership", style={
                **badge_style_active,
                "backgroundColor": "rgba(139, 92, 246, 0.2)",
                "color": "#8b5cf6",
            }))
        else:
            badges.append(html.Span("🎫 Membership", style=badge_style_inactive))

        # Start.gg badge
        if req_startgg:
            badges.append(html.Span("🎮 Start.gg", style={
                **badge_style_active,
                "backgroundColor": "rgba(0, 212, 255, 0.2)",
                "color": "#00d4ff",
            }))
        else:
            badges.append(html.Span("🎮 Start.gg", style=badge_style_inactive))

        # If nothing required, add a note
        if not any([req_payment, req_membership, req_startgg]):
            badges.append(html.Span("(All players auto-Ready)", style={"color": "#f59e0b", "fontStyle": "italic", "marginLeft": "0.5rem"}))

        return badges

    # -------------------------------------------------------------------------
    # Sync checkbox values with requirements-store (keeps UI in sync)
    # -------------------------------------------------------------------------
    @app.callback(
        Output("require-payment-toggle", "value"),
        Output("require-membership-toggle", "value"),
        Output("require-startgg-toggle", "value"),
        Input("requirements-store", "data"),
    )
    def sync_checkboxes_with_store(requirements):
        """Sync the settings checkboxes with the requirements store."""
        if not requirements:
            # Default all to True if no store data
            return [True], [True], [True]

        # Default to True (checked) unless explicitly False
        payment_val = [True] if requirements.get("require_payment", True) else []
        membership_val = [True] if requirements.get("require_membership", True) else []
        startgg_val = [True] if requirements.get("require_startgg", True) else []

        return payment_val, membership_val, startgg_val

    # -------------------------------------------------------------------------
    # Save Payment Settings - update swish_expected_per_game and swish_number
    # -------------------------------------------------------------------------
    @app.callback(
        Output("payment-settings-feedback", "children"),
        Input("btn-save-payment-settings", "n_clicks"),
        State("input-price-per-game", "value"),
        State("input-swish-number", "value"),
        prevent_initial_call=True,
    )
    def save_payment_settings(n_clicks, price_per_game, swish_number):
        """Save payment settings (price per game, swish number) to Airtable."""
        if not n_clicks:
            return no_update

        # Get the active settings record ID
        settings_data = get_active_settings_with_id()
        if not settings_data:
            return html.Span("❌ No active settings found", style={"color": "#ef4444"})

        record_id = settings_data.get("record_id")
        if not record_id:
            return html.Span("❌ Could not find settings record", style={"color": "#ef4444"})

        # Validate price
        try:
            price = int(price_per_game) if price_per_game else 0
            if price < 0:
                return html.Span("❌ Price must be 0 or higher", style={"color": "#ef4444"})
        except (ValueError, TypeError):
            return html.Span("❌ Invalid price value", style={"color": "#ef4444"})

        # Prepare update data
        update_data = {
            "swish_expected_per_game": price,
            "swish_number": swish_number or "",
        }

        # Update Airtable
        result = update_settings(record_id, update_data)

        if result:
            return html.Span(
                f"✅ Saved! Price: {price} kr/game, Swish: {swish_number}",
                style={"color": "#10b981"}
            )
        else:
            return html.Span("❌ Failed to save settings", style={"color": "#ef4444"})

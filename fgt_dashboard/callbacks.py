# callbacks.py
from dash.dependencies import Input, Output, State
from dash import no_update, html, ctx
from shared.airtable_api import get_checkins  # single source of truth
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
    # Live check-ins table update (with column filtering)
    # ---------------------------------------------------------------------
    @app.callback(
        Output("checkins-table", "data"),
        Output("checkins-table", "columns"),
        Input("event-dropdown", "value"),
        Input("interval-refresh", "n_intervals"),
        Input("btn-refresh", "n_clicks"),
        Input("visible-columns-store", "data"),
    )
    def update_table(selected_slug, _interval, _clicks, visible_columns):
        """
        Refresh the check-ins table when:
        - user selects a different event slug
        - the interval timer ticks
        - visible columns change
        """
        if not selected_slug:
            logger.warning("No event slug selected – skipping table update.")
            return no_update, no_update

        # Default columns if none specified
        if not visible_columns:
            visible_columns = ["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"]

        logger.info(f"Updating check-ins table for slug: {selected_slug}")
        try:
            data = get_checkins(selected_slug) or []
            if not isinstance(data, list) or not data:
                logger.info(f"No check-ins found for slug: {selected_slug}")
                return [], [{"name": "No participants", "id": "info"}]

            df = pd.DataFrame(data)

            # Always include record_id for delete/update operations (hidden from display)
            all_cols = ["record_id"] + [c for c in visible_columns if c in df.columns]
            df_filtered = df[[c for c in all_cols if c in df.columns]]

            # Create column definitions - hide record_id
            cols = []
            for c in df_filtered.columns:
                col_def = {"name": str(c).replace("_", " ").title(), "id": str(c)}
                if c == "record_id":
                    col_def["hideable"] = True
                    col_def["hidden"] = True
                cols.append(col_def)

            return df_filtered.to_dict("records"), cols

        except Exception as e:
            logger.exception(f"Error fetching check-ins for slug '{selected_slug}': {e}")
            return [], [{"name": "Error fetching data", "id": "error"}]

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
    # Admin: Fetch event data from Start.gg and update Airtable settings
    # ---------------------------------------------------------------------
    @app.callback(
        Output("settings-output", "children"),
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
            return no_update

        # Guards for env
        if not STARTGG_API_KEY:
            return "❌ Missing STARTGG_API_KEY."
        if not AIRTABLE_API_KEY or not BASE_ID:
            return "❌ Missing Airtable env (AIRTABLE_API_KEY/BASE_ID)."

        # 1) Extract slug robustly
        try:
            parsed = urlparse(link.strip())
            m = re.search(r"/tournament/([^/]+)", parsed.path or "")
            if not m:
                logger.warning(f"Invalid Start.gg link: {link}")
                return "❌ Invalid Start.gg tournament link."
            slug = m.group(1)
            logger.info(f"Extracted slug: {slug}")
        except Exception:
            return "❌ Invalid URL format."

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
                return f"❌ Start.gg error: {payload['errors']}"
            tournament = payload.get("data", {}).get("tournament")
            if not tournament:
                return "❌ Tournament not found."
        except Exception as e:
            logger.exception("Start.gg request failed")
            return f"❌ Start.gg request failed: {e}"

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

        # 3) Find active settings row
        settings_url = f"https://api.airtable.com/v0/{BASE_ID}/{SETTINGS_TABLE}"
        headers_airtable = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            q = requests.get(
                settings_url,
                headers=headers_airtable,
                params={"filterByFormula": "{is_active}=TRUE()", "maxRecords": 1},
                timeout=20,
            )
            q.raise_for_status()
            records = q.json().get("records", [])
            if not records:
                # Fallback: take the very first record
                q2 = requests.get(settings_url, headers=headers_airtable, params={"maxRecords": 1}, timeout=20)
                q2.raise_for_status()
                records = q2.json().get("records", [])
            if not records:
                return "❌ No settings record found in Airtable."
            settings_id = records[0]["id"]
            current_fields = records[0].get("fields", {}) or {}
        except Exception as e:
            logger.exception("Airtable read failed")
            return f"❌ Airtable read failed: {e}"

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

        # 5) Patch Airtable settings - only store slug, fetch rest from Start.gg when needed
        patch_fields = {
            "active_event_slug": slug,
            "is_active": True,
        }
        try:
            patch = requests.patch(
                f"{settings_url}/{settings_id}",
                json={"fields": patch_fields},
                headers=headers_airtable,
                timeout=20,
            )
            patch.raise_for_status()
        except Exception as e:
            logger.exception("Airtable update failed")
            return f"❌ Airtable update failed: {e}"

        logger.info(f"Updated settings in Airtable for slug: {slug}")
        return f"✅ Updated {tournament['name']} • {len(events)} events • Visible in form: {len(new_selected)}"

    # ---------------------------------------------------------------------
    # Helper: read active settings.fields from Airtable
    # ---------------------------------------------------------------------
    def _get_active_settings_fields():
        url = f"https://api.airtable.com/v0/{BASE_ID}/{SETTINGS_TABLE}"
        headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
        try:
            r = requests.get(
                url,
                headers=headers,
                params={"filterByFormula": "{is_active}=TRUE()", "maxRecords": 1},
                timeout=15,
            )
            r.raise_for_status()
            recs = r.json().get("records", [])
            if not recs:
                return None
            return recs[0].get("fields", {})
        except Exception as e:
            logger.exception(f"Airtable settings fetch failed: {e}")
            return None

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
        Input("checkins-table", "data"),
    )
    def update_needs_attention(table_data):
        """
        Build a list of players who are missing membership, payment, or start.gg registration.
        Shows what each player is missing to help TOs prioritize assistance.
        """
        from dash import html

        if not table_data:
            return html.P("No players checked in yet.", style={"color": "#888"})

        needs_help = []
        for row in table_data:
            missing = []

            # Check membership status
            member_val = row.get("member") or row.get("medlem")
            if not member_val or str(member_val).lower() in ("false", "0", "no"):
                missing.append("Membership")

            # Check payment status
            payment_val = row.get("payment_valid") or row.get("swish_valid")
            if not payment_val or str(payment_val).lower() in ("false", "0", "no"):
                missing.append("Payment")

            # Check start.gg registration
            startgg_val = row.get("startgg")
            if not startgg_val or str(startgg_val).lower() in ("false", "0", "no"):
                missing.append("Start.gg")

            if missing:
                name = row.get("name") or row.get("tag") or "Unknown"
                needs_help.append({
                    "name": name,
                    "missing": missing,
                    "record_id": row.get("record_id", ""),
                })

        if not needs_help:
            return html.P("All players are ready!", style={"color": "#4caf50"})

        # Build list of players needing help
        items = []
        for player in needs_help:
            missing_str = ", ".join(player["missing"])
            items.append(
                html.Div([
                    html.Strong(player["name"], style={"color": "#fff"}),
                    html.Span(f" - Missing: {missing_str}", style={"color": "#ff9800"}),
                ], style={"padding": "0.3rem 0"})
            )

        return items

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
    )
    def update_stats(table_data):
        """
        Update stats cards reactively when check-ins table data changes.
        Also shows/hides the needs-attention section.
        """
        if not table_data:
            hidden_style = {"display": "none"}
            return "0", "0", "0", "0", hidden_style

        total = len(table_data)
        ready = len([d for d in table_data if d.get("status") == "Ready"])
        pending = len([d for d in table_data if d.get("status") == "Pending"])

        # Count players needing attention (missing membership, payment, or start.gg)
        needs_attention = 0
        for row in table_data:
            missing_something = False
            # Check membership
            member_val = row.get("member") or row.get("medlem")
            if not member_val or str(member_val).lower() in ("false", "0", "no"):
                missing_something = True
            # Check payment
            payment_val = row.get("payment_valid") or row.get("swish_valid")
            if not payment_val or str(payment_val).lower() in ("false", "0", "no"):
                missing_something = True
            # Check start.gg
            startgg_val = row.get("startgg")
            if not startgg_val or str(startgg_val).lower() in ("false", "0", "no"):
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
    # TO Payment Approval - approve payment for selected player
    # -------------------------------------------------------------------------
    @app.callback(
        Output("payment-update-feedback", "children"),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("checkins-table", "active_cell"),
        State("checkins-table", "data"),
        State("event-dropdown", "value"),
        prevent_initial_call=True,
    )
    def approve_payment(active_cell, table_data, selected_slug):
        """
        When TO clicks on a cell in the 'payment_valid' column, toggle it.
        If payment_valid becomes True and member+startgg are OK, set status to Ready.
        """
        if not active_cell or not table_data:
            return no_update, no_update

        row_idx = active_cell.get("row")
        col_id = active_cell.get("column_id")

        # Only trigger on payment_valid column click
        if col_id != "payment_valid":
            return no_update, no_update

        if row_idx is None or row_idx >= len(table_data):
            return no_update, no_update

        row = table_data[row_idx]
        record_id = row.get("record_id")
        player_name = row.get("name") or row.get("tag") or "Unknown"

        if not record_id:
            return html.Span("❌ No record_id found for this player.", style={"color": "#ef4444"}), no_update

        # Toggle payment_valid
        current_payment = row.get("payment_valid")
        new_payment = not (current_payment is True or str(current_payment).lower() == "true")

        # Determine new status
        member_ok = row.get("member") is True or str(row.get("member", "")).lower() == "true"
        startgg_ok = row.get("startgg") is True or str(row.get("startgg", "")).lower() == "true"
        new_status = "Ready" if (new_payment and member_ok and startgg_ok) else "Pending"

        # Update Airtable
        airtable_url = f"https://api.airtable.com/v0/{BASE_ID}/{CHECKINS_TABLE}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        }
        patch_data = {
            "fields": {
                "payment_valid": new_payment,
                "status": new_status,
            }
        }

        try:
            resp = requests.patch(airtable_url, json=patch_data, headers=headers, timeout=15)
            resp.raise_for_status()

            # Update local table data for immediate feedback
            table_data[row_idx]["payment_valid"] = new_payment
            table_data[row_idx]["status"] = new_status

            status_emoji = "✅" if new_payment else "⏸️"
            feedback = html.Span(
                f"{status_emoji} {player_name}: payment_valid={new_payment}, status={new_status}",
                style={"color": "#10b981" if new_payment else "#f59e0b"}
            )
            return feedback, table_data

        except Exception as e:
            logger.exception(f"Failed to update payment for {player_name}: {e}")
            return html.Span(f"❌ Failed to update: {e}", style={"color": "#ef4444"}), no_update

    # -------------------------------------------------------------------------
    # Delete selected player from check-ins
    # -------------------------------------------------------------------------
    @app.callback(
        Output("delete-feedback", "children"),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("btn-delete-selected", "n_clicks"),
        State("checkins-table", "active_cell"),
        State("checkins-table", "data"),
        prevent_initial_call=True,
    )
    def delete_selected_player(n_clicks, active_cell, table_data):
        """
        Delete the selected player from Airtable when TO clicks delete button.
        """
        if not n_clicks or not active_cell or not table_data:
            return no_update, no_update

        row_idx = active_cell.get("row")
        if row_idx is None or row_idx >= len(table_data):
            return html.Span("⚠️ No row selected.", style={"color": "#f59e0b"}), no_update

        row = table_data[row_idx]
        record_id = row.get("record_id")
        player_name = row.get("name") or row.get("tag") or "Unknown"

        if not record_id:
            return html.Span("❌ No record_id found for this player.", style={"color": "#ef4444"}), no_update

        # Delete from Airtable
        airtable_url = f"https://api.airtable.com/v0/{BASE_ID}/{CHECKINS_TABLE}/{record_id}"
        headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

        try:
            resp = requests.delete(airtable_url, headers=headers, timeout=15)
            resp.raise_for_status()

            # Remove from local table data
            table_data = [r for i, r in enumerate(table_data) if i != row_idx]

            feedback = html.Span(
                f"🗑️ Deleted: {player_name}",
                style={"color": "#ef4444"}
            )
            return feedback, table_data

        except Exception as e:
            logger.exception(f"Failed to delete {player_name}: {e}")
            return html.Span(f"❌ Failed to delete: {e}", style={"color": "#ef4444"}), no_update

# callbacks.py
from dash.dependencies import Input, Output, State
from dash import no_update
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
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY")

def register_callbacks(app):
    """
    Register all Dash callbacks for:
    - Live check-ins table updates
    - Admin "Fetch Event Data" (Start.gg -> Airtable settings)
    - Populate game dropdown from Airtable (default_game) and expose events map
    """

    # ---------------------------------------------------------------------
    # Live check-ins table update
    # ---------------------------------------------------------------------
    @app.callback(
        Output("checkins-table", "data"),
        Output("checkins-table", "columns"),
        Input("event-dropdown", "value"),
        Input("interval-refresh", "n_intervals"),
    )
    def update_table(selected_slug, _):
        """
        Refresh the check-ins table when:
        - user selects a different event slug
        - the interval timer ticks
        """
        if not selected_slug:
            logger.warning("No event slug selected – skipping table update.")
            return no_update, no_update

        logger.info(f"Updating check-ins table for slug: {selected_slug}")
        try:
            data = get_checkins(selected_slug) or []
            if not isinstance(data, list) or not data:
                logger.info(f"No check-ins found for slug: {selected_slug}")
                return [], [{"name": "No participants", "id": "info"}]

            df = pd.DataFrame(data)
            cols = [{"name": str(c), "id": str(c)} for c in df.columns]
            return df.to_dict("records"), cols

        except Exception as e:
            logger.exception(f"Error fetching check-ins for slug '{selected_slug}': {e}")
            return [], [{"name": "Error fetching data", "id": "error"}]

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

        # 5) Patch Airtable settings
        patch_fields = {
            "active_event_slug": slug,
            "is_active": True,
            "event_date": start_iso,  # ISO YYYY-MM-DD
            "default_game": new_selected,  # Multi-select (array of strings)
            "events_json": json.dumps({"events": events_compact}, ensure_ascii=False),
            "startgg_event_url": link,
            "tournament_name": tournament.get("name"),
            "timezone": tournament.get("timezone"),
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

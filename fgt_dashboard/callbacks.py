# callbacks.py
from dash.dependencies import Input, Output, State
from dash import no_update, html, ctx, dcc
from shared.storage import (
    get_checkins,
    get_active_settings,
    get_active_slug,
    get_active_settings_with_id,
    update_settings,
    update_checkin,
    delete_checkin,
    get_audit_log,
)
import shared.storage as storage_api
import pandas as pd
import requests
import os
import logging
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta, date

logger = logging.getLogger(__name__)

# Storage backend + Start.gg API config
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000")

ACTION_META = {
    "auth_login_success": ("Auth", "Login Success"),
    "auth_login_denied": ("Auth", "Login Denied"),
    "auth_logout": ("Auth", "Logout"),
    "auth_select_active_event": ("Auth", "Select Active Event"),
    "admin_fetch_event_data": ("Settings", "Fetch Event Data"),
    "admin_update_requirements": ("Settings", "Update Requirements"),
    "admin_update_payment_settings": ("Settings", "Update Payment Settings"),
    "admin_toggle_field": ("Check-ins", "Toggle Field"),
    "admin_update_name": ("Check-ins", "Update Name"),
    "admin_update_tag": ("Check-ins", "Update Tag"),
    "admin_update_telephone": ("Check-ins", "Update Phone"),
    "admin_update_games": ("Check-ins", "Update Games"),
    "admin_manual_checkin": ("Check-ins", "Manual Check-in"),
    "admin_recheck_startgg": ("Check-ins", "Re-check Start.gg"),
    "admin_delete_checkin": ("Check-ins", "Delete Player"),
    "integration_result": ("Integrations", "Result"),
    "event_archived": ("Archive", "Event Archived"),
    "event_rearchived": ("Archive", "Event Re-Archived"),
    "event_reopened": ("Archive", "Event Reopened"),
    "event_deleted_from_history": ("Archive", "Deleted From History"),
}

ACTION_GROUP_ORDER = ["Auth", "Settings", "Check-ins", "Archive", "Integrations", "Other"]


def format_action_label(action: str) -> str:
    """Render friendly action names for the audit table."""
    if not action:
        return ""
    group, label = ACTION_META.get(action, ("Other", action.replace("_", " ").title()))
    return f"{group}: {label}"


def get_action_group(action: str) -> str:
    """Get audit action group for table category column."""
    if not action:
        return "Other"
    group, _ = ACTION_META.get(action, ("Other", action.replace("_", " ").title()))
    return group


def format_action_filter_label(action: str) -> str:
    """Render grouped labels for action dropdown options."""
    if not action:
        return ""
    group, label = ACTION_META.get(action, ("Other", action.replace("_", " ").title()))
    return f"[{group}] {label}"


def action_sort_key(action: str) -> tuple:
    """Sort actions by group, then by human-readable label."""
    group, label = ACTION_META.get(action, ("Other", action.replace("_", " ").title()))
    try:
        group_rank = ACTION_GROUP_ORDER.index(group)
    except ValueError:
        group_rank = len(ACTION_GROUP_ORDER)
    return (group_rank, label.lower())


def register_callbacks(app):
    """
    Register all Dash callbacks for:
    - Live check-ins table updates
    - Admin "Fetch Event Data" (Start.gg -> settings)
    - Populate game dropdown from settings (default_game) and expose events map
    """

    # ---------------------------------------------------------------------
    # Live check-ins table update (with column filtering, search, and quick filters)
    # ---------------------------------------------------------------------
    @app.callback(
        Output("checkins-table", "data"),
        Output("checkins-table", "columns"),
        Output("player-count", "children"),
        Output("active-event-coverage", "children"),
        Output("game-filter", "options"),
        Output("duplicate-warning", "style"),
        Output("duplicate-warning-text", "children"),
        Output("duplicate-warning-list", "children"),
        Input("event-dropdown", "value"),
        Input("interval-refresh", "n_intervals"),
        Input("btn-refresh", "n_clicks"),
        Input("sse-trigger", "data"),
        Input("visible-columns-store", "data"),
        Input("active-filter", "data"),
        Input("search-input", "value"),
        Input("game-filter", "value"),
        State("requirements-store", "data"),
    )
    def update_table(
        selected_slug,
        _interval,
        _clicks,
        _sse_trigger,
        visible_columns,
        active_filter,
        search_query,
        game_filter,
        requirements,
    ):
        """
        Refresh the check-ins table when:
        - user selects a different event slug
        - the interval timer ticks
        - visible columns change
        - filter or search changes
        """
        if not selected_slug:
            logger.warning("No event slug selected – skipping table update.")
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
            )

        # Default columns if none specified
        if not visible_columns:
            visible_columns = [
                "name",
                "tag",
                "telephone",
                "member",
                "startgg",
                "is_guest",
                "payment_valid",
                "status",
            ]

        # Hide columns for disabled requirements
        requirements = requirements or {}
        if requirements.get("require_membership") is not True:
            visible_columns = [c for c in visible_columns if c != "member"]
        if requirements.get("require_payment") is not True:
            visible_columns = [c for c in visible_columns if c != "payment_valid"]

        # Handle special "__ALL__" value for debugging
        is_all_events = selected_slug == "__ALL__"
        logger.info(
            f"Updating check-ins table for: {'ALL EVENTS' if is_all_events else selected_slug}"
        )

        try:
            if is_all_events:
                data = get_checkins(include_all=True) or []
            else:
                data = get_checkins(selected_slug) or []
            if not isinstance(data, list) or not data:
                logger.info(f"No check-ins found for slug: {selected_slug}")
                return (
                    [],
                    [{"name": "No participants", "id": "info"}],
                    "0 players",
                    "",
                    [],
                    {"display": "none"},
                    "",
                    [],
                )

            df = pd.DataFrame(data)
            total_count = len(df)
            coverage_text = ""

            try:
                settings = get_active_settings() or {}
                events_json = settings.get("events_json")
                registered_count = 0
                if isinstance(events_json, dict):
                    registered_count = int(events_json.get("tournament_entrants") or 0)
                elif isinstance(settings.get("startgg_registered_count"), (int, float, str)):
                    registered_count = int(settings.get("startgg_registered_count") or 0)
                if registered_count > 0:
                    coverage_rate = (total_count / registered_count) * 100
                    coverage_text = f"Start.gg coverage: {total_count}/{registered_count} ({coverage_rate:.0f}%)"
            except Exception:
                coverage_text = ""

            def _normalize_text(v):
                if v is None:
                    return ""
                txt = str(v).strip().lower()
                txt = re.sub(r"\s+", " ", txt)
                return txt

            def _normalize_phone(v):
                if v is None:
                    return ""
                return re.sub(r"\D+", "", str(v))

            # Duplicate warning: flag likely duplicates when at least 2 of 3 identity fields match.
            duplicate_warning_style = {"display": "none"}
            duplicate_warning_text = ""
            duplicate_warning_list = []
            identity_pairs = [
                ("name", "tag", "name + tag"),
                ("name", "telephone", "name + phone"),
                ("tag", "telephone", "tag + phone"),
            ]
            duplicate_hits = {}

            for col_a, col_b, reason in identity_pairs:
                if col_a not in df.columns or col_b not in df.columns:
                    continue

                buckets = {}
                for idx, row in df[[col_a, col_b]].iterrows():
                    a_raw = row.get(col_a)
                    b_raw = row.get(col_b)
                    a = _normalize_phone(a_raw) if col_a == "telephone" else _normalize_text(a_raw)
                    b = _normalize_phone(b_raw) if col_b == "telephone" else _normalize_text(b_raw)
                    if not a or not b:
                        continue
                    buckets.setdefault((a, b), []).append(idx)

                for idxs in buckets.values():
                    if len(idxs) < 2:
                        continue
                    for idx in idxs:
                        duplicate_hits.setdefault(idx, set()).add(reason)

            if duplicate_hits:
                flagged = df.loc[list(duplicate_hits.keys())]
                preview_rows = []
                for idx, row in flagged.head(4).iterrows():
                    display_name = str(row.get("name") or "Unknown")
                    display_tag = str(row.get("tag") or "-")
                    reasons = ", ".join(sorted(duplicate_hits.get(idx, [])))
                    preview_rows.append(html.Li(f"{display_name} ({display_tag}) – {reasons}"))
                duplicate_warning_style = {
                    "marginBottom": "0.75rem",
                    "display": "block",
                    "border": "1px solid rgba(245, 158, 11, 0.45)",
                    "backgroundColor": "rgba(245, 158, 11, 0.08)",
                    "borderRadius": "8px",
                    "padding": "0.55rem 0.75rem",
                }
                duplicate_warning_text = (
                    f"⚠ Possible duplicate participants detected ({len(flagged)})."
                )
                duplicate_warning_list = preview_rows

            # Game name shortening map
            GAME_SHORT_NAMES = {
                "STREET FIGHTER 6 TOURNAMENT": "SF6",
                "STREET FIGHTER 6": "SF6",
                "TEKKEN 8 TOURNAMENT": "T8",
                "TEKKEN 8": "T8",
                "SMASH SINGLES": "SSBU",
                "SUPER SMASH BROS": "SSBU",
                "SUPER SMASH BROS ULTIMATE": "SSBU",
            }

            GAME_SHORT_PATTERNS = [
                ("SUPER SMASH BROS", "SSBU"),
                ("SMASH", "SSBU"),
                ("TEKKEN 8", "T8"),
                ("STREET FIGHTER 6", "SF6"),
            ]

            def shorten_game(name):
                """Shorten game name using mapping, case-insensitive."""
                if not name:
                    return ""
                normalized = str(name).upper().strip()
                if normalized in GAME_SHORT_NAMES:
                    return GAME_SHORT_NAMES[normalized]
                for pattern, short in GAME_SHORT_PATTERNS:
                    if pattern in normalized:
                        return short
                return name

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
                    lambda x: (
                        ", ".join(shorten_game(g) for g in x)
                        if isinstance(x, list)
                        else shorten_game(x) if x else ""
                    )
                )

            # Store original boolean values for filtering before converting to icons
            for col in ["member", "startgg", "payment_valid", "is_guest"]:
                if col in df.columns:
                    df[f"_{col}_bool"] = df[col].apply(
                        lambda x: x is True or str(x).lower() == "true"
                    )

            # Convert booleans to icons ✓/✗
            for col in ["member", "startgg", "payment_valid", "is_guest"]:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: "✓" if x is True or str(x).lower() == "true" else "✗"
                    )

            # Apply quick filter
            # If payment is not required, disable the no-payment filter behavior.
            if active_filter == "no-payment" and requirements.get("require_payment") is not True:
                active_filter = "all"

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
                df = df[
                    df["tournament_games_registered"].str.contains(
                        game_filter, case=False, na=False
                    )
                ]

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
                    col_def = {
                        "name": header,
                        "id": str(c),
                        "reorderable": True,
                        "editable": c
                        in ["name", "tag", "telephone", "tournament_games_registered"],
                    }
                    cols.append(col_def)

            # Player count text
            if filtered_count == total_count:
                count_text = f"{total_count} players"
            else:
                count_text = f"{filtered_count} of {total_count} players"

            return (
                df_filtered.to_dict("records"),
                cols,
                count_text,
                coverage_text,
                game_options,
                duplicate_warning_style,
                duplicate_warning_text,
                duplicate_warning_list,
            )

        except Exception as e:
            logger.exception(f"Error fetching check-ins for slug '{selected_slug}': {e}")
            return (
                [],
                [{"name": "Error fetching data", "id": "error"}],
                "Error",
                "",
                [],
                {"display": "none"},
                "",
                [],
            )

    @app.callback(
        Output("duplicate-warning", "style", allow_duplicate=True),
        Input("duplicate-warning-dismiss", "n_clicks"),
        prevent_initial_call=True,
    )
    def dismiss_duplicate_warning(n_clicks):
        if n_clicks:
            return {"display": "none"}
        return no_update

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
        State("requirements-store", "data"),
        prevent_initial_call=True,
    )
    def update_active_filter(
        all_clicks, pending_clicks, ready_clicks, no_payment_clicks, requirements
    ):
        """Update the active filter based on which button was clicked."""
        triggered = ctx.triggered_id
        if triggered == "filter-pending":
            return "pending"
        elif triggered == "filter-ready":
            return "ready"
        elif triggered == "filter-no-payment":
            if (requirements or {}).get("require_payment") is not True:
                return "all"
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
        Input("requirements-store", "data"),
    )
    def update_stat_card_styles(active_filter, requirements):
        """Highlight active stat card filter with stronger visual state."""
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
            "position": "relative",
        }

        # Define colors for each card
        colors = {
            "all": "#00d4ff",  # accent_blue
            "ready": "#10b981",  # accent_green
            "pending": "#f59e0b",  # accent_yellow
            "no-payment": "#ef4444",  # accent_red
        }

        styles = {}
        for key in ["all", "ready", "pending", "no-payment"]:
            color = colors[key]
            if active_filter == key:
                # Active: stronger glow + lift + tinted surface
                styles[key] = {
                    **base_style,
                    "borderTop": f"3px solid {color}",
                    "border": "1px solid #1e293b",
                    "transform": "translateY(-3px) scale(1.03)",
                    "boxShadow": f"0 10px 26px {color}4d, 0 0 0 1px {color}4d",
                    "background": f"linear-gradient(180deg, {color}24 0%, #12121a 55%)",
                }
            else:
                # Inactive: original style, full brightness
                styles[key] = {
                    **base_style,
                    "borderTop": f"3px solid {color}",
                    "opacity": "0.95",
                }

        if (requirements or {}).get("require_payment") is not True:
            styles["no-payment"] = {
                **styles["no-payment"],
                "display": "none",
            }

        return styles["all"], styles["ready"], styles["pending"], styles["no-payment"]

    @app.callback(
        Output("manual-checkin-panel", "style"),
        Output("btn-toggle-manual-checkin", "children"),
        Input("btn-toggle-manual-checkin", "n_clicks"),
    )
    def toggle_manual_checkin_panel(n_clicks):
        expanded = bool(n_clicks and n_clicks % 2 == 1)
        if expanded:
            return {"display": "block", "marginTop": "0.6rem"}, "Manual Tools ▴"
        return {"display": "none", "marginTop": "0.6rem"}, "Manual Tools ▾"

    # ---------------------------------------------------------------------
    # Admin: Fetch event data from Start.gg and update settings
    # ---------------------------------------------------------------------
    @app.callback(
        Output("settings-output", "children"),
        Output("event-dropdown", "options"),
        Output("event-dropdown", "value"),
        Input("btn-fetch-event", "n_clicks"),
        State("input-startgg-link", "value"),
        State("auth-store", "data"),
    )
    def fetch_event_data(n_clicks, link, auth_state):
        """
        Admin action:
        1) Extract tournament slug from Start.gg URL
        2) Fetch tournament + events via GraphQL
        3) Update settings via storage facade with:
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
                events {
                  id
                  name
                  slug
                  startAt
                  entrants(query: {page: 1, perPage: 1}) {
                    pageInfo { total }
                  }
                }
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
                start_iso = (
                    datetime.fromtimestamp(int(tournament["startAt"]), tz=timezone.utc)
                    .date()
                    .isoformat()
                )
            except Exception:
                start_iso = None

        # Build compact events array
        events = tournament.get("events") or []
        events_compact = []
        for e in events:
            if not isinstance(e, dict):
                continue
            entrant_total = 0
            try:
                entrant_total = e["entrants"]["pageInfo"]["total"]
            except (KeyError, TypeError):
                pass
            events_compact.append({
                "id": e.get("id"),
                "name": e.get("name"),
                "slug": e.get("slug"),
                "startAt": e.get("startAt"),
                "numEntrants": entrant_total,
            })
        tournament_entrants = sum(ev.get("numEntrants", 0) for ev in events_compact)
        logger.info(f"Entrant count from events: {tournament_entrants} (across {len(events_compact)} events)")
        fetched_names = [e.get("name") for e in events if isinstance(e, dict) and e.get("name")]

        # 3) Find active settings row using storage backend
        settings_data = get_active_settings_with_id()
        if not settings_data:
            return "❌ No active settings record found.", no_update, no_update
        settings_id = settings_data["record_id"]
        current_fields = settings_data.get("fields", {}) or {}

        # 4) Preserve TO selection & merge with new games
        current_selected = current_fields.get("default_game") or []  # list[str]
        prev_names = set()
        try:
            prev_raw = current_fields.get("events_json")
            if prev_raw:
                parsed_prev = (
                    prev_raw if isinstance(prev_raw, (list, dict)) else json.loads(prev_raw)
                )
                prev_list = (
                    parsed_prev.get("events") if isinstance(parsed_prev, dict) else parsed_prev
                )
                if isinstance(prev_list, list):
                    prev_names = {
                        e.get("name") for e in prev_list if isinstance(e, dict) and e.get("name")
                    }
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

        # 5) Update settings using storage backend
        # Wrap events_json as {tournament_entrants, events} for no-show tracking.
        # Readers already handle both list and dict formats (backward compatible).
        events_json_value = {
            "tournament_entrants": tournament_entrants,
            "events": events_compact,
        }
        patch_fields = {
            "active_event_slug": slug,
            "event_display_name": tournament.get("name", ""),
            "is_active": True,
            "events_json": events_json_value,
            "event_date": start_iso,
            "default_game": new_selected,
            "startgg_event_url": link.strip(),
            "tournament_name": tournament.get("name", ""),
            "timezone": tournament.get("timezone", ""),
        }
        result = update_settings(settings_id, patch_fields)
        if not result:
            return "❌ Settings update failed", no_update, no_update

        try:
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "admin_fetch_event_data",
                "settings",
                target_event=slug,
                details=json.dumps(
                    {
                        "tournament_name": tournament.get("name", ""),
                        "events_found": len(events),
                    }
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for fetch event: {e}")

        logger.info(f"Updated settings for slug: {slug}")

        # Build new dropdown options with the new slug
        from shared.storage import get_all_event_slugs

        all_slugs = get_all_event_slugs() or []
        if slug not in all_slugs:
            all_slugs = [slug] + all_slugs

        # Use tournament name (with proper åäö) for the active slug
        tournament_name = tournament.get("name", "")
        dropdown_options = [
            {
                "label": (
                    tournament_name
                    if s == slug and tournament_name
                    else s.replace("-", " ").title()
                ),
                "value": s,
            }
            for s in all_slugs
        ]

        return f"✅ Updated {tournament_name} • {len(events)} events", dropdown_options, slug

    # ---------------------------------------------------------------------
    # Helper: read active settings.fields from storage backend
    # ---------------------------------------------------------------------
    def _get_active_settings_fields():
        result = get_active_settings_with_id()
        return result.get("fields") if result else None

    # ---------------------------------------------------------------------
    # Populate game-dropdown from settings (default_game) + mapping store
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
        Populate the game dropdown from settings (default_game multi-select).
        Also provide name->(id, slug) mapping through events-map-store (from events_json).
        """
        fields = _get_active_settings_fields()
        if not fields:
            return [], None, "⚠️ Could not read active settings.", []

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
                        for e in evs
                        if isinstance(e, dict)
                    ]
            except Exception:
                logger.warning("Failed to parse events_json")

        # 3) Default value: pick the first option if any
        value = options[0]["value"] if options else None

        # 4) Help text
        help_text = "Games populated from settings (default_game)."

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

        # Get active requirements (only enabled when explicitly True)
        requirements = requirements or {}
        require_membership = requirements.get("require_membership") is True
        require_payment = requirements.get("require_payment") is True
        require_startgg = requirements.get("require_startgg") is True

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
                needs_help.append(
                    {
                        "name": name,
                        "tag": tag,
                        "missing": missing,
                        "record_id": row.get("record_id", ""),
                    }
                )

        if not needs_help:
            return (
                html.P(
                    "✅ All players are ready!", style={"color": "#10b981", "fontWeight": "600"}
                ),
                "0",
            )

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
                html.Div(
                    [
                        # Player name with bullet point
                        html.Div(
                            [
                                html.Span(
                                    "•",
                                    style={
                                        "color": "#ef4444",
                                        "marginRight": "0.5rem",
                                        "fontSize": "1.2rem",
                                    },
                                ),
                                html.Span(
                                    display_name, style={"color": "#fff", "fontWeight": "500"}
                                ),
                            ],
                            style={
                                "display": "flex",
                                "alignItems": "center",
                                "marginBottom": "0.25rem",
                            },
                        ),
                        # Missing badges on second line, indented
                        html.Div(
                            missing_badges,
                            style={
                                "display": "flex",
                                "gap": "0.5rem",
                                "flexWrap": "wrap",
                                "marginLeft": "1rem",
                            },
                        ),
                    ],
                    style={
                        "padding": "0.75rem",
                        "marginBottom": "0.5rem",
                        "backgroundColor": "rgba(239, 68, 68, 0.05)",
                        "borderRadius": "8px",
                        "borderLeft": "3px solid #ef4444",
                    },
                )
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
        Output("tab-insights-content", "style"),
        Output("tab-settings-content", "style"),
        Input("tabs", "value"),
    )
    def switch_tabs(selected_tab):
        """
        Toggle visibility of tab content based on selected tab.
        """
        hidden = {"display": "none"}
        visible = {"display": "block"}
        if selected_tab == "tab-insights":
            return hidden, visible, hidden
        if selected_tab == "tab-settings":
            return hidden, hidden, visible
        else:
            return visible, hidden, hidden

    # -------------------------------------------------------------------------
    # Insights - load archived event options + summary KPIs
    # -------------------------------------------------------------------------
    @app.callback(
        Output("insights-series-dropdown", "options"),
        Output("insights-series-dropdown", "value"),
        Output("insights-event-dropdown", "options"),
        Output("insights-event-dropdown", "value"),
        Output("insights-summary-title", "children"),
        Output("insights-empty-hint", "children"),
        Output("insights-kpi-total", "children"),
        Output("insights-kpi-revenue", "children"),
        Output("insights-kpi-readyrate", "children"),
        Output("insights-kpi-memberrate", "children"),
        Output("insights-kpi-guestrate", "children"),
        Output("insights-kpi-startggrate", "children"),
        Output("insights-kpi-retention", "children"),
        Output("insights-kpi-total-delta", "children"),
        Output("insights-kpi-revenue-delta", "children"),
        Output("insights-kpi-readyrate-delta", "children"),
        Output("insights-kpi-memberrate-delta", "children"),
        Output("insights-kpi-guestrate-delta", "children"),
        Output("insights-kpi-startggrate-delta", "children"),
        Output("insights-kpi-retention-delta", "children"),
        Output("insights-top-game", "children"),
        Output("insights-top-players-title", "children"),
        Output("insights-top-players-table", "data"),
        Output("insights-games-title", "children"),
        Output("insights-games-table", "data"),
        Output("insights-events-table", "data"),
        Output("insights-earnings-table", "data"),
        Input("tabs", "value"),
        Input("btn-insights-refresh", "n_clicks"),
        Input("insights-event-dropdown", "value"),
        Input("insights-period-dropdown", "value"),
        Input("insights-series-dropdown", "value"),
        Input("insights-date-range", "start_date"),
        Input("insights-date-range", "end_date"),
    )
    def update_insights(
        selected_tab,
        _refresh_clicks,
        selected_event_slugs,
        selected_period,
        selected_series,
        custom_start_date,
        custom_end_date,
    ):
        if selected_tab != "tab-insights":
            return (no_update,) * 27

        def _empty_response(hint: str = "", summary: str = "Insights overview"):
            return (
                [],
                [],
                [],
                [],
                summary,
                hint,
                "0",
                "0 kr",
                "0%",
                "0%",
                "0%",
                "0%",
                "0%",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "Most popular game: -",
                "Top attendees",
                [],
                "Most played games",
                [],
                [],
                [],
            )

        try:
            history_fn = getattr(storage_api, "get_event_history_dashboard", None)
            if history_fn:
                events = history_fn() or []
            else:
                events = storage_api.get_event_history() or []
        except Exception as e:
            logger.exception(f"Failed to load insights data: {e}")
            return _empty_response(
                "Could not load insights. Try refreshing.", "Insights unavailable"
            )

        if not events:
            return _empty_response(
                "No archived events yet. Archive your first event to unlock insights."
            )

        all_events = list(events)

        selected_period = selected_period or "month"

        def _as_float(v):
            try:
                return float(v or 0)
            except Exception:
                return 0.0

        def _as_int(v):
            try:
                return int(v or 0)
            except Exception:
                return 0

        def _as_date(v):
            if not v:
                return None
            if isinstance(v, datetime):
                return v.date()
            if isinstance(v, date):
                return v
            txt = str(v).strip()
            if not txt:
                return None
            txt = txt.split("T")[0]
            try:
                return datetime.fromisoformat(txt).date()
            except Exception:
                return None

        today = datetime.now(timezone.utc).date()
        range_start = None
        range_end = None

        if selected_period == "day":
            range_start = today - timedelta(days=1)
            range_end = today
        elif selected_period == "week":
            range_start = today - timedelta(days=7)
            range_end = today
        elif selected_period == "month":
            range_start = today - timedelta(days=30)
            range_end = today
        elif selected_period == "quarter":
            range_start = today - timedelta(days=90)
            range_end = today
        elif selected_period == "year":
            range_start = today - timedelta(days=365)
            range_end = today
        elif selected_period == "custom":
            range_start = _as_date(custom_start_date)
            range_end = _as_date(custom_end_date)

        filtered_events = []
        for ev in events:
            ev_date = _as_date(ev.get("event_date"))
            if selected_period == "all":
                filtered_events.append(ev)
                continue

            # For ranged filters, include only events with parseable date in range.
            if not ev_date:
                continue
            if range_start and ev_date < range_start:
                continue
            if range_end and ev_date > range_end:
                continue
            filtered_events.append(ev)

        events = filtered_events

        if not events:
            label_map = {
                "day": "last 24h",
                "week": "last 7 days",
                "month": "last 30 days",
                "quarter": "last 90 days",
                "year": "last 365 days",
                "custom": "selected range",
                "all": "all time",
            }
            return _empty_response(
                f"No archived events in {label_map.get(selected_period, 'selected period')}. Try widening date range or clearing filters.",
            )

        def _event_title(ev):
            return (ev.get("event_display_name") or ev.get("event_slug") or "").strip()

        def _series_key(ev):
            title = _event_title(ev)
            if not title:
                return "Other"
            # Remove trailing numbering patterns: "#12", "12", "- 12"
            cleaned = re.sub(r"\s*[-#]?\s*\d+\s*$", "", title).strip(" -#")
            return cleaned or title

        selected_series = selected_series or []

        # Build series options from events in current period.
        series_values = sorted({_series_key(ev) for ev in events if _series_key(ev)})
        series_options = [{"label": s, "value": s} for s in series_values]
        selected_series = [s for s in selected_series if s in set(series_values)]

        series_scoped_events = [
            ev for ev in events if (not selected_series) or (_series_key(ev) in selected_series)
        ]

        options = []
        for ev in series_scoped_events:
            slug = ev.get("event_slug")
            if not slug:
                continue
            name = ev.get("event_display_name") or slug.replace("-", " ").title()
            date = ev.get("event_date") or ""
            label = f"{name} ({date})" if date else name
            options.append({"label": label, "value": slug})

        if not options:
            empty = _empty_response("No archived events yet")
            return (series_options, selected_series) + empty[2:]

        available_slugs = {o["value"] for o in options}
        selected_event_slugs = [s for s in (selected_event_slugs or []) if s in available_slugs]

        # Empty selection means "all events in selected period"
        selected_events = [
            ev
            for ev in series_scoped_events
            if (not selected_event_slugs) or ev.get("event_slug") in selected_event_slugs
        ]

        def _aggregate_metrics(event_rows):
            total = 0
            total_revenue = 0.0
            member_count = 0
            guest_count = 0
            startgg_count = 0
            ready_count = 0
            weighted_retention_sum = 0.0
            weighted_retention_den = 0
            top_game_counts = {}
            startgg_registered_total = 0
            checked_in_total = 0
            no_show_total = 0

            for ev in event_rows:
                ev_total = _as_int(ev.get("total_participants") or ev.get("participants"))
                total += ev_total
                total_revenue += _as_float(ev.get("total_revenue"))
                member_count += _as_int(ev.get("member_count"))
                guest_count += _as_int(ev.get("guest_count"))
                startgg_count += _as_int(ev.get("startgg_count"))

                status_breakdown = ev.get("status_breakdown")
                if isinstance(status_breakdown, dict):
                    ready_count += _as_int(status_breakdown.get("Ready"))

                retention_val = _as_float(ev.get("retention_rate"))
                if ev_total > 0:
                    weighted_retention_sum += retention_val * ev_total
                    weighted_retention_den += ev_total

                top_game = ev.get("most_popular_game")
                if top_game:
                    top_game_counts[top_game] = top_game_counts.get(top_game, 0) + 1

                # No-show aggregation
                startgg_registered_total += _as_int(ev.get("startgg_registered_count"))
                checked_in_total += _as_int(ev.get("checked_in_count"))
                no_show_total += _as_int(ev.get("no_show_count"))

            no_show_rate_agg = (
                (no_show_total / startgg_registered_total * 100)
                if startgg_registered_total > 0
                else 0.0
            )

            return {
                "total": total,
                "revenue": total_revenue,
                "member_count": member_count,
                "guest_count": guest_count,
                "startgg_count": startgg_count,
                "ready_count": ready_count,
                "retention": (
                    (weighted_retention_sum / weighted_retention_den)
                    if weighted_retention_den > 0
                    else 0.0
                ),
                "top_game_counts": top_game_counts,
                "startgg_registered_total": startgg_registered_total,
                "checked_in_total": checked_in_total,
                "no_show_total": no_show_total,
                "no_show_rate": no_show_rate_agg,
            }

        table_rows = []
        earnings_rows = []
        for ev in selected_events:
            ev_total = _as_int(ev.get("total_participants") or ev.get("participants"))
            ev_member_rate = (
                (_as_int(ev.get("member_count")) / ev_total * 100) if ev_total > 0 else 0.0
            )
            ev_startgg_rate = (
                (_as_int(ev.get("startgg_count")) / ev_total * 100) if ev_total > 0 else 0.0
            )
            ev_revenue = _as_float(ev.get("total_revenue"))
            revenue_per_player = (ev_revenue / ev_total) if ev_total > 0 else 0.0
            ev_no_show = _as_int(ev.get("no_show_count"))
            ev_no_show_rate = _as_float(ev.get("no_show_rate"))
            table_rows.append(
                {
                    "event_display_name": ev.get("event_display_name") or ev.get("event_slug", ""),
                    "event_slug": ev.get("event_slug", ""),
                    "event_date": ev.get("event_date") or "",
                    "total_participants": ev_total,
                    "checked_in_vs_registered": (
                        f"{_as_int(ev.get('checked_in_count'))}/{_as_int(ev.get('startgg_registered_count'))}"
                        if _as_int(ev.get("startgg_registered_count")) > 0
                        else "-"
                    ),
                    "total_revenue": f"{ev_revenue:.0f} kr",
                    "member_rate": f"{ev_member_rate:.0f}%",
                    "startgg_rate": f"{ev_startgg_rate:.0f}%",
                    "retention_rate": f"{_as_float(ev.get('retention_rate')):.0f}%",
                    "no_show_count": ev_no_show,
                    "no_show_rate": f"{ev_no_show_rate:.0f}%" if ev_no_show_rate > 0 else "-",
                }
            )
            earnings_rows.append(
                {
                    "event_display_name": ev.get("event_display_name") or ev.get("event_slug", ""),
                    "event_date": ev.get("event_date") or "",
                    "total_participants": ev_total,
                    "total_revenue": f"{ev_revenue:.0f} kr",
                    "revenue_per_player": f"{revenue_per_player:.0f} kr",
                }
            )

        if not selected_events:
            empty = _empty_response("No events selected")
            return (series_options, selected_series, options, []) + empty[4:]

        metrics = _aggregate_metrics(selected_events)

        ready_rate = (
            (metrics["ready_count"] / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
        )
        member_rate = (
            (metrics["member_count"] / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
        )
        guest_rate = (
            (metrics["guest_count"] / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
        )
        startgg_rate = (
            (metrics["startgg_count"] / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
        )
        retention = metrics["retention"]

        if metrics["top_game_counts"]:
            top_game = max(metrics["top_game_counts"], key=metrics["top_game_counts"].get)
            top_game_text = f"Most popular game: {top_game}"
        else:
            top_game_text = ""

        def _fmt_delta(curr, prev, unit="count"):
            if prev is None:
                return "—"
            diff = curr - prev
            if diff > 0:
                arrow = "↑"
            elif diff < 0:
                arrow = "↓"
            else:
                arrow = "→"

            if unit == "kr":
                return f"{arrow} {diff:+.0f} kr vs prev"
            if unit == "pp":
                return f"{arrow} {diff:+.1f} pp vs prev"
            return f"{arrow} {int(diff):+d} vs prev"

        # Delta against previous period (same length), disabled for all-time and specific-event selection.
        prev_metrics = None
        if selected_period != "all" and not selected_event_slugs and range_start and range_end:
            period_days = (range_end - range_start).days + 1
            prev_end = range_start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=period_days - 1)

            prev_events = []
            for ev in all_events:
                ev_date = _as_date(ev.get("event_date"))
                if not ev_date:
                    continue
                if ev_date < prev_start or ev_date > prev_end:
                    continue
                if selected_series and (_series_key(ev) not in selected_series):
                    continue
                prev_events.append(ev)

            if prev_events:
                prev_metrics = _aggregate_metrics(prev_events)

        total_delta = _fmt_delta(
            metrics["total"], prev_metrics["total"] if prev_metrics else None, "count"
        )
        revenue_delta = _fmt_delta(
            metrics["revenue"], prev_metrics["revenue"] if prev_metrics else None, "kr"
        )
        ready_delta = _fmt_delta(
            ready_rate,
            (
                (prev_metrics["ready_count"] / prev_metrics["total"] * 100)
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "pp",
        )
        member_delta = _fmt_delta(
            member_rate,
            (
                (prev_metrics["member_count"] / prev_metrics["total"] * 100)
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "pp",
        )
        guest_delta = _fmt_delta(
            guest_rate,
            (
                (prev_metrics["guest_count"] / prev_metrics["total"] * 100)
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "pp",
        )
        startgg_delta = _fmt_delta(
            startgg_rate,
            (
                (prev_metrics["startgg_count"] / prev_metrics["total"] * 100)
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "pp",
        )
        retention_delta = _fmt_delta(
            retention, prev_metrics["retention"] if prev_metrics else None, "pp"
        )

        period_label = {
            "day": "Last 24h",
            "week": "Last 7 days",
            "month": "Last 30 days",
            "quarter": "Last 90 days",
            "year": "Last 365 days",
            "custom": "Custom range",
            "all": "All time",
        }.get(selected_period, "Selected period")

        if selected_event_slugs:
            scope_label = f"{len(selected_events)} selected events"
        elif selected_series:
            scope_label = f"All events in {', '.join(selected_series)}"
        else:
            scope_label = "All events"
        summary_title = f"{scope_label} • {period_label}"

        # Community-friendly heads-up text for no-show trend (non-alarm tone).
        heads_up_text = ""
        reg_total = metrics.get("startgg_registered_total", 0)
        checked_in_total = metrics.get("checked_in_total", 0)
        no_show_total = metrics.get("no_show_total", 0)
        no_show_rate = metrics.get("no_show_rate", 0.0)
        if reg_total > 0:
            checked_rate = (checked_in_total / reg_total) * 100
            heads_up_text = (
                f"Checked-in coverage: {checked_in_total}/{reg_total} ({checked_rate:.0f}%)."
            )
            if reg_total >= 10:
                if no_show_rate >= 30:
                    heads_up_text += (
                        f" Heads-up: no-show is {no_show_rate:.0f}% "
                        f"({no_show_total} of {reg_total} Start.gg registrations) in this scope."
                    )
                elif no_show_rate >= 15:
                    heads_up_text += (
                        f" Heads-up: no-show is {no_show_rate:.0f}% "
                        f"({no_show_total} of {reg_total}) in this scope."
                    )

        # Top attendees leaderboard for selected scope
        top_players_rows = []
        top_players_title = "Top attendees"
        try:
            top_players_fn = getattr(storage_api, "get_top_players_history", None)
            if top_players_fn:
                selected_scope_slugs = [
                    ev.get("event_slug") for ev in selected_events if ev.get("event_slug")
                ]
                top_players_rows = (
                    top_players_fn(
                        event_slugs=selected_scope_slugs,
                        start_date=range_start.isoformat() if range_start else None,
                        end_date=range_end.isoformat() if range_end else None,
                        limit=15,
                    )
                    or []
                )
                top_players_title = f"Top attendees ({len(top_players_rows)} shown)"
        except Exception as e:
            logger.warning(f"Failed to load top players leaderboard: {e}")

        # Game popularity leaderboard for selected scope
        game_counts = {}
        total_entries = 0
        for ev in selected_events:
            breakdown = ev.get("games_breakdown")
            if not isinstance(breakdown, dict):
                continue
            for game, count in breakdown.items():
                cnt = _as_int(count)
                if cnt <= 0:
                    continue
                game_counts[game] = game_counts.get(game, 0) + cnt
                total_entries += cnt

        sorted_games = sorted(game_counts.items(), key=lambda kv: (-kv[1], str(kv[0]).lower()))
        games_rows = []
        for idx, (game, cnt) in enumerate(sorted_games[:20], start=1):
            share = (cnt / total_entries * 100) if total_entries > 0 else 0.0
            games_rows.append(
                {
                    "rank": idx,
                    "game": game,
                    "entries": cnt,
                    "share": f"{share:.0f}%",
                }
            )
        games_title = f"Most played games ({len(games_rows)} shown)"

        return (
            series_options,
            selected_series,
            options,
            selected_event_slugs,
            summary_title,
            heads_up_text,
            str(metrics["total"]),
            f"{metrics['revenue']:.0f} kr",
            f"{ready_rate:.0f}%",
            f"{member_rate:.0f}%",
            f"{guest_rate:.0f}%",
            f"{startgg_rate:.0f}%",
            f"{retention:.0f}%",
            total_delta,
            revenue_delta,
            ready_delta,
            member_delta,
            guest_delta,
            startgg_delta,
            retention_delta,
            top_game_text,
            top_players_title,
            top_players_rows,
            games_title,
            games_rows,
            table_rows,
            earnings_rows,
        )

    @app.callback(
        Output("insights-custom-range-wrap", "style"),
        Input("insights-period-dropdown", "value"),
    )
    def toggle_insights_custom_range(selected_period):
        if selected_period == "custom":
            return {"display": "block", "minWidth": "320px"}
        return {"display": "none"}

    @app.callback(
        Output("insights-view-players", "style"),
        Output("insights-view-games", "style"),
        Output("insights-view-events", "style"),
        Output("insights-view-earnings", "style"),
        Output("insights-top-game", "style"),
        Input("insights-subtabs", "value"),
    )
    def toggle_insights_focus_view(view_mode):
        base_visible = {"display": "block"}
        hidden = {"display": "none"}
        top_game_visible = {"color": "#94a3b8", "marginBottom": "1rem"}
        mode = (view_mode or "").strip().lower()

        if mode == "players":
            return base_visible, hidden, hidden, hidden, hidden
        if mode == "games":
            return hidden, base_visible, hidden, hidden, top_game_visible
        if mode == "events":
            return hidden, hidden, base_visible, hidden, top_game_visible
        if mode == "earnings":
            return hidden, hidden, hidden, base_visible, hidden
        return base_visible, hidden, hidden, hidden, hidden

    @app.callback(
        Output("insights-kpi-help", "children"),
        Input("insights-card-total", "n_clicks"),
        Input("insights-card-ready", "n_clicks"),
        Input("insights-card-member", "n_clicks"),
        Input("insights-card-guest", "n_clicks"),
        Input("insights-card-startgg", "n_clicks"),
        Input("insights-card-retention", "n_clicks"),
        Input("insights-card-revenue", "n_clicks"),
    )
    def show_kpi_help(_total, _ready, _member, _guest, _startgg, _retention, _revenue):
        help_map = {
            "insights-card-total": "Participants: total participant entries in selected period/scope.",
            "insights-card-ready": "Ready Rate: ready participants divided by total participants at archive time.",
            "insights-card-member": "Member Rate: members divided by total participants.",
            "insights-card-guest": "Guest Share: guests divided by total participants.",
            "insights-card-startgg": "Start.gg Rate: Start.gg-verified participants divided by total participants.",
            "insights-card-retention": "Retention: returning-player share, weighted by event size.",
            "insights-card-revenue": "Total Revenue: summed event revenue in selected scope.",
        }
        triggered = ctx.triggered_id
        if triggered in help_map:
            return help_map[triggered]
        return "Tip: click a KPI card to see how it is calculated."

    @app.callback(
        Output("insights-download", "data"),
        Input("btn-insights-export-csv", "n_clicks"),
        State("insights-subtabs", "value"),
        State("insights-top-players-table", "data"),
        State("insights-games-table", "data"),
        State("insights-events-table", "data"),
        State("insights-earnings-table", "data"),
        prevent_initial_call=True,
    )
    def export_insights_csv(n_clicks, subtab, players_data, games_data, events_data, earnings_data):
        if not n_clicks:
            return no_update

        mode = (subtab or "players").strip().lower()
        data_map = {
            "players": players_data or [],
            "games": games_data or [],
            "events": events_data or [],
            "earnings": earnings_data or [],
        }
        rows = data_map.get(mode, [])
        if not rows:
            return no_update

        df = pd.DataFrame(rows)
        filename = f"insights_{mode}.csv"
        return dcc.send_data_frame(df.to_csv, filename, index=False)

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

        # Get active requirements (only enabled when explicitly True)
        requirements = requirements or {}
        require_membership = requirements.get("require_membership") is True
        require_payment = requirements.get("require_payment") is True
        require_startgg = requirements.get("require_startgg") is True

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
        Output("payment-update-feedback", "children", allow_duplicate=True),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("checkins-table", "active_cell"),
        State("checkins-table", "data"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def toggle_field(active_cell, table_data, selected_slug, auth_state):
        """
        When TO clicks on a cell in toggleable columns (payment_valid, startgg, is_guest), toggle it.
        If all required fields are OK, set status to Ready.
        """
        if not active_cell or not table_data:
            return no_update, no_update

        row_idx = active_cell.get("row")
        col_id = active_cell.get("column_id")

        # Only trigger on toggleable columns (is_guest is set automatically by system)
        # member is toggled manually or set by eBas Register flow
        toggleable_columns = ["payment_valid", "startgg", "member"]
        if col_id not in toggleable_columns:
            return no_update, no_update

        if row_idx is None or row_idx >= len(table_data):
            return no_update, no_update

        row = table_data[row_idx]
        record_id = row.get("record_id")
        player_name = row.get("name") or row.get("tag") or "Unknown"

        if not record_id:
            return (
                html.Span("❌ No record_id found for this player.", style={"color": "#ef4444"}),
                no_update,
            )

        # Helper to check if value is truthy
        def is_checked(val):
            return val == "✓" or val is True or str(val).lower() == "true"

        # Toggle the clicked field
        current_val = row.get(col_id)
        new_val = not is_checked(current_val)

        # Build update dict
        update_data = {col_id: new_val}

        # If TO manually marks Start.gg as approved, classify as guest flow.
        # (Matched Start.gg players are handled by integration_result logic.)
        if col_id == "startgg" and new_val:
            update_data["is_guest"] = True

        # Fetch configurable requirements from settings
        # Use "is True" so that missing/None = requirement OFF
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

        # Update database
        result = update_checkin(record_id, update_data)

        if result:
            # Update local table data for immediate feedback
            table_data[row_idx][col_id] = "✓" if new_val else "✗"
            if "is_guest" in update_data:
                table_data[row_idx]["is_guest"] = "✓" if bool(update_data["is_guest"]) else "✗"
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
            col_labels = {
                "payment_valid": "Payment",
                "startgg": "Start.gg",
                "member": "Member",
                "is_guest": "Guest",
            }
            col_label = col_labels.get(col_id, col_id)
            status_emoji = "✅" if new_val else "⏸️"

            feedback = html.Span(
                f"{status_emoji} {player_name}: {col_label} {'✓' if new_val else '✗'}, status={new_status}",
                style={"color": "#10b981" if new_val else "#f59e0b"},
            )

            # Audit log for manual admin toggle
            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_toggle_field",
                    "active_event_data",
                    target_event=selected_slug or "",
                    target_record=record_id,
                    target_player=player_name,
                    details=json.dumps(
                        {
                            "field": col_id,
                            "old": bool(is_checked(current_val)),
                            "new": bool(new_val),
                            "status": new_status,
                        }
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for toggle: {e}")

            return feedback, table_data
        else:
            logger.error(f"Failed to update {col_id} for {player_name}")
            return html.Span(f"❌ Failed to update {col_id}", style={"color": "#ef4444"}), no_update

    @app.callback(
        Output("payment-update-feedback", "children", allow_duplicate=True),
        Output("checkins-table", "data", allow_duplicate=True),
        Input("checkins-table", "data_timestamp"),
        State("checkins-table", "data"),
        State("checkins-table", "data_previous"),
        State("checkins-table", "active_cell"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def save_tag_edits(
        _timestamp, table_data, previous_data, active_cell, selected_slug, auth_state
    ):
        """Persist inline edits (name/tag/phone/games) from the check-ins table."""
        if not table_data or not previous_data:
            return no_update, no_update

        editable_columns = ["name", "tag", "telephone", "tournament_games_registered"]
        if not active_cell or active_cell.get("column_id") not in editable_columns:
            return no_update, no_update

        edited_column = active_cell.get("column_id")

        prev_by_id = {
            str(r.get("record_id")): r
            for r in previous_data
            if isinstance(r, dict) and r.get("record_id")
        }
        curr_by_id = {
            str(r.get("record_id")): r
            for r in table_data
            if isinstance(r, dict) and r.get("record_id")
        }

        if not prev_by_id or not curr_by_id:
            return no_update, no_update

        settings = get_active_settings() or {}
        per_game = int(settings.get("swish_expected_per_game") or 0)

        def _clean_phone(value):
            txt = str(value or "").strip()
            return re.sub(r"\s+", "", txt)

        def _parse_games(value):
            if value is None:
                return []
            if isinstance(value, list):
                raw = value
            else:
                raw = re.split(r"[,;\n]+", str(value))
            out = []
            seen = set()
            for item in raw:
                g = str(item or "").strip()
                if not g:
                    continue
                key = g.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(g)
            return out

        changed_rows = []
        for record_id, curr_row in curr_by_id.items():
            prev_row = prev_by_id.get(record_id)
            if not prev_row:
                continue
            old_val = prev_row.get(edited_column)
            new_val = curr_row.get(edited_column)

            if edited_column == "telephone":
                old_cmp = _clean_phone(old_val)
                new_cmp = _clean_phone(new_val)
            elif edited_column == "tournament_games_registered":
                old_cmp = ", ".join(_parse_games(old_val)).lower()
                new_cmp = ", ".join(_parse_games(new_val)).lower()
            else:
                old_cmp = str(old_val or "").strip()
                new_cmp = str(new_val or "").strip()

            if old_cmp != new_cmp:
                changed_rows.append((record_id, old_val, new_val))

        if not changed_rows:
            return no_update, no_update

        updated = 0
        reverted = 0
        messages = []
        table_needs_refresh = False

        action_by_column = {
            "name": "admin_update_name",
            "tag": "admin_update_tag",
            "telephone": "admin_update_telephone",
            "tournament_games_registered": "admin_update_games",
        }
        label_by_column = {
            "name": "name",
            "tag": "tag",
            "telephone": "phone",
            "tournament_games_registered": "games",
        }

        for record_id, old_value, new_value in changed_rows:
            row = curr_by_id.get(record_id, {})
            player_name = row.get("name") or row.get("tag") or "Unknown"

            update_data = {}
            old_clean = old_value
            new_clean = new_value

            if edited_column in ["name", "tag"]:
                old_clean = str(old_value or "").strip()
                new_clean = str(new_value or "").strip()
                if not new_clean:
                    row[edited_column] = old_clean
                    reverted += 1
                    messages.append(
                        f"{player_name}: {label_by_column[edited_column]} cannot be empty"
                    )
                    table_needs_refresh = True
                    continue
                update_data[edited_column] = new_clean

            elif edited_column == "telephone":
                old_clean = _clean_phone(old_value)
                new_clean = _clean_phone(new_value)
                row[edited_column] = new_clean
                update_data[edited_column] = new_clean
                table_needs_refresh = True

            elif edited_column == "tournament_games_registered":
                old_games = _parse_games(old_value)
                new_games = _parse_games(new_value)
                old_clean = old_games
                new_clean = new_games
                row[edited_column] = ", ".join(new_games)
                update_data[edited_column] = new_games
                if per_game >= 0:
                    update_data["payment_expected"] = len(new_games) * per_game
                table_needs_refresh = True

            result = update_checkin(record_id, update_data, typecast=True)
            if not result:
                if edited_column in ["name", "tag", "telephone"]:
                    row[edited_column] = str(old_value or "").strip()
                elif edited_column == "tournament_games_registered":
                    row[edited_column] = ", ".join(_parse_games(old_value))
                reverted += 1
                messages.append(f"{player_name}: failed to save {label_by_column[edited_column]}")
                table_needs_refresh = True
                continue

            updated += 1

            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    action_by_column[edited_column],
                    "active_event_data",
                    target_event=selected_slug or "",
                    target_record=record_id,
                    target_player=player_name,
                    details=json.dumps(
                        {"field": edited_column, "old": old_clean, "new": new_clean}
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for inline edit: {e}")

        if updated and not reverted:
            if updated == 1:
                feedback = html.Span(
                    f"✅ {label_by_column[edited_column].title()} updated.",
                    style={"color": "#10b981"},
                )
            else:
                feedback = html.Span(
                    f"✅ Updated {updated} {label_by_column[edited_column]} values.",
                    style={"color": "#10b981"},
                )
        elif reverted and not updated:
            msg = "; ".join(messages[:2]) if messages else "No valid changes saved."
            feedback = html.Span(f"⚠️ {msg}", style={"color": "#f59e0b"})
        else:
            feedback = html.Span(
                f"✅ Updated {updated}, ⚠️ reverted {reverted}.", style={"color": "#f59e0b"}
            )

        return feedback, table_data if table_needs_refresh else no_update

    @app.callback(
        Output("manual-checkin-feedback", "children"),
        Output("input-manual-name", "value"),
        Output("input-manual-tag", "value"),
        Input("btn-manual-checkin", "n_clicks"),
        State("input-manual-name", "value"),
        State("input-manual-tag", "value"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def add_manual_checkin(n_clicks, input_name, input_tag, selected_slug, auth_state):
        """Manually add a missing Start.gg participant to active check-ins."""
        if not n_clicks:
            return no_update, no_update, no_update

        if not selected_slug or selected_slug == "__ALL__":
            return (
                html.Span("⚠️ Select a specific event first.", style={"color": "#f59e0b"}),
                no_update,
                no_update,
            )

        name = (input_name or "").strip()
        tag = (input_tag or "").strip()
        if not name and not tag:
            return (
                html.Span("⚠️ Enter at least name or tag.", style={"color": "#f59e0b"}),
                no_update,
                no_update,
            )

        if not name:
            name = tag

        settings = get_active_settings() or {}
        require_membership = settings.get("require_membership") is True
        require_payment = settings.get("require_payment") is True

        payload = {
            "name": name,
            "tag": tag,
            "startgg": True,
            "is_guest": False,
            "member": require_membership,
            "payment_valid": require_payment,
        }

        payment_ok = (not require_payment) or bool(payload["payment_valid"])
        member_ok = (not require_membership) or bool(payload["member"])
        startgg_ok = bool(payload["startgg"])
        payload["status"] = "Ready" if (payment_ok and member_ok and startgg_ok) else "Pending"

        begin_fn = getattr(storage_api, "begin_checkin", None)
        if not callable(begin_fn):
            return (
                html.Span(
                    "❌ Manual check-in is not available in current backend.",
                    style={"color": "#ef4444"},
                ),
                no_update,
                no_update,
            )

        try:
            result = begin_fn(selected_slug, payload)
            record_id = result.get("record_id") or result.get("checkin_id")
            created = bool(result.get("created"))

            try:
                requests.post(
                    "http://backend:8000/api/notify/update",
                    json={
                        "type": "manual_checkin",
                        "record_id": record_id,
                        "player_name": name,
                    },
                    timeout=2,
                )
            except Exception as e:
                logger.warning(f"Failed to broadcast manual check-in SSE: {e}")

            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_manual_checkin",
                    "active_event_data",
                    target_event=selected_slug,
                    target_record=record_id,
                    target_player=name,
                    details=json.dumps({"tag": tag, "created": created}),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for manual check-in: {e}")

            label = "added" if created else "updated"
            feedback = html.Span(f"✅ Manual check-in {label}: {name}", style={"color": "#10b981"})
            return feedback, "", ""
        except Exception as e:
            logger.exception(f"Manual check-in failed for '{name}': {e}")
            return (
                html.Span(f"❌ Manual check-in failed: {e}", style={"color": "#ef4444"}),
                no_update,
                no_update,
            )

    # -------------------------------------------------------------------------
    # Re-check Start.gg - re-validate registration + sync games for selected player
    # -------------------------------------------------------------------------
    @app.callback(
        Output("recheck-startgg-feedback", "children"),
        Input("btn-recheck-startgg", "n_clicks"),
        State("checkins-table", "selected_rows"),
        State("checkins-table", "data"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def recheck_startgg(n_clicks, selected_rows, table_data, selected_slug, auth_state):
        """Re-validate Start.gg registration and sync games for a selected player."""
        if not n_clicks:
            return no_update

        if not selected_rows or len(selected_rows) == 0:
            return html.Span(
                "Select a player row first.",
                style={"color": "#f59e0b"},
            )

        if len(selected_rows) > 1:
            return html.Span(
                "Select only one player at a time for re-check.",
                style={"color": "#f59e0b"},
            )

        row_idx = selected_rows[0]
        if not table_data or row_idx >= len(table_data):
            return no_update

        row = table_data[row_idx]
        record_id = row.get("record_id")
        player_name = row.get("name") or row.get("tag") or "Unknown"
        player_tag = row.get("tag") or ""

        if not record_id:
            return html.Span("No record_id for selected row.", style={"color": "#ef4444"})

        if not player_tag:
            return html.Span(
                f"{player_name}: no tag set — add a tag first.",
                style={"color": "#f59e0b"},
            )

        # Save previous state for audit
        prev_startgg = row.get("startgg", False)
        prev_games = row.get("tournament_games_registered", "")

        try:
            resp = requests.post(
                "http://backend:8000/api/admin/recheck-startgg",
                json={"record_id": record_id},
                timeout=20,
            )

            if resp.status_code >= 400:
                error_detail = (
                    resp.json().get("detail", resp.text[:200])
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else resp.text[:200]
                )
                return html.Span(
                    f"Re-check failed: {error_detail}",
                    style={"color": "#ef4444"},
                )

            data = resp.json()
            startgg = data.get("startgg", False)
            events = data.get("events", [])
            status = data.get("status", "?")
            events_str = ", ".join(events) if events else "none"

            # Audit log
            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_recheck_startgg",
                    "active_event_data",
                    target_event=selected_slug or "",
                    target_record=record_id,
                    target_player=player_name,
                    details=json.dumps(
                        {
                            "tag": player_tag,
                            "prev_startgg": prev_startgg,
                            "new_startgg": startgg,
                            "prev_games": prev_games,
                            "new_games": events,
                            "new_status": status,
                        }
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for recheck: {e}")

            if startgg:
                return html.Span(
                    f"Re-checked {player_name}: Start.gg verified, Games: {events_str}, Status: {status}",
                    style={"color": "#10b981"},
                )
            else:
                return html.Span(
                    f"Re-checked {player_name}: not found on Start.gg, Status: {status}",
                    style={"color": "#f59e0b"},
                )

        except requests.exceptions.Timeout:
            return html.Span(
                "Re-check timed out — Start.gg or n8n may be slow. Try again.",
                style={"color": "#ef4444"},
            )
        except Exception as e:
            logger.exception(f"Re-check Start.gg failed for '{player_name}': {e}")
            return html.Span(
                f"Re-check failed: {e}",
                style={"color": "#ef4444"},
            )

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
            return (
                False,
                "",
                (
                    html.Span("⚠️ No row selected.", style={"color": "#f59e0b"})
                    if n_clicks
                    else no_update
                ),
            )

        # Build list of names to delete
        names = []
        for row_idx in selected_rows:
            if row_idx is not None and row_idx < len(table_data):
                row = table_data[row_idx]
                player_name = row.get("name") or row.get("tag") or "Unknown"
                player_tag = row.get("tag") or ""
                display_name = (
                    f"{player_name} ({player_tag})"
                    if player_tag and player_tag != player_name
                    else player_name
                )
                names.append(display_name)

        if not names:
            return False, "", html.Span("⚠️ No row selected.", style={"color": "#f59e0b"})

        if len(names) == 1:
            message = (
                f"Are you sure you want to delete {names[0]}?\n\nThis action cannot be undone."
            )
        else:
            message = (
                f"Are you sure you want to delete {len(names)} players?\n\n• "
                + "\n• ".join(names)
                + "\n\nThis action cannot be undone."
            )

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
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def delete_selected_player(
        submit_n_clicks, selected_rows, table_data, selected_slug, auth_state
    ):
        """Delete the selected player(s) after confirmation."""
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

            # Delete from database
            if delete_checkin(record_id):
                deleted_names.append(player_name)
                rows_to_remove.add(row_idx)

                # Audit log for manual admin deletion
                try:
                    storage_api.log_action(
                        {
                            "user_id": (auth_state or {}).get("user_id", ""),
                            "user_name": (auth_state or {}).get("user_name", "system"),
                            "user_email": (auth_state or {}).get("user_email", ""),
                        },
                        "admin_delete_checkin",
                        "active_event_data",
                        target_event=selected_slug or "",
                        target_record=record_id,
                        target_player=player_name,
                    )
                except Exception as e:
                    logger.warning(f"Failed to write audit log for delete: {e}")
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
                feedback = html.Span(
                    f"🗑️ Deleted {len(deleted_names)} players", style={"color": "#ef4444"}
                )
        elif failed_names and not deleted_names:
            feedback = html.Span(
                f"❌ Failed to delete: {', '.join(failed_names)}", style={"color": "#ef4444"}
            )
        elif deleted_names and failed_names:
            feedback = html.Span(
                f"🗑️ Deleted {len(deleted_names)}, ❌ Failed: {len(failed_names)}",
                style={"color": "#f59e0b"},
            )
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
            return no_update, html.Span("⚠️ No guests found to export.", style={"color": "#f59e0b"})

        # Build export data grouped by game
        # Players registered for multiple games are duplicated to each game section
        export_data = []
        for g in guests:
            tag = g.get("tag", "")
            games_raw = g.get("tournament_games_registered", "")

            # Parse games (could be string "Game1, Game2" or list ["Game1", "Game2"])
            if isinstance(games_raw, list):
                games = games_raw
            elif isinstance(games_raw, str) and games_raw:
                games = [game.strip() for game in games_raw.split(",")]
            else:
                games = ["Unknown"]

            # Add one row per game for this player
            for game in games:
                if game:  # Skip empty
                    export_data.append(
                        {
                            "Tag": tag,
                            "Game": game,
                        }
                    )

        # Sort by Game so all players per game are grouped together
        df = pd.DataFrame(export_data)
        if not df.empty:
            df = df.sort_values(by="Game", key=lambda x: x.str.upper())

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"guests_export_{timestamp}.csv"

        # Count unique guests vs total rows (some duplicated across games)
        unique_guests = len(guests)
        total_rows = len(df)

        feedback = html.Span(
            f"✅ Exported {unique_guests} guests ({total_rows} entries across games)",
            style={"color": "#10b981"},
        )

        return dict(content=df.to_csv(index=False), filename=filename), feedback

    # -------------------------------------------------------------------------
    # Save Check-in Requirements - update settings
    # -------------------------------------------------------------------------
    @app.callback(
        Output("requirements-save-feedback", "children"),
        Output("requirements-store", "data"),
        Input("btn-save-requirements", "n_clicks"),
        State("require-payment-toggle", "value"),
        State("require-membership-toggle", "value"),
        State("require-startgg-toggle", "value"),
        State("offer-membership-toggle", "value"),
        State("requirements-store", "data"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def save_requirements(
        n_clicks,
        req_payment,
        req_membership,
        req_startgg,
        offer_membership,
        current_store,
        auth_state,
    ):
        """Save check-in requirement settings to storage backend."""
        if not n_clicks:
            return no_update, no_update

        # Get the active settings record ID
        settings_data = get_active_settings_with_id()
        if not settings_data:
            return html.Span("❌ No active settings found", style={"color": "#ef4444"}), no_update

        record_id = settings_data.get("record_id")
        if not record_id:
            return (
                html.Span("❌ Could not find settings record", style={"color": "#ef4444"}),
                no_update,
            )

        # Prepare update data (checklist returns [True] if checked, [] if not)
        update_data = {
            "require_payment": bool(req_payment),
            "require_membership": bool(req_membership),
            "require_startgg": bool(req_startgg),
            "offer_membership": bool(offer_membership),
        }

        # Update settings
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

            try:
                previous = current_store or {}
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_update_requirements",
                    "settings",
                    target_event=get_active_slug() or "",
                    details=json.dumps(
                        {
                            "require_payment": {
                                "old": bool(previous.get("require_payment")),
                                "new": bool(req_payment),
                            },
                            "require_membership": {
                                "old": bool(previous.get("require_membership")),
                                "new": bool(req_membership),
                            },
                            "require_startgg": {
                                "old": bool(previous.get("require_startgg")),
                                "new": bool(req_startgg),
                            },
                            "offer_membership": {
                                "old": bool(
                                    (settings_data.get("fields") or {}).get("offer_membership")
                                ),
                                "new": bool(offer_membership),
                            },
                        }
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for requirements save: {e}")

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
            badges.append(
                html.Span(
                    "💳 Payment",
                    style={
                        **badge_style_active,
                        "backgroundColor": "rgba(16, 185, 129, 0.2)",
                        "color": "#10b981",
                    },
                )
            )
        else:
            badges.append(html.Span("💳 Payment", style=badge_style_inactive))

        # Membership badge
        if req_membership:
            badges.append(
                html.Span(
                    "🎫 Membership",
                    style={
                        **badge_style_active,
                        "backgroundColor": "rgba(139, 92, 246, 0.2)",
                        "color": "#8b5cf6",
                    },
                )
            )
        else:
            badges.append(html.Span("🎫 Membership", style=badge_style_inactive))

        # Start.gg badge
        if req_startgg:
            badges.append(
                html.Span(
                    "🎮 Start.gg",
                    style={
                        **badge_style_active,
                        "backgroundColor": "rgba(0, 212, 255, 0.2)",
                        "color": "#00d4ff",
                    },
                )
            )
        else:
            badges.append(html.Span("🎮 Start.gg", style=badge_style_inactive))

        # If nothing required, add a note
        if not any([req_payment, req_membership, req_startgg]):
            badges.append(
                html.Span(
                    "(All players auto-Ready)",
                    style={"color": "#f59e0b", "fontStyle": "italic", "marginLeft": "0.5rem"},
                )
            )

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
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def save_payment_settings(n_clicks, price_per_game, swish_number, auth_state):
        """Save payment settings (price per game, swish number) to storage backend."""
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

        # Update settings
        result = update_settings(record_id, update_data)

        if result:
            try:
                prev_fields = settings_data.get("fields", {}) or {}
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_update_payment_settings",
                    "settings",
                    target_event=get_active_slug() or "",
                    details=json.dumps(
                        {
                            "swish_expected_per_game": {
                                "old": prev_fields.get("swish_expected_per_game", 0),
                                "new": price,
                            },
                            "swish_number": {
                                "old": prev_fields.get("swish_number", ""),
                                "new": swish_number or "",
                            },
                        }
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for payment settings save: {e}")

            return html.Span(
                f"✅ Saved! Price: {price} kr/game, Swish: {swish_number}",
                style={"color": "#10b981"},
            )
        else:
            return html.Span("❌ Failed to save settings", style={"color": "#ef4444"})

    # -------------------------------------------------------------------------
    # Archive current event to event_archive + event_stats
    # -------------------------------------------------------------------------
    @app.callback(
        Output("archive-feedback", "children"),
        Input("btn-archive-event-quick", "n_clicks"),
        Input("btn-archive-event", "n_clicks"),
        State("event-dropdown", "value"),
        State("archive-clear-active-toggle", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def archive_current_event(n_clicks_quick, n_clicks, selected_slug, clear_flags, auth_state):
        if not n_clicks and not n_clicks_quick:
            return no_update

        if not selected_slug or selected_slug == "__ALL__":
            return html.Span(
                "❌ Select a specific event before archiving.", style={"color": "#ef4444"}
            )

        clear_active = "clear" in (clear_flags or [])

        settings = get_active_settings() or {}
        payload = {
            "event_slug": selected_slug,
            "event_date": settings.get("event_date"),
            "event_display_name": settings.get("event_display_name", ""),
            "swish_expected_per_game": settings.get("swish_expected_per_game", 0),
            "startgg_snapshot": settings.get("events_json"),
            "clear_active": clear_active,
            "user": {
                "user_id": (auth_state or {}).get("user_id", ""),
                "user_name": (auth_state or {}).get("user_name", "system"),
                "user_email": (auth_state or {}).get("user_email", ""),
            },
        }

        archive_fn = getattr(storage_api, "archive_event", None)
        if archive_fn:
            try:
                result = archive_fn(**payload)
            except Exception as e:
                logger.exception(f"Archive failed for {selected_slug}: {e}")
                return html.Span(f"❌ Archive failed: {e}", style={"color": "#ef4444"})
        else:
            try:
                resp = requests.post(
                    f"{BACKEND_INTERNAL_URL}/api/archive/event",
                    json=payload,
                    timeout=30,
                )
                if not resp.ok:
                    return html.Span(
                        f"❌ Archive failed ({resp.status_code}): {resp.text}",
                        style={"color": "#ef4444"},
                    )
                result = resp.json()
            except Exception as e:
                logger.exception(f"Archive API call failed for {selected_slug}: {e}")
                return html.Span(f"❌ Archive API failed: {e}", style={"color": "#ef4444"})

        try:
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "event_archived",
                "event_stats",
                target_event=selected_slug,
                details=json.dumps(
                    {
                        "archived": result.get("archived", 0),
                        "total_revenue": result.get("total_revenue", 0),
                        "new_players": result.get("new_players", 0),
                        "returning_players": result.get("returning_players", 0),
                        "clear_active": clear_active,
                    }
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for archive: {e}")

        return html.Div(
            style={"color": "#10b981", "lineHeight": "1.5"},
            children=[
                html.Div(f"✅ Archived event: {result.get('event_slug', selected_slug)}"),
                html.Div(
                    f"Participants: {result.get('archived', 0)} | "
                    f"Revenue: {result.get('total_revenue', 0)} | "
                    f"New: {result.get('new_players', 0)} | Returning: {result.get('returning_players', 0)}"
                ),
                html.Div(
                    f"Replaced rows: {result.get('replaced_rows', 0)} | "
                    f"Cleared active: {result.get('cleared_active', 0)}"
                ),
            ],
        )

    # -------------------------------------------------------------------------
    # Reopen archived event and optionally restore active check-ins
    # -------------------------------------------------------------------------
    @app.callback(
        Output("reopen-feedback", "children"),
        Input("btn-reopen-event", "n_clicks"),
        State("event-dropdown", "value"),
        State("reopen-restore-active-toggle", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def reopen_current_event(n_clicks, selected_slug, restore_flags, auth_state):
        if not n_clicks:
            return no_update

        if not selected_slug or selected_slug == "__ALL__":
            return html.Span(
                "❌ Select a specific event before reopening.", style={"color": "#ef4444"}
            )

        restore_active = "restore" in (restore_flags or [])
        payload = {
            "event_slug": selected_slug,
            "restore_active": restore_active,
            "user": {
                "user_id": (auth_state or {}).get("user_id", ""),
                "user_name": (auth_state or {}).get("user_name", "system"),
                "user_email": (auth_state or {}).get("user_email", ""),
            },
        }

        reopen_fn = getattr(storage_api, "reopen_event", None)
        if reopen_fn:
            try:
                result = reopen_fn(**payload)
            except Exception as e:
                logger.exception(f"Reopen failed for {selected_slug}: {e}")
                return html.Span(f"❌ Reopen failed: {e}", style={"color": "#ef4444"})
        else:
            try:
                resp = requests.post(
                    f"{BACKEND_INTERNAL_URL}/api/archive/reopen",
                    json=payload,
                    timeout=30,
                )
                if not resp.ok:
                    return html.Span(
                        f"❌ Reopen failed ({resp.status_code}): {resp.text}",
                        style={"color": "#ef4444"},
                    )
                result = resp.json()
            except Exception as e:
                logger.exception(f"Reopen API call failed for {selected_slug}: {e}")
                return html.Span(f"❌ Reopen API failed: {e}", style={"color": "#ef4444"})

        try:
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "event_reopened",
                "event_stats",
                target_event=selected_slug,
                details=json.dumps(
                    {
                        "restore_active": restore_active,
                        "restored_rows": result.get("restored_rows", 0),
                    }
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for reopen: {e}")

        return html.Div(
            style={"color": "#10b981", "lineHeight": "1.5"},
            children=[
                html.Div(f"✅ Reopened event: {result.get('event_slug', selected_slug)}"),
                html.Div(
                    f"Restore active: {result.get('restore_active', restore_active)} | "
                    f"Restored rows: {result.get('restored_rows', 0)}"
                ),
            ],
        )

    # -------------------------------------------------------------------------
    # Delete archived event from history - Step 1: confirmation prompt
    # -------------------------------------------------------------------------
    @app.callback(
        Output("confirm-delete-event-dialog", "displayed"),
        Output("confirm-delete-event-dialog", "message"),
        Output("delete-event-feedback", "children", allow_duplicate=True),
        Input("btn-delete-event-history", "n_clicks"),
        State("delete-archive-event-dropdown", "value"),
        State("input-delete-event-reason", "value"),
        prevent_initial_call=True,
    )
    def show_delete_event_confirmation(n_clicks, selected_slug, reason):
        if not n_clicks:
            return False, "", no_update

        if not selected_slug or selected_slug == "__ALL__":
            return (
                False,
                "",
                html.Span(
                    "❌ Select a specific event before deleting archive.",
                    style={"color": "#ef4444"},
                ),
            )

        reason_text = (reason or "").strip()
        if not reason_text:
            return (
                False,
                "",
                html.Span("❌ Deletion reason is required.", style={"color": "#ef4444"}),
            )

        msg = (
            f"Delete archived history for '{selected_slug}'?\n\n"
            f"Reason: {reason_text}\n\n"
            "This deletes event_archive + event_stats rows and cannot be undone."
        )
        return True, msg, no_update

    # -------------------------------------------------------------------------
    # Delete archived event from history - Step 2: execute delete
    # -------------------------------------------------------------------------
    @app.callback(
        Output("delete-event-feedback", "children"),
        Input("confirm-delete-event-dialog", "submit_n_clicks"),
        State("delete-archive-event-dropdown", "value"),
        State("input-delete-event-reason", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def delete_archived_event(submit_n_clicks, selected_slug, reason, auth_state):
        if not submit_n_clicks:
            return no_update

        if not selected_slug or selected_slug == "__ALL__":
            return html.Span(
                "❌ Select a specific event before deleting archive.", style={"color": "#ef4444"}
            )

        reason_text = (reason or "").strip()
        if not reason_text:
            return html.Span("❌ Deletion reason is required.", style={"color": "#ef4444"})

        payload = {
            "event_slug": selected_slug,
            "reason": reason_text,
            "user": {
                "user_id": (auth_state or {}).get("user_id", ""),
                "user_name": (auth_state or {}).get("user_name", "system"),
                "user_email": (auth_state or {}).get("user_email", ""),
            },
        }

        delete_fn = getattr(storage_api, "delete_archived_event", None)
        if delete_fn:
            try:
                result = delete_fn(**payload)
            except Exception as e:
                logger.exception(f"Delete archive failed for {selected_slug}: {e}")
                return html.Span(f"❌ Delete archive failed: {e}", style={"color": "#ef4444"})
        else:
            try:
                resp = requests.post(
                    f"{BACKEND_INTERNAL_URL}/api/archive/delete",
                    json=payload,
                    timeout=30,
                )
                if not resp.ok:
                    return html.Span(
                        f"❌ Delete archive failed ({resp.status_code}): {resp.text}",
                        style={"color": "#ef4444"},
                    )
                result = resp.json()
            except Exception as e:
                logger.exception(f"Delete archive API call failed for {selected_slug}: {e}")
                return html.Span(f"❌ Delete archive API failed: {e}", style={"color": "#ef4444"})

        return html.Div(
            style={"color": "#10b981", "lineHeight": "1.5"},
            children=[
                html.Div(f"🗑️ Deleted archived event: {result.get('event_slug', selected_slug)}"),
                html.Div(
                    f"Archive rows removed: {result.get('deleted_archive_rows', 0)} | "
                    f"Stats rows removed: {result.get('deleted_stats_rows', 0)}"
                ),
                html.Div(f"Reason: {reason_text}"),
            ],
        )

    # -------------------------------------------------------------------------
    # Audit Log - load and filter entries
    # -------------------------------------------------------------------------
    @app.callback(
        Output("audit-log-table", "data"),
        Output("audit-log-table", "tooltip_data"),
        Output("audit-log-count", "children"),
        Output("audit-filter-action", "options"),
        Output("audit-filter-user", "options"),
        Input("tabs", "value"),
        Input("btn-audit-refresh", "n_clicks"),
        Input("audit-filter-action", "value"),
        Input("audit-filter-user", "value"),
    )
    def update_audit_log(selected_tab, _refresh_clicks, filter_action, filter_user):
        """
        Load audit log entries when the Audit Log tab is selected.
        Applies optional action and user filters.
        Only fetches data when the audit tab is active (avoids unnecessary API calls).
        """
        if selected_tab != "tab-settings":
            return no_update, no_update, no_update, no_update, no_update

        try:
            # Fetch with server-side filters where possible
            entries = get_audit_log(
                action=filter_action or None,
                user_id=filter_user or None,
                limit=200,
            )
        except Exception as e:
            logger.error(f"Failed to load audit log: {e}")
            return [], [], "Error loading audit log", [], []

        if not entries:
            return [], [], "0 entries", [], []

        # Format timestamps for display (keep full ISO in tooltip)
        for entry in entries:
            raw_action = entry.get("action") or ""
            entry["_action_raw"] = raw_action
            entry["action_category"] = get_action_group(raw_action)
            entry["action"] = format_action_label(raw_action)

            raw_ts = entry.get("timestamp", "")
            if raw_ts:
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    entry["timestamp"] = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    pass

        # Build unique action and user options from ALL entries (unfiltered)
        # For action options, re-fetch without action filter
        try:
            all_entries = get_audit_log(limit=200)
        except Exception:
            all_entries = entries

        action_values = sorted(
            {e.get("action") for e in all_entries if e.get("action")},
            key=action_sort_key,
        )
        user_values = sorted(
            {e.get("user_name") for e in all_entries if e.get("user_name")},
            key=str.lower,
        )

        action_options = [
            {"label": format_action_filter_label(a), "value": a} for a in action_values
        ]
        # User filter uses user_id for filtering but shows user_name
        user_id_map = {}
        for e in all_entries:
            uid = e.get("user_id")
            uname = e.get("user_name")
            if uid and uname and uid not in user_id_map:
                user_id_map[uid] = uname
        user_options = [
            {"label": name, "value": uid}
            for uid, name in sorted(user_id_map.items(), key=lambda x: x[1].lower())
        ]

        # Build tooltip data for full details on hover
        tooltip_data = []
        for entry in entries:
            row_tips = {}
            details = entry.get("details", "")
            reason = entry.get("reason", "")
            raw_action = entry.get("_action_raw", "")
            action_tip = f"Raw action: {raw_action}" if raw_action else ""
            if details:
                action_tip = f"{action_tip}\n\nDetails: {details}" if action_tip else details
            if action_tip:
                row_tips["action"] = {"value": action_tip, "type": "text"}
            if reason:
                row_tips["reason"] = {"value": reason, "type": "text"}
            tooltip_data.append(row_tips)

        count_text = f"{len(entries)} entries"

        return entries, tooltip_data, count_text, action_options, user_options

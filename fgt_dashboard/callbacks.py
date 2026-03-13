# callbacks.py
from dash.dependencies import Input, Output, State, ALL
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
import unicodedata
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Storage backend + Start.gg API config
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:8000")

# Local color tokens used by duplicates UI callbacks.
COLORS = {
    "bg_card": "#0f172a",
    "border": "#1e293b",
    "text_primary": "#e2e8f0",
    "text_secondary": "#94a3b8",
    "accent_blue": "#00d4ff",
    "accent_green": "#34d399",
    "accent_yellow": "#f59e0b",
    "accent_red": "#ef4444",
}

ACTION_META = {
    "auth_login_success": ("Auth", "Login Success"),
    "auth_login_denied": ("Auth", "Login Denied"),
    "auth_logout": ("Auth", "Logout"),
    "auth_select_active_event": ("Auth", "Select Active Event"),
    "admin_fetch_event_data": ("Settings", "Fetch Event Data"),
    "admin_use_mock_event": ("Settings", "Use Mock Event"),
    "admin_update_requirements": ("Settings", "Update Requirements"),
    "admin_update_payment_settings": ("Settings", "Update Payment Settings"),
    "admin_toggle_field": ("Check-ins", "Toggle Field"),
    "admin_update_name": ("Check-ins", "Update Name"),
    "admin_update_tag": ("Check-ins", "Update Tag"),
    "admin_update_telephone": ("Check-ins", "Update Phone"),
    "admin_update_games": ("Check-ins", "Update Games"),
    "admin_update_event_timing": ("Settings", "Update Event Timing"),
    "admin_manual_checkin": ("Check-ins", "Manual Check-in"),
    "admin_recheck_startgg": ("Check-ins", "Re-check Start.gg"),
    "admin_delete_checkin": ("Check-ins", "Delete Player"),
    "integration_result": ("Integrations", "Result"),
    "event_archived": ("Archive", "Event Archived"),
    "admin_clear_active_event": ("Archive", "Clear Active Event"),
    "event_rearchived": ("Archive", "Event Re-Archived"),
    "event_reopened": ("Archive", "Event Reopened"),
    "event_deleted_from_history": ("Archive", "Deleted From History"),
    "event_stats_recomputed": ("Archive", "Stats Recomputed"),
    "event_stats_integrity_scanned": ("Archive", "Integrity Scanned"),
}

ACTION_GROUP_ORDER = ["Auth", "Settings", "Check-ins", "Archive", "Integrations", "Other"]

_default_dev_identities = "viktor molina,logisticuz"
_env_dev_identities = os.getenv("DEV_TOOLS_ALLOWED_IDENTITIES", _default_dev_identities)
DEV_ALLOWED_IDENTITIES = {
    item.strip().lower() for item in str(_env_dev_identities).split(",") if item.strip()
}


def _is_dev_tools_owner(auth_state: Any) -> bool:
    if not isinstance(auth_state, dict):
        return False

    candidates = [
        auth_state.get("user_name"),
        auth_state.get("user_email"),
        auth_state.get("user_id"),
        auth_state.get("username"),
        auth_state.get("tag"),
    ]
    normalized = {str(v).strip().lower() for v in candidates if str(v or "").strip()}
    return any(v in DEV_ALLOWED_IDENTITIES for v in normalized)


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
        Output("active-event-coverage-source", "children"),
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
            logger.info("No event slug selected – clearing table.")
            return (
                [],
                [{"name": "No event selected", "id": "info"}],
                "Select an event",
                "",
                "",
                [],
                {"display": "none"},
                "",
                [],
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
                    "",
                    [],
                    {"display": "none"},
                    "",
                    [],
                )

            df = pd.DataFrame(data)
            total_count = len(df)
            coverage_text = ""
            coverage_source = ""

            try:
                settings = get_active_settings() or {}
                active_slug = (settings.get("active_event_slug") or "").strip()
                events_json = settings.get("events_json")
                registered_players = 0
                registered_slots = 0

                snapshot_slug = ""
                startgg_url = (settings.get("startgg_event_url") or "").strip()
                if startgg_url:
                    m = re.search(r"/tournament/([^/]+)", startgg_url)
                    if m:
                        snapshot_slug = (m.group(1) or "").strip()

                # For active event: use live settings snapshot.
                if selected_slug == active_slug:
                    # Guard against stale/mismatched settings snapshot.
                    snapshot_matches_selected = (not snapshot_slug) or (
                        snapshot_slug == selected_slug
                    )

                    if snapshot_matches_selected:
                        if isinstance(events_json, dict):
                            registered_players = int(
                                events_json.get("tournament_entrants_players") or 0
                            )
                            registered_slots = int(events_json.get("tournament_entrants") or 0)
                        elif isinstance(
                            settings.get("startgg_registered_count"), (int, float, str)
                        ):
                            registered_slots = int(settings.get("startgg_registered_count") or 0)

                        if registered_players > 0:
                            coverage_rate = (total_count / registered_players) * 100
                            coverage_text = f"Coverage: {total_count}/{registered_players} players ({coverage_rate:.0f}%)"
                            coverage_source = "Source: Active snapshot"
                            if registered_slots and registered_slots != registered_players:
                                coverage_text += f" | {registered_slots} event slots"
                        elif registered_slots > 0:
                            coverage_rate = (total_count / registered_slots) * 100
                            coverage_text = f"Coverage (slots): {total_count}/{registered_slots} ({coverage_rate:.0f}%)"
                            coverage_source = "Source: Active snapshot (slot fallback)"
                    else:
                        coverage_text = (
                            "Coverage unavailable: Start.gg snapshot belongs to another event. "
                            "Run Fetch Event Data for the selected event."
                        )
                        coverage_source = "Source: Guarded mismatch"

                # For non-active/archived events: use event_stats by slug.
                elif selected_slug and selected_slug != "__ALL__":
                    history_fn = getattr(storage_api, "get_event_history_dashboard", None)
                    if callable(history_fn):
                        history_raw = history_fn() or []
                        history_rows = history_raw if isinstance(history_raw, list) else []
                        stat_row = next(
                            (
                                r
                                for r in history_rows
                                if (r.get("event_slug") or "") == selected_slug
                            ),
                            None,
                        )
                        if stat_row:
                            checked_in = int(
                                stat_row.get("checked_in_count")
                                or stat_row.get("total_participants")
                                or total_count
                            )
                            registered_players = int(
                                stat_row.get("startgg_registered_players") or 0
                            )
                            registered_slots = int(stat_row.get("startgg_registered_count") or 0)

                            if registered_players > 0:
                                coverage_rate = (checked_in / registered_players) * 100
                                coverage_text = f"Coverage: {checked_in}/{registered_players} players ({coverage_rate:.0f}%)"
                                coverage_source = "Source: Archived stats"
                                if registered_slots and registered_slots != registered_players:
                                    coverage_text += f" | {registered_slots} event slots"
                            elif registered_slots > 0:
                                coverage_rate = (checked_in / registered_slots) * 100
                                coverage_text = (
                                    f"Coverage (slots): {checked_in}/{registered_slots} ({coverage_rate:.0f}%)"
                                    " | player coverage unavailable for this archived snapshot"
                                )
                                coverage_source = "Source: Archived stats (slot fallback)"
                            else:
                                coverage_text = "Coverage unavailable for this archived event"
                                coverage_source = "Source: Archived stats"
            except Exception:
                coverage_text = ""
                coverage_source = ""

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
                coverage_source,
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
    # Sync column visibility: dropdown (Settings) ↔ checklist (Manual Tools)
    # ---------------------------------------------------------------------
    @app.callback(
        Output("visible-columns-store", "data"),
        Output("column-visibility-dropdown", "value"),
        Output("quick-column-toggle", "value"),
        Input("column-visibility-dropdown", "value"),
        Input("quick-column-toggle", "value"),
        prevent_initial_call=True,
    )
    def update_column_visibility(dropdown_val, checklist_val):
        """Keep both column selectors and the store in sync."""
        default = ["name", "tag", "telephone", "member", "startgg", "payment_valid", "status"]
        trigger = ctx.triggered_id
        if trigger == "quick-column-toggle":
            val = checklist_val or default
            return val, val, no_update
        else:
            val = dropdown_val or default
            return val, no_update, val

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
            return {"display": "block", "marginTop": "0.6rem"}, "Manual Checkin Tools ▴"
        return {"display": "none", "marginTop": "0.6rem"}, "Manual Checkin Tools ▾"

    @app.callback(
        Output("checkins-table", "row_selectable"),
        Output("checkins-table", "selected_rows"),
        Output("btn-toggle-multiselect", "children"),
        Output("btn-toggle-multiselect", "style"),
        Input("btn-toggle-multiselect", "n_clicks"),
        State("checkins-table", "selected_rows"),
        State("checkins-table", "row_selectable"),
        prevent_initial_call=False,
    )
    def toggle_multiselect_mode(n_clicks, selected_rows, current_mode):
        enabled_style = {
            "backgroundColor": "transparent",
            "color": "#00d4ff",
            "border": "1px solid #00d4ff",
            "borderRadius": "8px",
            "padding": "0.45rem 0.75rem",
            "fontSize": "0.8rem",
            "fontWeight": "600",
            "cursor": "pointer",
        }
        disabled_style = {
            "backgroundColor": "transparent",
            "color": "#94a3b8",
            "border": "1px solid #334155",
            "borderRadius": "8px",
            "padding": "0.45rem 0.75rem",
            "fontSize": "0.8rem",
            "fontWeight": "600",
            "cursor": "pointer",
        }

        # If user already has selected rows, this button works as quick "clear selection".
        if selected_rows:
            is_enabled = current_mode == "multi"
            return (
                "multi" if is_enabled else False,
                [],
                "Multi-select: ON" if is_enabled else "Multi-select: OFF",
                enabled_style if is_enabled else disabled_style,
            )

        enabled = not bool(n_clicks and n_clicks % 2 == 1)
        if enabled:
            return "multi", [], "Multi-select: ON", enabled_style
        return (
            False,
            [],
            "Multi-select: OFF",
            disabled_style,
        )

    @app.callback(
        Output("confirm-clear-event-dialog", "displayed"),
        Output("confirm-clear-event-dialog", "message"),
        Input("btn-clear-current-event", "n_clicks"),
        State("event-dropdown", "value"),
        prevent_initial_call=True,
    )
    def show_clear_event_confirmation(n_clicks, selected_slug):
        if not n_clicks:
            return False, no_update
        if not selected_slug or selected_slug == "__ALL__":
            return False, no_update
        return True, f"Clear current event '{selected_slug}' without archiving?"

    @app.callback(
        Output("event-dropdown", "value", allow_duplicate=True),
        Output("archive-feedback", "children", allow_duplicate=True),
        Input("confirm-clear-event-dialog", "submit_n_clicks"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
        running=[
            (Output("btn-clear-current-event", "disabled"), True, False),
            (Output("btn-archive-event-quick", "disabled"), True, False),
            (Output("btn-archive-event", "disabled"), True, False),
        ],
    )
    def clear_current_event(submit_n_clicks, selected_slug, auth_state):
        if not submit_n_clicks:
            return no_update, no_update
        if not selected_slug or selected_slug == "__ALL__":
            return no_update, html.Span("⚠️ No specific event selected.", style={"color": "#f59e0b"})

        try:
            # Clear only local dropdown selection in dashboard;
            # keep global active event to avoid auth redirect kicks.

            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_clear_active_event",
                    "settings",
                    target_event=selected_slug,
                    details=json.dumps({"cleared_via": "manual_clear_button"}),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for clear current event: {e}")

            return None, html.Span(
                "✅ Cleared event selection (dashboard only).", style={"color": "#10b981"}
            )
        except Exception as e:
            logger.exception(f"Failed to clear current event: {e}")
            return no_update, html.Span(
                f"❌ Failed to clear current event: {e}", style={"color": "#ef4444"}
            )

    @app.callback(
        Output("dev-tools-advanced-container", "style"),
        Output("dev-tools-panel", "style"),
        Output("recompute-dev-container", "style"),
        Input("auth-store", "data"),
        Input("dev-tools-visible-toggle", "value"),
    )
    def toggle_dev_tools_visibility(auth_state, visibility_flags):
        if not _is_dev_tools_owner(auth_state):
            return {"display": "none"}, {"display": "none"}, {"display": "none"}

        show_panel = "show" in (visibility_flags or [])
        return {"display": "block", "marginBottom": "1.1rem"}, {
            "display": "block" if show_panel else "none"
        }, {"display": "block"}

    # ---------------------------------------------------------------------
    # Admin: Fetch event data from Start.gg and update settings
    # ---------------------------------------------------------------------
    @app.callback(
        Output("settings-output", "children"),
        Output("event-dropdown", "options"),
        Output("event-dropdown", "value"),
        Input("btn-fetch-event", "n_clicks"),
        Input("btn-use-mock-event", "n_clicks"),
        State("input-startgg-link", "value"),
        State("input-mock-event-slug", "value"),
        State("input-mock-event-name", "value"),
        State("auth-store", "data"),
    )
    def fetch_event_data(n_clicks, mock_clicks, link, mock_slug, mock_name, auth_state):
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
        if not n_clicks and not mock_clicks:
            return no_update, no_update, no_update

        trigger_id = ctx.triggered_id

        if trigger_id == "btn-use-mock-event":
            if not _is_dev_tools_owner(auth_state):
                return "❌ Unauthorized: dev tools are owner-only.", no_update, no_update

            # Prepare safe mock values
            slug = (mock_slug or "mock-event").strip().lower()
            slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
            if not slug:
                return "❌ Invalid mock slug.", no_update, no_update

            event_name = (mock_name or "Mock Event").strip() or "Mock Event"
            start_iso = date.today().isoformat()
            fetched_names = ["SSBU Singles"]
            events_compact = [
                {
                    "id": 999999,
                    "name": "SSBU Singles",
                    "slug": f"{slug}/event/ssbu-singles",
                    "startAt": None,
                    "numEntrants": 0,
                    "setCount": 0,
                    "gamesPlayed": 0,
                }
            ]
            events_json_value = {
                "tournament_entrants": 0,
                "tournament_entrants_players": 0,
                "events": events_compact,
            }

            settings_data = get_active_settings_with_id()
            if not settings_data:
                return "❌ No active settings record found.", no_update, no_update

            settings_id = settings_data["record_id"]
            patch_fields = {
                "active_event_slug": slug,
                "event_display_name": event_name,
                "is_active": True,
                "events_json": events_json_value,
                "event_date": start_iso,
                "default_game": fetched_names,
                "startgg_event_url": f"https://start.gg/tournament/{slug}",
                "tournament_name": event_name,
                "timezone": "Europe/Stockholm",
            }

            result = update_settings(settings_id, patch_fields)
            if not result:
                return "❌ Failed to apply mock event", no_update, no_update

            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_use_mock_event",
                    "settings",
                    target_event=slug,
                    details=json.dumps(
                        {
                            "event_name": event_name,
                            "default_games": fetched_names,
                            "source": "dev_tools",
                        }
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for mock event: {e}")

            from shared.storage import get_all_event_slugs

            all_slugs = get_all_event_slugs() or []
            if slug not in all_slugs:
                all_slugs = [slug] + all_slugs

            dropdown_options = [
                {
                    "label": (event_name if s == slug else s.replace("-", " ").title()),
                    "value": s,
                }
                for s in all_slugs
            ]

            return (
                f"✅ Mock event active: {event_name} ({slug})",
                dropdown_options,
                slug,
            )

        if not link:
            return "❌ Enter a Start.gg link first.", no_update, no_update

        def _fetch_event_games_summary(event_id: Any, headers: Dict[str, str]) -> Dict[str, int]:
            """Fetch set scores for one event and aggregate game counts."""
            if not event_id:
                return {"games_played": 0, "sets_with_score": 0, "set_total": 0}

            query_sets = {
                "query": """
                query EventSets($eventId: ID!, $page: Int!, $perPage: Int!) {
                  event(id: $eventId) {
                    sets(page: $page, perPage: $perPage, sortType: STANDARD) {
                      pageInfo { total totalPages }
                      nodes {
                        slots(includeByes: false) {
                          standing {
                            stats {
                              score {
                                value
                                label
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
                """,
                "variables": {"eventId": str(event_id), "page": 1, "perPage": 100},
            }

            games_played = 0
            sets_with_score = 0
            set_total = 0
            page = 1

            while True:
                query_sets["variables"]["page"] = page
                resp_sets = requests.post(
                    "https://api.start.gg/gql/alpha",
                    json=query_sets,
                    headers=headers,
                    timeout=25,
                )
                resp_sets.raise_for_status()
                payload_sets = resp_sets.json()
                if payload_sets.get("errors"):
                    raise RuntimeError(str(payload_sets.get("errors")))

                sets_conn = (
                    (payload_sets.get("data") or {}).get("event") or {}
                ).get("sets") or {}
                page_info = sets_conn.get("pageInfo") or {}
                nodes = sets_conn.get("nodes") or []

                try:
                    set_total = int(page_info.get("total") or set_total or 0)
                except Exception:
                    set_total = set_total or 0

                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    slot_scores = []
                    for slot in node.get("slots") or []:
                        score_obj = (
                            ((slot or {}).get("standing") or {}).get("stats") or {}
                        ).get("score") or {}
                        raw_val = score_obj.get("value")
                        try:
                            score_val = int(raw_val)
                        except (TypeError, ValueError):
                            continue
                        if score_val < 0:
                            continue
                        slot_scores.append(score_val)

                    if slot_scores:
                        sets_with_score += 1
                        games_played += sum(slot_scores)

                total_pages = int(page_info.get("totalPages") or 1)
                if page >= total_pages or not nodes:
                    break
                page += 1

            return {
                "games_played": games_played,
                "sets_with_score": sets_with_score,
                "set_total": set_total,
            }

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
                  entrants(query: {page: 1, perPage: 100}) {
                    pageInfo { total totalPages }
                    nodes {
                      participants { id }
                    }
                  }
                  sets(page: 1, perPage: 1, sortType: STANDARD) {
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

        # Build compact events array + collect unique participant IDs
        events = tournament.get("events") or []
        events_compact = []
        all_participant_ids = set()
        partial_participant_data = False
        partial_score_data = False
        for e in events:
            if not isinstance(e, dict):
                continue
            entrant_total = 0
            entrants_data = e.get("entrants") or {}
            try:
                entrant_total = entrants_data["pageInfo"]["total"]
            except (KeyError, TypeError):
                pass
            # Collect participant IDs for unique player count
            nodes = entrants_data.get("nodes") or []
            for node in nodes:
                for p in node.get("participants") or []:
                    pid = p.get("id")
                    if pid:
                        all_participant_ids.add(pid)
            sets_data = e.get("sets") or {}
            set_total = 0
            try:
                set_total = int((sets_data.get("pageInfo") or {}).get("total") or 0)
            except Exception:
                set_total = 0

            games_played = 0
            sets_with_score = 0
            try:
                score_summary = _fetch_event_games_summary(e.get("id"), headers_startgg)
                games_played = int(score_summary.get("games_played") or 0)
                sets_with_score = int(score_summary.get("sets_with_score") or 0)
            except Exception as ex:
                partial_score_data = True
                logger.warning(f"Failed set-score summary for event {e.get('id')}: {ex}")
            # Check if we got all entrants (pagination)
            total_pages = (entrants_data.get("pageInfo") or {}).get("totalPages", 1)
            if total_pages > 1:
                partial_participant_data = True
                logger.warning(
                    f"Event {e.get('name')}: {total_pages} pages of entrants, "
                    f"only fetched page 1. Unique player count may be approximate."
                )
            events_compact.append(
                {
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "slug": e.get("slug"),
                    "startAt": e.get("startAt"),
                    "numEntrants": entrant_total,
                    "setCount": set_total,
                    "gamesPlayed": games_played,
                    "scoredSets": sets_with_score,
                }
            )
        tournament_entrants_slots = sum(ev.get("numEntrants", 0) for ev in events_compact)
        tournament_entrants_players = len(all_participant_ids)
        logger.info(
            f"Entrant counts: {tournament_entrants_players} unique players, "
            f"{tournament_entrants_slots} event slots (across {len(events_compact)} events)"
            f"{' [partial data]' if partial_participant_data else ''}"
        )
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
        # Wrap events_json with both slot and player counts for accurate no-show tracking.
        # tournament_entrants = slot total (backward compat for old snapshots)
        # tournament_entrants_players = unique players (used for no-show)
        events_json_value = {
            "tournament_entrants": tournament_entrants_slots,
            "tournament_entrants_players": tournament_entrants_players,
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

        output_msg = f"✅ Updated {tournament_name} • {len(events)} events"
        if partial_participant_data:
            output_msg += " • ⚠ entrants partial"
        if partial_score_data:
            output_msg += " • ⚠ games partial"

        return output_msg, dropdown_options, slug

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
        Output("input-manual-games", "options"),
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
            return [], None, "⚠️ Could not read active settings.", [], []

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

        return options, value, help_text, mapping, options

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
        Output("insights-kpi-unique", "children"),
        Output("insights-kpi-total-delta", "children"),
        Output("insights-kpi-revenue-delta", "children"),
        Output("insights-kpi-readyrate-delta", "children"),
        Output("insights-kpi-memberrate-delta", "children"),
        Output("insights-kpi-guestrate-delta", "children"),
        Output("insights-kpi-startggrate-delta", "children"),
        Output("insights-kpi-retention-delta", "children"),
        Output("insights-top-game", "children"),
        Output("insights-added-via-summary", "children"),
        Output("insights-ops-live-note", "children"),
        Output("insights-top-players-title", "children"),
        Output("insights-top-players-table", "data"),
        Output("insights-player-funnel-note", "children"),
        Output("insights-player-funnel", "children"),
        Output("insights-games-title", "children"),
        Output("insights-games-table", "data"),
        Output("insights-game-mover", "children"),
        Output("insights-games-trend", "figure"),
        Output("insights-crossover-title", "children"),
        Output("insights-crossover-table", "data"),
        Output("insights-events-table", "data"),
        Output("insights-earnings-table", "data"),
        Output("insights-kpi-slots", "children"),
        Output("insights-kpi-avggames", "children"),
        Output("insights-kpi-multigame", "children"),
        Output("insights-kpi-new", "children"),
        Output("insights-kpi-returning", "children"),
        Output("insights-kpi-noshow", "children"),
        Output("insights-kpi-manual", "children"),
        Output("insights-kpi-checkinspeed", "children"),
        Output("insights-kpi-duration", "children"),
        Output("insights-kpi-growth", "children"),
        Output("insights-kpi-churn", "children"),
        Output("insights-kpi-slots-delta", "children"),
        Output("insights-kpi-avggames-delta", "children"),
        Output("insights-kpi-multigame-delta", "children"),
        Output("insights-kpi-new-delta", "children"),
        Output("insights-kpi-returning-delta", "children"),
        Output("insights-kpi-noshow-delta", "children"),
        Output("insights-kpi-manual-delta", "children"),
        Output("insights-kpi-checkinspeed-delta", "children"),
        Output("insights-kpi-duration-delta", "children"),
        Output("insights-kpi-growth-delta", "children"),
        Output("insights-kpi-churn-delta", "children"),
        Output("insights-kpi-coreplayers", "children"),
        Output("insights-kpi-lifetime", "children"),
        Output("insights-kpi-coreplayers-delta", "children"),
        Output("insights-kpi-lifetime-delta", "children"),
        Output("insights-summary-core-value", "children"),
        Output("insights-summary-community-value", "children"),
        Output("insights-summary-tournament-value", "children"),
        Output("insights-summary-operations-value", "children"),
        Input("tabs", "value"),
        Input("btn-insights-refresh", "n_clicks"),
        Input("insights-event-dropdown", "value"),
        Input("insights-period-dropdown", "value"),
        Input("insights-series-dropdown", "value"),
        Input("insights-top-players-limit", "value"),
        Input("insights-top-players-game-filter", "value"),
        Input("insights-top-players-search", "value"),
        Input("insights-date-range", "start_date"),
        Input("insights-date-range", "end_date"),
    )
    def update_insights(
        selected_tab,
        _refresh_clicks,
        selected_event_slugs,
        selected_period,
        selected_series,
        top_players_limit,
        selected_player_game,
        top_players_search,
        custom_start_date,
        custom_end_date,
    ):
        if selected_tab != "tab-insights":
            return (no_update,) * 66

        def _normalize_game_name(name: Any) -> str:
            raw = str(name or "").strip()
            if not raw:
                return ""
            key = raw.lower()
            compact = re.sub(r"[^a-z0-9]+", "", key)

            # SSBU canonicalization
            if (
                "ssbu" in key
                or "super smash bros ultimate" in key
                or key == "smash singles"
                or ("smash" in key and "ultimate" in key)
            ):
                return "SSBU Singles"

            # SF6 canonicalization
            if (
                compact in {"sf6", "streetfighter6", "streetfighter6tournament"}
                or "street fighter 6" in key
                or "sf6" == key
            ):
                return "STREET FIGHTER 6 TOURNAMENT"

            # Tekken 8 canonicalization
            if (
                compact in {"t8", "tekken8", "tekken8tournament"}
                or "tekken 8" in key
                or "t8" == key
            ):
                return "TEKKEN 8 TOURNAMENT"

            return raw

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
                "",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "Game highlight: -",
                "Added via: -",
                "",
                "Top attendees",
                [],
                "",
                [],
                "Game distribution",
                [],
                "",
                {},
                "",
                [],
                [],
                [],
                "0",
                "0.0",
                "0.0%",
                "0",
                "0",
                "0.0%",
                "0.0%",
                "-",
                "-",
                "-",
                "-",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "Live",
                "Live",
                "Live",
                "Live",
                "0",
                "0.0",
                "—",
                "—",
                "-",
                "-",
                "-",
                "-",
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

        try:
            live_settings = get_active_settings() or {}
        except Exception:
            live_settings = {}

        all_events = list(events)

        selected_period = selected_period or "custom"

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

        def _norm_text(value: Any) -> str:
            txt = unicodedata.normalize("NFKD", str(value or ""))
            txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
            txt = re.sub(r"[^a-z0-9]+", "", txt.lower())
            return txt

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
            ev_date_str = ev.get("event_date") or ""
            label = f"{name} ({ev_date_str})" if ev_date_str else name
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
        scope_event_count = len(selected_events)
        single_event_scope = scope_event_count == 1
        selected_single_slug = (
            str(selected_events[0].get("event_slug") or "") if single_event_scope else ""
        )
        active_event_slug = str(live_settings.get("active_event_slug") or "")
        single_active_scope = single_event_scope and selected_single_slug == active_event_slug

        def _aggregate_metrics(event_rows):
            total = 0
            total_revenue = 0.0
            member_count = 0
            guest_count = 0
            startgg_count = 0
            startgg_account_count = 0
            ready_count = 0
            weighted_retention_sum = 0.0
            weighted_retention_den = 0
            top_game_counts = {}
            slots = 0
            new_players = 0
            returning_players = 0
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
                new_players += _as_int(ev.get("new_players"))
                returning_players += _as_int(ev.get("returning_players"))
                startgg_account_count += max(
                    _as_int(ev.get("startgg_count")) - _as_int(ev.get("guest_count")),
                    0,
                )

                status_breakdown = ev.get("status_breakdown")
                if isinstance(status_breakdown, dict):
                    ready_count += _as_int(status_breakdown.get("Ready"))

                retention_val = _as_float(ev.get("retention_rate"))
                if ev_total > 0:
                    weighted_retention_sum += retention_val * ev_total
                    weighted_retention_den += ev_total

                breakdown = ev.get("games_breakdown")
                if isinstance(breakdown, dict):
                    for game, count in breakdown.items():
                        cnt = _as_int(count)
                        if cnt <= 0:
                            continue
                        top_game_counts[game] = top_game_counts.get(game, 0) + cnt
                        slots += cnt

                # No-show aggregation (prefer player count, fall back to slot count)
                reg_players = _as_int(ev.get("startgg_registered_players"))
                reg_slots = _as_int(ev.get("startgg_registered_count"))
                startgg_registered_total += reg_players or reg_slots
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
                "startgg_account_count": startgg_account_count,
                "ready_count": ready_count,
                "new_players": new_players,
                "returning_players": returning_players,
                "retention": (
                    (weighted_retention_sum / weighted_retention_den)
                    if weighted_retention_den > 0
                    else 0.0
                ),
                "top_game_counts": top_game_counts,
                "slots": slots,
                "startgg_registered_total": startgg_registered_total,
                "checked_in_total": checked_in_total,
                "no_show_total": no_show_total,
                "no_show_rate": no_show_rate_agg,
            }

        table_rows = []
        earnings_rows = []
        manual_by_event: Dict[str, Dict[str, Any]] = {}
        added_via_breakdown: List[Dict[str, Any]] = []
        acquisition_source_breakdown: List[Dict[str, Any]] = []

        selected_scope_slugs = [
            ev.get("event_slug") for ev in selected_events if ev.get("event_slug")
        ]
        start_date_iso = range_start.isoformat() if range_start else None
        end_date_iso = range_end.isoformat() if range_end else None
        multi_game_count = 0
        core_players = 0
        player_lifetime = 0.0

        try:
            manual_stats_fn = getattr(storage_api, "get_event_manual_add_stats", None)
            if manual_stats_fn:
                manual_by_event = (
                    manual_stats_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=start_date_iso,
                        end_date=end_date_iso,
                    )
                    or {}
                )
        except Exception as e:
            logger.warning(f"Failed to load manual check-in stats: {e}")

        try:
            added_via_fn = getattr(storage_api, "get_added_via_breakdown", None)
            if added_via_fn:
                added_via_breakdown = (
                    added_via_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=start_date_iso,
                        end_date=end_date_iso,
                    )
                    or []
                )
        except Exception as e:
            logger.warning(f"Failed to load added_via breakdown: {e}")

        try:
            acquisition_source_fn = getattr(storage_api, "get_acquisition_source_breakdown", None)
            if acquisition_source_fn:
                acquisition_source_breakdown = (
                    acquisition_source_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=start_date_iso,
                        end_date=end_date_iso,
                    )
                    or []
                )
        except Exception as e:
            logger.warning(f"Failed to load acquisition-source breakdown: {e}")

        try:
            multi_game_fn = getattr(storage_api, "get_multi_game_count", None)
            if multi_game_fn:
                multi_stats = (
                    multi_game_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=start_date_iso,
                        end_date=end_date_iso,
                    )
                    or {}
                )
                multi_game_count = _as_int(multi_stats.get("multi_game_count"))
        except Exception as e:
            logger.warning(f"Failed to load multi-game stats: {e}")

        try:
            community_v2_fn = getattr(storage_api, "get_community_health_v2_stats", None)
            if community_v2_fn:
                community_v2 = (
                    community_v2_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=start_date_iso,
                        end_date=end_date_iso,
                        anchor_date=end_date_iso,
                    )
                    or {}
                )
                core_players = _as_int(community_v2.get("core_players"))
                player_lifetime = _as_float(community_v2.get("player_lifetime"))
        except Exception as e:
            logger.warning(f"Failed to load community health v2 stats: {e}")

        for ev in selected_events:
            ev_total = _as_int(ev.get("total_participants") or ev.get("participants"))
            ev_checked_in = _as_int(ev.get("checked_in_count"))
            ev_registered = _as_int(ev.get("startgg_registered_players")) or _as_int(
                ev.get("startgg_registered_count")
            )
            ev_member_rate = (
                (_as_int(ev.get("member_count")) / ev_total * 100) if ev_total > 0 else 0.0
            )
            ev_startgg_rate = (
                (
                    max(_as_int(ev.get("startgg_count")) - _as_int(ev.get("guest_count")), 0)
                    / ev_total
                    * 100
                )
                if ev_total > 0
                else 0.0
            )
            ev_revenue = _as_float(ev.get("total_revenue"))
            revenue_per_player = (ev_revenue / ev_total) if ev_total > 0 else 0.0
            ev_no_show = _as_int(ev.get("no_show_count"))
            ev_no_show_rate = _as_float(ev.get("no_show_rate"))
            ev_slug = ev.get("event_slug", "")
            manual_stats = manual_by_event.get(ev_slug, {}) if ev_slug else {}
            manual_count = _as_int(manual_stats.get("manual_count"))
            manual_pct = _as_float(manual_stats.get("manual_pct"))

            checked_in_rate = (ev_checked_in / ev_registered * 100) if ev_registered > 0 else 0.0
            retention_rate = _as_float(ev.get("retention_rate"))

            # Local-event friendly no-show scoring (10-30 players, 1-3 no-shows is normal)
            if ev_no_show_rate <= 10:
                no_show_score = 100.0
            elif ev_no_show_rate <= 20:
                no_show_score = 100.0 - ((ev_no_show_rate - 10.0) * 2.5)  # 100 -> 75
            elif ev_no_show_rate <= 30:
                no_show_score = 75.0 - ((ev_no_show_rate - 20.0) * 2.0)  # 75 -> 55
            elif ev_no_show_rate <= 40:
                no_show_score = 55.0 - ((ev_no_show_rate - 30.0) * 2.0)  # 55 -> 35
            else:
                no_show_score = max(0.0, 35.0 - ((ev_no_show_rate - 40.0) * 1.5))

            manual_score = max(0.0, min(100.0, 100.0 - (manual_pct * 2.0)))
            event_quality_score = (
                0.30 * no_show_score
                + 0.30 * checked_in_rate
                + 0.20 * retention_rate
                + 0.20 * manual_score
            )
            if event_quality_score >= 80:
                quality_level, quality_rank = "Healthy", "4/4"
            elif event_quality_score >= 65:
                quality_level, quality_rank = "Stable", "3/4"
            elif event_quality_score >= 45:
                quality_level, quality_rank = "Watch", "2/4"
            else:
                quality_level, quality_rank = "Critical", "1/4"

            table_rows.append(
                {
                    "event_display_name": ev.get("event_display_name") or ev.get("event_slug", ""),
                    "event_slug": ev_slug,
                    "event_date": ev.get("event_date") or "",
                    "total_participants": ev_total,
                    "no_show_rate": round(ev_no_show_rate, 1),
                    "no_show_count": ev_no_show,
                    "event_quality": f"{quality_level} [{quality_rank}] ({event_quality_score:.0f})",
                    "checked_in_vs_registered": (
                        f"{ev_checked_in}/{ev_registered}"
                        if ev_registered > 0
                        else "-"
                    ),
                    "top_game": _normalize_game_name(ev.get("most_popular_game")) or "-",
                    "total_revenue": f"{ev_revenue:.0f} kr",
                    "member_rate": f"{ev_member_rate:.0f}%",
                    "startgg_rate": f"{ev_startgg_rate:.0f}%",
                    "retention_rate": f"{retention_rate:.0f}%",
                    "manual_count": manual_count,
                    "manual_share": f"{manual_pct:.0f}%" if ev_total > 0 else "-",
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
            (metrics["startgg_account_count"] / metrics["total"] * 100)
            if metrics["total"] > 0
            else 0.0
        )
        retention = metrics["retention"]
        slots = _as_int(metrics.get("slots"))
        avg_games = (slots / metrics["total"]) if metrics["total"] > 0 else 0.0
        multigame_pct = (multi_game_count / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
        new_players = _as_int(metrics.get("new_players"))
        returning_players = _as_int(metrics.get("returning_players"))
        noshow_rate = _as_float(metrics.get("no_show_rate"))
        manual_total_count = sum(_as_int(v.get("total_count")) for v in manual_by_event.values())
        manual_total_manual = sum(_as_int(v.get("manual_count")) for v in manual_by_event.values())
        manual_share = (
            (manual_total_manual / manual_total_count * 100) if manual_total_count > 0 else 0.0
        )

        if metrics["top_game_counts"]:
            top_game = max(metrics["top_game_counts"], key=metrics["top_game_counts"].get)
            top_game_text = f"Game highlight: {top_game}"
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
            if unit == "count1":
                return f"{arrow} {diff:+.1f} vs prev"
            return f"{arrow} {int(diff):+d} vs prev"

        # Delta against previous period (same length), disabled for all-time and specific-event selection.
        prev_metrics = None
        prev_start = None
        prev_end = None
        prev_multi_game_count = None
        prev_manual_share = None
        prev_core_players = None
        prev_player_lifetime = None
        prev_unique_count = None
        prev_churn_rate = None
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
                prev_scope_slugs = [
                    ev.get("event_slug") for ev in prev_events if ev.get("event_slug")
                ]
                prev_start_iso = prev_start.isoformat() if prev_start else None
                prev_end_iso = prev_end.isoformat() if prev_end else None

                try:
                    prev_multi_game_fn = getattr(storage_api, "get_multi_game_count", None)
                    if prev_multi_game_fn:
                        prev_multi_stats = (
                            prev_multi_game_fn(
                                event_slugs=prev_scope_slugs or None,
                                start_date=prev_start_iso,
                                end_date=prev_end_iso,
                            )
                            or {}
                        )
                        prev_multi_game_count = _as_int(prev_multi_stats.get("multi_game_count"))
                except Exception as e:
                    logger.warning(f"Failed to load previous multi-game stats: {e}")

                try:
                    prev_manual_fn = getattr(storage_api, "get_event_manual_add_stats", None)
                    if prev_manual_fn:
                        prev_manual_stats = (
                            prev_manual_fn(
                                event_slugs=prev_scope_slugs or None,
                                start_date=prev_start_iso,
                                end_date=prev_end_iso,
                            )
                            or {}
                        )
                        prev_total_count = sum(
                            _as_int(v.get("total_count")) for v in prev_manual_stats.values()
                        )
                        prev_total_manual = sum(
                            _as_int(v.get("manual_count")) for v in prev_manual_stats.values()
                        )
                        prev_manual_share = (
                            (prev_total_manual / prev_total_count * 100)
                            if prev_total_count > 0
                            else None
                        )
                except Exception as e:
                    logger.warning(f"Failed to load previous manual-share stats: {e}")

                try:
                    prev_community_v2_fn = getattr(
                        storage_api, "get_community_health_v2_stats", None
                    )
                    if prev_community_v2_fn:
                        prev_community_v2 = (
                            prev_community_v2_fn(
                                event_slugs=prev_scope_slugs or None,
                                start_date=prev_start_iso,
                                end_date=prev_end_iso,
                                anchor_date=prev_end_iso,
                            )
                            or {}
                        )
                        prev_core_players = _as_int(prev_community_v2.get("core_players"))
                        prev_player_lifetime = _as_float(prev_community_v2.get("player_lifetime"))
                except Exception as e:
                    logger.warning(f"Failed to load previous community-v2 stats: {e}")

                try:
                    prev_unique_fn = getattr(storage_api, "get_unique_attendee_count", None)
                    if prev_unique_fn:
                        prev_unique_count = prev_unique_fn(
                            event_slugs=prev_scope_slugs or None,
                            start_date=prev_start_iso,
                            end_date=prev_end_iso,
                        )
                except Exception as e:
                    logger.warning(f"Failed to load previous unique attendee count: {e}")

                try:
                    prev_churn_fn = getattr(storage_api, "get_player_churn_stats", None)
                    if prev_churn_fn:
                        prev_churn_stats = (
                            prev_churn_fn(
                                event_slugs=prev_scope_slugs or None,
                                start_date=prev_start_iso,
                                end_date=prev_end_iso,
                                anchor_date=prev_end_iso,
                            )
                            or {}
                        )
                        prev_churn_rate = _as_float(prev_churn_stats.get("churn_rate"))
                except Exception as e:
                    logger.warning(f"Failed to load previous churn stats: {e}")

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
                (prev_metrics["startgg_account_count"] / prev_metrics["total"] * 100)
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "pp",
        )
        retention_delta = _fmt_delta(
            retention, prev_metrics["retention"] if prev_metrics else None, "pp"
        )
        slots_delta = _fmt_delta(
            slots, _as_int(prev_metrics.get("slots")) if prev_metrics else None, "count"
        )
        avggames_delta = _fmt_delta(
            avg_games,
            (
                (_as_int(prev_metrics.get("slots")) / prev_metrics["total"])
                if prev_metrics and prev_metrics["total"] > 0
                else None
            ),
            "count1",
        )
        multigame_delta = _fmt_delta(
            multigame_pct,
            (
                (prev_multi_game_count / prev_metrics["total"] * 100)
                if (
                    prev_metrics and prev_metrics["total"] > 0 and prev_multi_game_count is not None
                )
                else None
            ),
            "pp",
        )
        new_delta = _fmt_delta(
            new_players,
            _as_int(prev_metrics.get("new_players")) if prev_metrics else None,
            "count",
        )
        returning_delta = _fmt_delta(
            returning_players,
            _as_int(prev_metrics.get("returning_players")) if prev_metrics else None,
            "count",
        )
        noshow_delta = _fmt_delta(
            noshow_rate,
            _as_float(prev_metrics.get("no_show_rate")) if prev_metrics else None,
            "pp",
        )
        manual_delta = _fmt_delta(manual_share, prev_manual_share, "pp")
        coreplayers_delta = _fmt_delta(core_players, prev_core_players, "count")
        lifetime_delta = _fmt_delta(player_lifetime, prev_player_lifetime, "count1")
        summary_core = "Participants, checked-in slots, readiness, and revenue"
        summary_community = "New/returning, core, growth, and churn trends"
        summary_tournament = "Game spread, multi-game share, and no-show quality"
        summary_operations = "Manual share and overall check-in process health"
        if not single_active_scope:
            summary_operations += " (ops timing cards need active single-event scope)"

        period_label = {
            "day": "Last 24h",
            "week": "Last 7 days",
            "month": "Last 30 days",
            "quarter": "Last 90 days",
            "year": "Last 365 days",
            "custom": "Year to date",
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
                f"Player coverage: {checked_in_total}/{reg_total} ({checked_rate:.0f}%)."
            )
            if reg_total >= 10:
                if no_show_rate >= 30:
                    heads_up_text += (
                        f" Heads-up: no-show is {no_show_rate:.0f}% "
                        f"({no_show_total} of {reg_total} registered players) in this scope."
                    )
                elif no_show_rate >= 15:
                    heads_up_text += (
                        f" Heads-up: no-show is {no_show_rate:.0f}% "
                        f"({no_show_total} of {reg_total} players) in this scope."
                    )

        # Added-via source summary for selected scope (archive)
        if added_via_breakdown:
            chunks = []
            for row in added_via_breakdown[:3]:
                source = str(row.get("source") or "unknown")
                share = _as_float(row.get("share"))
                chunks.append(f"{source}: {share:.0f}%")
            added_via_summary = "Added via: " + " | ".join(chunks)
        else:
            added_via_summary = "Added via: -"

        if acquisition_source_breakdown:
            total_source_count = sum(int(r.get("count") or 0) for r in acquisition_source_breakdown)
            unknown_source_count = sum(
                int(r.get("count") or 0)
                for r in acquisition_source_breakdown
                if str(r.get("source") or "").strip().lower() == "unknown"
            )
            known_rows = [
                r
                for r in acquisition_source_breakdown
                if str(r.get("source") or "").strip().lower() != "unknown"
            ]
            known_total = max(total_source_count - unknown_source_count, 0)

            if known_total > 0 and known_rows:
                known_rows_sorted = sorted(
                    known_rows,
                    key=lambda r: int(r.get("count") or 0),
                    reverse=True,
                )
                known_chunks = []
                for row in known_rows_sorted[:3]:
                    source = str(row.get("source") or "other")
                    count = int(row.get("count") or 0)
                    share_known = (count / known_total) * 100.0 if known_total > 0 else 0.0
                    known_chunks.append(f"{source}: {share_known:.0f}%")
                acquisition_summary = "Acq known: " + " | ".join(known_chunks)
            else:
                source_chunks = []
                for row in acquisition_source_breakdown[:3]:
                    source = str(row.get("source") or "unknown")
                    share = _as_float(row.get("share"))
                    source_chunks.append(f"{source}: {share:.0f}%")
                acquisition_summary = "Acq: " + " | ".join(source_chunks)

            if total_source_count > 0 and unknown_source_count > 0:
                unknown_share = (unknown_source_count / total_source_count) * 100.0
                acquisition_summary += f" • missing: {unknown_share:.0f}%"
                if unknown_share >= 40:
                    acquisition_summary += " (legacy)"

            added_via_summary = f"{added_via_summary} • {acquisition_summary}"

        ops_live_note = ""
        checkin_speed_value = "-"
        duration_value = "-"
        opened_at = live_settings.get("checkin_opened_at")
        started_at = live_settings.get("event_started_at")
        ended_at = live_settings.get("event_ended_at")
        now_utc = datetime.now(timezone.utc)

        if single_active_scope and opened_at and metrics["total"] > 0:
            try:
                if not isinstance(opened_at, datetime):
                    opened_at = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                minutes = max((now_utc - opened_at).total_seconds() / 60.0, 1.0)
                checkin_speed = metrics["total"] / minutes
                checkin_speed_value = f"{checkin_speed:.2f}/min"
                ops_live_note = f"Live check-in speed: {checkin_speed:.2f} players/min"
            except Exception:
                pass

        if single_active_scope and started_at and ended_at:
            try:
                if not isinstance(started_at, datetime):
                    started_at = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if not isinstance(ended_at, datetime):
                    ended_at = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
                duration_minutes = max((ended_at - started_at).total_seconds() / 60.0, 0.0)
                duration_value = f"{duration_minutes / 60.0:.2f} h"
                duration_text = f"Tournament duration: {duration_minutes / 60.0:.2f} h"
                ops_live_note = (
                    f"{ops_live_note} | {duration_text}" if ops_live_note else duration_text
                )
            except Exception:
                pass

        if single_active_scope:
            checkin_speed_delta = "Live"
            duration_delta = "Live"
        elif single_event_scope:
            checkin_speed_delta = "N/A (active event only)"
            duration_delta = "N/A (active event only)"
        else:
            checkin_speed_delta = "N/A (single-event metric)"
            duration_delta = "N/A (single-event metric)"

        # Top attendees leaderboard for selected scope
        top_players_rows = []
        top_players_title = "Top attendees"
        funnel_note = ""
        funnel_cards: List[Any] = []
        try:
            top_players_fn = getattr(storage_api, "get_top_players_history", None)
            if top_players_fn:
                search_query = _norm_text(top_players_search)
                if top_players_limit == "all":
                    players_limit = 10000
                else:
                    players_limit = _as_int(top_players_limit) or 15
                fetch_limit = 10000 if search_query else players_limit
                selected_player_game = str(selected_player_game or "all").strip() or "all"
                game_filter = None if selected_player_game == "all" else selected_player_game
                top_players_rows = (
                    top_players_fn(
                        event_slugs=selected_scope_slugs,
                        start_date=range_start.isoformat() if range_start else None,
                        end_date=range_end.isoformat() if range_end else None,
                        limit=fetch_limit,
                        game_filter=game_filter,
                    )
                    or []
                )

                if search_query:
                    filtered_rows = []
                    for row in top_players_rows:
                        if not isinstance(row, dict):
                            continue
                        name_norm = _norm_text(row.get("name"))
                        tag_norm = _norm_text(row.get("tag"))
                        if search_query in name_norm or search_query in tag_norm:
                            filtered_rows.append(row)
                    top_players_rows = filtered_rows

                scope_label = (
                    f" in {selected_player_game}"
                    if selected_player_game and selected_player_game != "all"
                    else ""
                )
                if top_players_limit == "all" or search_query:
                    count_label = f"{len(top_players_rows)} shown"
                else:
                    count_label = f"{len(top_players_rows)} shown of {players_limit}"
                top_players_title = f"Top attendees{scope_label} ({count_label})"
        except Exception as e:
            logger.warning(f"Failed to load top players leaderboard: {e}")

        try:
            funnel_fn = getattr(storage_api, "get_player_funnel_stats", None)
            if funnel_fn:
                funnel_stats = (
                    funnel_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=range_start.isoformat() if range_start else None,
                        end_date=range_end.isoformat() if range_end else None,
                        anchor_date=range_end.isoformat() if range_end else None,
                    )
                    or {}
                )
                funnel_new = _as_int(funnel_stats.get("new_count"))
                funnel_returning = _as_int(funnel_stats.get("returning_count"))
                funnel_core = _as_int(funnel_stats.get("core_count"))
                funnel_churned = _as_int(funnel_stats.get("churned_count"))
                funnel_max = max(funnel_new, funnel_returning, funnel_core, funnel_churned, 1)

                def _funnel_card(label: str, value: int, color: str) -> Any:
                    width_pct = int(round((value / funnel_max) * 100))
                    return html.Div(
                        style={
                            "background": "#0f172a",
                            "border": "1px solid #1e293b",
                            "borderRadius": "8px",
                            "padding": "0.45rem 0.55rem",
                        },
                        children=[
                            html.Div(
                                style={
                                    "display": "flex",
                                    "justifyContent": "space-between",
                                    "alignItems": "center",
                                    "marginBottom": "0.25rem",
                                },
                                children=[
                                    html.Span(
                                        label,
                                        style={
                                            "fontSize": "0.7rem",
                                            "color": "#94a3b8",
                                            "textTransform": "uppercase",
                                            "letterSpacing": "0.04em",
                                        },
                                    ),
                                    html.Span(
                                        str(value),
                                        style={
                                            "fontSize": "0.9rem",
                                            "color": color,
                                            "fontWeight": "700",
                                        },
                                    ),
                                ],
                            ),
                            html.Div(
                                style={
                                    "height": "6px",
                                    "background": "#0b1220",
                                    "borderRadius": "999px",
                                    "overflow": "hidden",
                                },
                                children=[
                                    html.Div(
                                        style={
                                            "height": "100%",
                                            "width": f"{width_pct}%",
                                            "background": color,
                                        }
                                    )
                                ],
                            ),
                        ],
                    )

                funnel_cards = [
                    _funnel_card("New", funnel_new, "#34d399"),
                    _funnel_card("Returning", funnel_returning, "#fb923c"),
                    _funnel_card("Core (6m)", funnel_core, "#22c55e"),
                    _funnel_card("Churned (8m)", funnel_churned, "#f87171"),
                ]
                funnel_note = (
                    "New: first-time players | Returning: 2+ total events | "
                    "Core: 3+ distinct events in rolling 6 months | "
                    "Churned: no attendance in 8 months (global)"
                )

                if single_event_scope:
                    funnel_cards = []
                    funnel_note = "Funnel v2 visas i period/series-scope (inte single-event)."
        except Exception as e:
            logger.warning(f"Failed to load player funnel stats: {e}")

        # Unique attendee count (distinct player_uuid) for selected scope
        unique_count = 0
        try:
            unique_fn = getattr(storage_api, "get_unique_attendee_count", None)
            if unique_fn:
                unique_count = unique_fn(
                    event_slugs=selected_scope_slugs or None,
                    start_date=range_start.isoformat() if range_start else None,
                    end_date=range_end.isoformat() if range_end else None,
                )
        except Exception as e:
            logger.warning(f"Failed to load unique attendee count: {e}")
        unique_text = f"{unique_count} unique attendees" if unique_count > 0 else ""

        growth_rate = None
        if prev_unique_count is not None and prev_unique_count > 0:
            growth_rate = ((unique_count - prev_unique_count) / prev_unique_count) * 100.0
        growth_value = f"{growth_rate:.1f}%" if growth_rate is not None else "-"
        growth_delta = (
            f"From prev uniques: {prev_unique_count}" if prev_unique_count is not None else "Live"
        )
        if single_event_scope:
            growth_value = "-"
            growth_delta = "N/A (multi-event trend)"

        churn_count = 0
        churn_rate = 0.0
        try:
            churn_fn = getattr(storage_api, "get_player_churn_stats", None)
            if churn_fn:
                churn_stats = (
                    churn_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=range_start.isoformat() if range_start else None,
                        end_date=range_end.isoformat() if range_end else None,
                        anchor_date=range_end.isoformat() if range_end else None,
                    )
                    or {}
                )
                churn_count = _as_int(churn_stats.get("churn_count"))
                churn_rate = _as_float(churn_stats.get("churn_rate"))
        except Exception as e:
            logger.warning(f"Failed to load churn stats: {e}")

        churn_value = f"{churn_rate:.1f}%"
        churn_delta = _fmt_delta(churn_rate, prev_churn_rate, "pp")
        if single_event_scope:
            churn_value = "-"
            churn_delta = "N/A (multi-event trend)"

        # Game distribution leaderboard for selected scope
        game_counts = {}
        configured_games: Dict[str, Dict[str, int]] = {}
        total_entries = 0
        for ev in selected_events:
            breakdown = ev.get("games_breakdown")
            if isinstance(breakdown, dict):
                for game, count in breakdown.items():
                    cnt = _as_int(count)
                    if cnt <= 0:
                        continue
                    normalized_game = _normalize_game_name(game)
                    if not normalized_game:
                        continue
                    game_counts[normalized_game] = game_counts.get(normalized_game, 0) + cnt
                    total_entries += cnt

            snapshot = ev.get("startgg_snapshot")
            if isinstance(snapshot, dict):
                snapshot_events = snapshot.get("events") or []
                if isinstance(snapshot_events, list):
                    for snap_ev in snapshot_events:
                        if not isinstance(snap_ev, dict):
                            continue
                        normalized_game = _normalize_game_name(snap_ev.get("name"))
                        if not normalized_game:
                            continue
                        agg = configured_games.setdefault(
                            normalized_game,
                            {"registered": 0, "sets": 0, "games": 0, "configured": 0},
                        )
                        agg["registered"] += _as_int(snap_ev.get("numEntrants"))
                        agg["sets"] += _as_int(snap_ev.get("setCount"))
                        agg["games"] += _as_int(snap_ev.get("gamesPlayed"))
                        agg["configured"] += 1

        all_games = set(game_counts) | set(configured_games)
        sorted_games = sorted(
            all_games,
            key=lambda g: (
                -_as_int(game_counts.get(g)),
                -_as_int((configured_games.get(g) or {}).get("registered")),
                str(g).lower(),
            ),
        )
        games_rows = []
        for idx, game in enumerate(sorted_games[:20], start=1):
            cnt = _as_int(game_counts.get(game))
            cfg = configured_games.get(game) or {}
            registered = _as_int(cfg.get("registered"))
            set_count = _as_int(cfg.get("sets"))
            game_count = _as_int(cfg.get("games"))
            share = (cnt / total_entries * 100) if total_entries > 0 else 0.0
            # Use games_played as strongest signal for "actually ran".
            # set_count can be non-zero even when all sets were DQ/never scored.
            if game_count > 0:
                run_status = "Played"
            elif registered > 0 and set_count > 0:
                run_status = "No games played"
            elif registered > 0:
                run_status = "Registered only"
            elif cfg:
                run_status = "No entrants"
            elif cnt > 0:
                run_status = "Check-ins only"
            else:
                run_status = "-"
            games_rows.append(
                {
                    "rank": idx,
                    "game": game,
                    "entries": cnt,
                    "registered": registered,
                    "sets_played": set_count,
                    "games_played": game_count,
                    "run_status": run_status,
                    "share": f"{share:.0f}%",
                }
            )
        games_title = f"Game distribution ({len(games_rows)} shown)"

        # Game trend figure (top games across selected events)
        trend_events = sorted(
            selected_events,
            key=lambda ev: (
                _as_date(ev.get("event_date")) or date.min,
                str(ev.get("event_slug") or ""),
            ),
        )
        normalized_trend_breakdowns: List[Dict[str, int]] = []
        trend_totals: Dict[str, int] = {}
        for ev in trend_events:
            breakdown = ev.get("games_breakdown")
            normalized_breakdown: Dict[str, int] = {}
            if isinstance(breakdown, dict):
                for game, count in breakdown.items():
                    cnt = _as_int(count)
                    if cnt <= 0:
                        continue
                    normalized_game = _normalize_game_name(game)
                    if not normalized_game:
                        continue
                    normalized_breakdown[normalized_game] = (
                        normalized_breakdown.get(normalized_game, 0) + cnt
                    )
                    trend_totals[normalized_game] = trend_totals.get(normalized_game, 0) + cnt
            normalized_trend_breakdowns.append(normalized_breakdown)

        trend_games = [
            g for g, _ in sorted(trend_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        ]
        trend_x = []
        trend_hover_labels: List[str] = []
        for ev in trend_events:
            ev_name = ev.get("event_display_name") or ev.get("event_slug") or "event"
            ev_date = _as_date(ev.get("event_date"))
            if ev_date:
                trend_x.append(ev_date.isoformat())
                trend_hover_labels.append(f"{ev_name} ({ev_date.isoformat()})")
            else:
                trend_x.append(str(ev_name))
                trend_hover_labels.append(str(ev_name))
        trend_traces = []
        palette = ["#22d3ee", "#34d399", "#f59e0b", "#f87171", "#a78bfa"]
        for idx, game in enumerate(trend_games):
            ys: List[int] = []
            for breakdown in normalized_trend_breakdowns:
                ys.append(_as_int(breakdown.get(game)))
            trend_traces.append(
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": game,
                    "x": trend_x,
                    "y": ys,
                    "customdata": [[lbl] for lbl in trend_hover_labels],
                    "hovertemplate": "%{customdata[0]}<br>%{fullData.name}: %{y}<extra></extra>",
                    "line": {"width": 2, "color": palette[idx % len(palette)]},
                    "marker": {"size": 6},
                }
            )

        games_trend_figure = {
            "data": trend_traces,
            "layout": {
                "paper_bgcolor": "#0f172a",
                "plot_bgcolor": "#0f172a",
                "font": {"color": "#cbd5e1", "size": 11},
                "margin": {"l": 36, "r": 12, "t": 24, "b": 62},
                "legend": {"orientation": "h", "y": 1.18, "x": 0},
                "xaxis": {"showgrid": False, "tickangle": -12, "automargin": True},
                "yaxis": {"showgrid": True, "gridcolor": "#1e293b", "rangemode": "tozero"},
            },
        }

        # Biggest mover (second half vs first half)
        game_mover_text = "Biggest mover: -"
        if len(trend_events) >= 2:
            mid = max(len(trend_events) // 2, 1)
            first_half = trend_events[:mid]
            second_half = trend_events[mid:]
            first_counts: Dict[str, int] = {}
            second_counts: Dict[str, int] = {}

            for ev in first_half:
                breakdown = ev.get("games_breakdown")
                if isinstance(breakdown, dict):
                    for game, count in breakdown.items():
                        normalized_game = _normalize_game_name(game)
                        if not normalized_game:
                            continue
                        first_counts[normalized_game] = first_counts.get(
                            normalized_game, 0
                        ) + _as_int(count)

            for ev in second_half:
                breakdown = ev.get("games_breakdown")
                if isinstance(breakdown, dict):
                    for game, count in breakdown.items():
                        normalized_game = _normalize_game_name(game)
                        if not normalized_game:
                            continue
                        second_counts[normalized_game] = second_counts.get(
                            normalized_game, 0
                        ) + _as_int(count)

            all_games = set(first_counts) | set(second_counts)
            mover_game = None
            mover_diff = 0
            first_den = max(len(first_half), 1)
            second_den = max(len(second_half), 1)
            for game in all_games:
                first_avg = first_counts.get(game, 0) / first_den
                second_avg = second_counts.get(game, 0) / second_den
                diff = second_avg - first_avg
                if abs(diff) > abs(mover_diff):
                    mover_game = game
                    mover_diff = diff

            if mover_game:
                sign = "+" if mover_diff > 0 else ""
                game_mover_text = (
                    f"Biggest mover: {mover_game} "
                    f"({sign}{mover_diff:.1f} avg entries/event vs previous half)"
                )

        # Game crossover (top pairs)
        crossover_rows: List[Dict[str, Any]] = []
        crossover_title = "Game crossover (top pairs)"
        try:
            crossover_fn = getattr(storage_api, "get_game_crossover_stats", None)
            if crossover_fn:
                crossover_stats = (
                    crossover_fn(
                        event_slugs=selected_scope_slugs or None,
                        start_date=range_start.isoformat() if range_start else None,
                        end_date=range_end.isoformat() if range_end else None,
                        limit=20,
                    )
                    or []
                )
                crossover_merged: Dict[tuple, int] = {}
                for row in crossover_stats:
                    game_a = _normalize_game_name(row.get("game_a"))
                    game_b = _normalize_game_name(row.get("game_b"))
                    if not game_a or not game_b or game_a == game_b:
                        continue
                    a, b = sorted([game_a, game_b])
                    key = (a, b)
                    crossover_merged[key] = crossover_merged.get(key, 0) + _as_int(
                        row.get("shared_players")
                    )

                merged_rows = sorted(
                    crossover_merged.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
                )
                for idx, (pair, shared) in enumerate(merged_rows, start=1):
                    game_a, game_b = pair
                    share = (shared / metrics["total"] * 100) if metrics["total"] > 0 else 0.0
                    crossover_rows.append(
                        {
                            "rank": idx,
                            "game_a": game_a,
                            "game_b": game_b,
                            "shared_players": shared,
                            "share": f"{share:.0f}%",
                        }
                    )
                crossover_title = f"Game crossover ({len(crossover_rows)} shown)"
        except Exception as e:
            logger.warning(f"Failed to load game crossover stats: {e}")

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
            unique_text,
            total_delta,
            revenue_delta,
            ready_delta,
            member_delta,
            guest_delta,
            startgg_delta,
            retention_delta,
            top_game_text,
            added_via_summary,
            ops_live_note,
            top_players_title,
            top_players_rows,
            funnel_note,
            funnel_cards,
            games_title,
            games_rows,
            game_mover_text,
            games_trend_figure,
            crossover_title,
            crossover_rows,
            table_rows,
            earnings_rows,
            str(slots),
            f"{avg_games:.1f}",
            f"{multigame_pct:.1f}%",
            str(new_players),
            str(returning_players),
            f"{noshow_rate:.1f}%",
            f"{manual_share:.1f}%",
            checkin_speed_value,
            duration_value,
            growth_value,
            churn_value,
            slots_delta,
            avggames_delta,
            multigame_delta,
            new_delta,
            returning_delta,
            noshow_delta,
            manual_delta,
            checkin_speed_delta,
            duration_delta,
            growth_delta,
            churn_delta,
            str(core_players),
            f"{player_lifetime:.1f}",
            coreplayers_delta,
            lifetime_delta,
            summary_core,
            summary_community,
            summary_tournament,
            summary_operations,
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
        Output("insights-kpi-category-filter", "value"),
        Input("insights-summary-all", "n_clicks"),
        Input("insights-summary-core", "n_clicks"),
        Input("insights-summary-community", "n_clicks"),
        Input("insights-summary-tournament", "n_clicks"),
        Input("insights-summary-operations", "n_clicks"),
        prevent_initial_call=True,
    )
    def select_kpi_category_from_summary(_all, _core, _community, _tournament, _operations):
        triggered = ctx.triggered_id
        mapping = {
            "insights-summary-all": "all",
            "insights-summary-core": "core",
            "insights-summary-community": "community",
            "insights-summary-tournament": "tournament",
            "insights-summary-operations": "operations",
        }
        return mapping.get(triggered, no_update)

    @app.callback(
        Output("insights-summary-all", "className"),
        Output("insights-summary-core", "className"),
        Output("insights-summary-community", "className"),
        Output("insights-summary-tournament", "className"),
        Output("insights-summary-operations", "className"),
        Output("insights-kpi-label-core", "className"),
        Output("insights-kpi-label-community", "className"),
        Output("insights-kpi-label-tournament", "className"),
        Output("insights-kpi-label-operations", "className"),
        Output("insights-card-total", "className"),
        Output("insights-card-slots", "className"),
        Output("insights-card-ready", "className"),
        Output("insights-card-revenue", "className"),
        Output("insights-card-new", "className"),
        Output("insights-card-returning", "className"),
        Output("insights-card-coreplayers", "className"),
        Output("insights-card-lifetime", "className"),
        Output("insights-card-growth", "className"),
        Output("insights-card-churn", "className"),
        Output("insights-card-retention", "className"),
        Output("insights-card-guest", "className"),
        Output("insights-card-startgg", "className"),
        Output("insights-card-member", "className"),
        Output("insights-card-avggames", "className"),
        Output("insights-card-multigame", "className"),
        Output("insights-card-noshow", "className"),
        Output("insights-card-manual", "className"),
        Output("insights-card-checkinspeed", "className"),
        Output("insights-card-duration", "className"),
        Input("insights-kpi-category-filter", "value"),
        Input("insights-kpi-auto-visibility-toggle", "value"),
        Input("insights-event-dropdown", "value"),
    )
    def filter_insights_kpi_category(selected_category, visibility_flags, selected_event_slugs):
        mode = (selected_category or "all").strip().lower()
        if mode not in {"all", "core", "community", "tournament", "operations"}:
            mode = "all"

        auto_visibility = "auto" in (visibility_flags or [])
        if isinstance(selected_event_slugs, list):
            selected_count = len([s for s in selected_event_slugs if s])
            selected_slug = str(selected_event_slugs[0]) if selected_count == 1 else ""
        elif selected_event_slugs:
            selected_count = 1
            selected_slug = str(selected_event_slugs)
        else:
            selected_count = 0
            selected_slug = ""

        try:
            active_settings = get_active_settings() or {}
            active_slug = str(active_settings.get("active_event_slug") or "")
        except Exception:
            active_slug = ""

        single_event_scope = selected_count == 1
        single_active_scope = single_event_scope and selected_slug == active_slug

        show_core = mode in {"all", "core"}
        show_community = mode in {"all", "community"}
        show_tournament = mode in {"all", "tournament"}
        show_operations = mode in {"all", "operations"}

        summary_base = "stat-card-live insights-summary-card"
        all_summary = f"{summary_base} is-active" if mode == "all" else summary_base
        core_summary = f"{summary_base} is-active" if mode == "core" else summary_base
        community_summary = f"{summary_base} is-active" if mode == "community" else summary_base
        tournament_summary = f"{summary_base} is-active" if mode == "tournament" else summary_base
        operations_summary = f"{summary_base} is-active" if mode == "operations" else summary_base

        label_visible = "insights-kpi-section-label"
        label_hidden = "insights-kpi-section-label kpi-hidden"
        card_visible = "stat-card-live"
        card_hidden = "stat-card-live kpi-hidden"

        growth_class = card_visible if show_community else card_hidden
        churn_class = card_visible if show_community else card_hidden
        checkinspeed_class = card_visible if show_operations else card_hidden
        duration_class = card_visible if show_operations else card_hidden

        if auto_visibility:
            if single_event_scope:
                growth_class = card_hidden
                churn_class = card_hidden
            if not single_active_scope:
                checkinspeed_class = card_hidden
                duration_class = card_hidden

        return (
            all_summary,
            core_summary,
            community_summary,
            tournament_summary,
            operations_summary,
            label_visible if show_core else label_hidden,
            label_visible if show_community else label_hidden,
            label_visible if show_tournament else label_hidden,
            label_visible if show_operations else label_hidden,
            card_visible if show_core else card_hidden,
            card_visible if show_core else card_hidden,
            card_visible if show_core else card_hidden,
            card_visible if show_core else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            growth_class,
            churn_class,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_community else card_hidden,
            card_visible if show_tournament else card_hidden,
            card_visible if show_tournament else card_hidden,
            card_visible if show_tournament else card_hidden,
            card_visible if show_operations else card_hidden,
            checkinspeed_class,
            duration_class,
        )

    @app.callback(
        Output("insights-top-players-game-filter", "options"),
        Output("insights-top-players-game-filter", "value"),
        Input("insights-games-table", "data"),
        Input("insights-crossover-table", "data"),
        Input("insights-events-table", "data"),
        State("insights-top-players-game-filter", "value"),
    )
    def sync_top_players_game_filter_options(games_rows, crossover_rows, events_rows, current_value):
        games_rows = games_rows or []
        crossover_rows = crossover_rows or []
        events_rows = events_rows or []
        options = [{"label": "All games", "value": "all"}]

        seen = set()

        def _add_option(game_name: Any):
            game = str(game_name or "").strip()
            if not game or game in seen or game == "-":
                return
            seen.add(game)
            options.append({"label": game, "value": game})

        for row in games_rows:
            if not isinstance(row, dict):
                continue
            _add_option(row.get("game"))

        for row in crossover_rows:
            if not isinstance(row, dict):
                continue
            _add_option(row.get("game_a"))
            _add_option(row.get("game_b"))

        for row in events_rows:
            if not isinstance(row, dict):
                continue
            _add_option(row.get("top_game"))

        # Fallback canonical games so dropdown never collapses to only "All games".
        for fallback_game in [
            "SSBU Singles",
            "STREET FIGHTER 6 TOURNAMENT",
            "TEKKEN 8 TOURNAMENT",
        ]:
            _add_option(fallback_game)

        valid_values = {str(o.get("value")) for o in options}
        selected_value = str(current_value or "all")
        if selected_value not in valid_values:
            selected_value = "all"

        return options, selected_value

    @app.callback(
        Output("insights-games-popularity-wrap", "style"),
        Output("insights-games-crossover-wrap", "style"),
        Output("insights-games-trends-wrap", "style"),
        Input("insights-games-table-view", "value"),
    )
    def switch_games_table_view(view_mode):
        mode = str(view_mode or "distribution").lower()
        if mode in {"crossovers", "crossover", "crossover_heatmap", "crossover_table"}:
            return {"display": "none"}, {"display": "block"}, {"display": "none"}
        if mode == "trends":
            return {"display": "none"}, {"display": "none"}, {"display": "block"}
        return {"display": "block"}, {"display": "none"}, {"display": "none"}

    app.clientside_callback(
        """
        function(_mode) {
            var y = window.scrollY || window.pageYOffset || 0;
            setTimeout(function() { window.scrollTo(0, y); }, 0);
            setTimeout(function() { window.scrollTo(0, y); }, 120);
            return "";
        }
        """,
        Output("insights-games-scroll-lock", "children"),
        Input("insights-games-table-view", "value"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("insights-crossover-heatmap-wrap", "style"),
        Output("insights-crossover-table-wrap", "style"),
        Input("insights-games-table-view", "value"),
    )
    def switch_crossover_visualization(view_mode):
        mode = str(view_mode or "distribution").lower()
        if mode in {"crossovers", "crossover", "crossover_heatmap", "crossover_table"}:
            return {"display": "block"}, {"display": "block"}
        return {"display": "none"}, {"display": "none"}

    @app.callback(
        Output("insights-events-overview-wrap", "style"),
        Output("insights-events-ops-wrap", "style"),
        Input("insights-events-table-view", "value"),
    )
    def switch_events_table_view(view_mode):
        mode = str(view_mode or "overview").lower()
        if mode == "ops_quality":
            return {"display": "none"}, {"display": "block"}
        return {"display": "block"}, {"display": "none"}

    @app.callback(
        Output("insights-events-ops-table", "data"),
        Input("insights-events-table", "data"),
    )
    def build_events_ops_table(rows):
        rows = rows or []

        def _to_float(value: Any) -> float:
            text = str(value or "").strip().replace("%", "")
            try:
                return float(text) if text else 0.0
            except Exception:
                return 0.0

        def _parse_checkin_conversion(value: Any) -> float:
            text = str(value or "").strip()
            if "/" not in text:
                return 0.0
            left, right = text.split("/", 1)
            try:
                checked_in = int(left.strip() or 0)
                registered = int(right.strip() or 0)
                return (checked_in / registered * 100.0) if registered > 0 else 0.0
            except Exception:
                return 0.0

        def _noshow_band(rate: float) -> str:
            if rate < 8:
                return "Low"
            if rate < 18:
                return "Normal"
            if rate < 30:
                return "Elevated"
            if rate < 40:
                return "High"
            return "Critical"

        ops_rows: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            no_show_rate = _to_float(row.get("no_show_rate"))
            manual_share = _to_float(row.get("manual_share"))
            checkin_conversion = _parse_checkin_conversion(row.get("checked_in_vs_registered"))

            flags: List[str] = []
            if no_show_rate >= 30:
                flags.append("High no-show")
            elif no_show_rate >= 18:
                flags.append("Elevated no-show")
            if checkin_conversion > 0 and checkin_conversion < 75:
                flags.append("Low check-in conversion")
            if manual_share >= 30:
                flags.append("High manual share")
            elif manual_share >= 15:
                flags.append("Elevated manual share")

            ops_rows.append(
                {
                    "event_display_name": row.get("event_display_name", ""),
                    "event_date": row.get("event_date", ""),
                    "checked_in_vs_registered": row.get("checked_in_vs_registered", "-"),
                    "no_show_count": row.get("no_show_count", 0),
                    "no_show_rate": round(no_show_rate, 1),
                    "no_show_band": _noshow_band(no_show_rate),
                    "manual_count": row.get("manual_count", 0),
                    "manual_share": row.get("manual_share", "-"),
                    "event_quality": row.get("event_quality", "-"),
                    "ops_flag": " | ".join(flags) if flags else "OK",
                }
            )

        return ops_rows

    @app.callback(
        Output("insights-games-pie", "figure"),
        Output("insights-games-pie-legend", "children"),
        Input("insights-games-table", "data"),
    )
    def render_games_popularity_pie(rows):
        rows = rows or []

        def _to_int(value: Any) -> int:
            try:
                return int(value or 0)
            except Exception:
                return 0

        labels: List[str] = []
        entries_values: List[int] = []
        sets_values: List[int] = []
        games_values: List[int] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            game_name = str(row.get("game") or "").strip()
            entries = _to_int(row.get("entries"))
            if not game_name or entries <= 0:
                continue
            labels.append(game_name)
            entries_values.append(entries)
            sets_values.append(_to_int(row.get("sets_played")))
            games_values.append(_to_int(row.get("games_played")))

        total_entries = sum(entries_values)
        total_sets = sum(sets_values)
        total_games = sum(games_values)

        if total_entries <= 0:
            empty_figure = {
                "data": [],
                "layout": {
                    "paper_bgcolor": "#0f172a",
                    "plot_bgcolor": "#0f172a",
                    "font": {"color": "#cbd5e1", "size": 11},
                    "margin": {"l": 10, "r": 10, "t": 28, "b": 10},
                    "annotations": [
                        {
                            "text": "No game data",
                            "x": 0.5,
                            "y": 0.5,
                            "showarrow": False,
                            "font": {"size": 12, "color": "#64748b"},
                        }
                    ],
                },
            }
            empty_legend = html.Div(
                "No game data in selected scope.",
                style={"color": "#64748b", "fontSize": "0.8rem"},
            )
            return empty_figure, empty_legend

        palette = [
            "#22d3ee",
            "#34d399",
            "#f59e0b",
            "#f87171",
            "#a78bfa",
            "#60a5fa",
            "#fb7185",
            "#2dd4bf",
            "#fbbf24",
            "#38bdf8",
        ]
        colors = [palette[idx % len(palette)] for idx in range(len(labels))]

        hover_texts: List[str] = []
        for idx, _label in enumerate(labels):
            share = (entries_values[idx] / total_entries * 100.0) if total_entries > 0 else 0.0
            hover_texts.append(
                "<b>{}</b><br>Check-ins: {}<br>Share: {:.1f}%<br>Sets played: {}<br>Games played: {}".format(
                    labels[idx],
                    entries_values[idx],
                    share,
                    sets_values[idx],
                    games_values[idx],
                )
            )

        figure = {
            "data": [
                {
                    "type": "pie",
                    "labels": labels,
                    "values": entries_values,
                    "hole": 0,
                    "sort": False,
                    "direction": "clockwise",
                    "marker": {
                        "colors": colors,
                        "line": {"color": "#0b1220", "width": 1.5},
                    },
                    "hovertext": hover_texts,
                    "textinfo": "percent",
                    "textfont": {"size": 11, "color": "#e2e8f0"},
                    "hovertemplate": "%{hovertext}<extra></extra>",
                    "showlegend": False,
                }
            ],
            "layout": {
                "paper_bgcolor": "#0f172a",
                "plot_bgcolor": "#0f172a",
                "font": {"color": "#cbd5e1", "size": 11},
                "margin": {"l": 8, "r": 8, "t": 16, "b": 8},
            },
        }

        legend_children = [
            html.Div(
                style={
                    "display": "flex",
                    "alignItems": "center",
                    "gap": "0.4rem",
                    "flexWrap": "wrap",
                    "marginBottom": "0.65rem",
                },
                children=[
                    html.Span(
                        f"Total sets: {total_sets}",
                        style={
                            "padding": "0.08rem 0.45rem",
                            "borderRadius": "999px",
                            "border": "1px solid #334155",
                            "color": "#93c5fd",
                            "fontSize": "0.72rem",
                            "fontWeight": "600",
                        },
                    ),
                    html.Span(
                        f"Total games: {total_games}",
                        style={
                            "padding": "0.08rem 0.45rem",
                            "borderRadius": "999px",
                            "border": "1px solid #334155",
                            "color": "#86efac",
                            "fontSize": "0.72rem",
                            "fontWeight": "600",
                        },
                    ),
                ],
            )
        ]

        for idx, game in enumerate(labels):
            share = (entries_values[idx] / total_entries * 100.0) if total_entries > 0 else 0.0
            legend_children.append(
                html.Div(
                    style={
                        "display": "grid",
                        "gridTemplateColumns": "12px 1fr auto",
                        "alignItems": "center",
                        "gap": "0.5rem",
                        "padding": "0.22rem 0",
                        "borderBottom": "1px solid #1e293b",
                    },
                    children=[
                        html.Span(
                            style={
                                "display": "inline-block",
                                "width": "10px",
                                "height": "10px",
                                "borderRadius": "999px",
                                "backgroundColor": colors[idx],
                            }
                        ),
                        html.Span(
                            game,
                            style={
                                "color": "#cbd5e1",
                                "fontSize": "0.78rem",
                                "whiteSpace": "nowrap",
                                "overflow": "hidden",
                                "textOverflow": "ellipsis",
                            },
                            title=game,
                        ),
                        html.Span(
                            f"{share:.0f}% · S{sets_values[idx]} G{games_values[idx]}",
                            style={
                                "color": "#94a3b8",
                                "fontSize": "0.74rem",
                                "fontVariantNumeric": "tabular-nums",
                            },
                        ),
                    ],
                )
            )

        return figure, legend_children

    @app.callback(
        Output("insights-crossover-heatmap", "figure"),
        Input("insights-crossover-table", "data"),
        Input("insights-games-table", "data"),
    )
    def render_crossover_heatmap(crossover_rows, games_rows):
        crossover_rows = crossover_rows or []
        games_rows = games_rows or []

        def _to_int(value: Any) -> int:
            try:
                return int(value or 0)
            except Exception:
                return 0

        game_order: List[str] = []
        for row in games_rows:
            if not isinstance(row, dict):
                continue
            game_name = str(row.get("game") or "").strip()
            if game_name and game_name not in game_order:
                game_order.append(game_name)

        pair_values: Dict[tuple, int] = {}
        pair_share: Dict[tuple, str] = {}
        for row in crossover_rows:
            if not isinstance(row, dict):
                continue
            game_a = str(row.get("game_a") or "").strip()
            game_b = str(row.get("game_b") or "").strip()
            if not game_a or not game_b or game_a == game_b:
                continue
            if game_a not in game_order:
                game_order.append(game_a)
            if game_b not in game_order:
                game_order.append(game_b)
            key = tuple(sorted([game_a, game_b]))
            pair_values[key] = max(pair_values.get(key, 0), _to_int(row.get("shared_players")))
            pair_share[key] = str(row.get("share") or "")

        if not game_order:
            return {
                "data": [],
                "layout": {
                    "paper_bgcolor": "#0f172a",
                    "plot_bgcolor": "#0f172a",
                    "font": {"color": "#cbd5e1", "size": 11},
                    "margin": {"l": 10, "r": 10, "t": 24, "b": 10},
                    "annotations": [
                        {
                            "text": "No crossover data",
                            "x": 0.5,
                            "y": 0.5,
                            "showarrow": False,
                            "font": {"size": 12, "color": "#64748b"},
                        }
                    ],
                },
            }

        game_entries: Dict[str, int] = {}
        for row in games_rows:
            if not isinstance(row, dict):
                continue
            game_name = str(row.get("game") or "").strip()
            if not game_name:
                continue
            game_entries[game_name] = _to_int(row.get("entries"))

        n = len(game_order)
        z_matrix: List[List[int]] = [[0 for _ in range(n)] for _ in range(n)]
        share_matrix: List[List[str]] = [["" for _ in range(n)] for _ in range(n)]

        for i, game_i in enumerate(game_order):
            z_matrix[i][i] = game_entries.get(game_i, 0)
            share_matrix[i][i] = ""
            for j in range(i + 1, n):
                game_j = game_order[j]
                key = tuple(sorted([game_i, game_j]))
                shared = _to_int(pair_values.get(key, 0))
                share_txt = pair_share.get(key, "")
                z_matrix[i][j] = shared
                z_matrix[j][i] = shared
                share_matrix[i][j] = share_txt
                share_matrix[j][i] = share_txt

        annotations: List[Dict[str, Any]] = []
        if n <= 8:
            for i in range(n):
                for j in range(n):
                    value = z_matrix[i][j]
                    if value <= 0:
                        continue
                    annotations.append(
                        {
                            "x": game_order[j],
                            "y": game_order[i],
                            "text": str(value),
                            "showarrow": False,
                            "font": {"size": 10, "color": "#e2e8f0"},
                        }
                    )

        return {
            "data": [
                {
                    "type": "heatmap",
                    "x": game_order,
                    "y": game_order,
                    "z": z_matrix,
                    "customdata": share_matrix,
                    "colorscale": [
                        [0.0, "#0b1220"],
                        [0.2, "#172554"],
                        [0.4, "#1d4ed8"],
                        [0.7, "#0891b2"],
                        [1.0, "#22d3ee"],
                    ],
                    "hovertemplate": (
                        "<b>%{y}</b> + <b>%{x}</b><br>"
                        "Shared players: %{z}<br>"
                        "Scope share: %{customdata}"
                        "<extra></extra>"
                    ),
                    "showscale": True,
                    "colorbar": {
                        "title": "Shared",
                        "titlefont": {"size": 10, "color": "#94a3b8"},
                        "tickfont": {"size": 10, "color": "#94a3b8"},
                    },
                }
            ],
            "layout": {
                "paper_bgcolor": "#0f172a",
                "plot_bgcolor": "#0f172a",
                "font": {"color": "#cbd5e1", "size": 11},
                "margin": {"l": 84, "r": 24, "t": 18, "b": 76},
                "xaxis": {"tickangle": -22, "automargin": True, "side": "bottom"},
                "yaxis": {"automargin": True, "autorange": "reversed"},
                "annotations": annotations,
            },
        }

    @app.callback(
        Output("insights-events-noshow-trend", "figure"),
        Input("insights-events-table", "data"),
    )
    def render_events_noshow_trend(rows):
        rows = rows or []

        points: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_text = str(row.get("event_date") or "").strip()
            if not date_text:
                continue
            try:
                y_val = float(row.get("no_show_rate") or 0.0)
            except Exception:
                y_val = 0.0
            points.append(
                {
                    "event": str(row.get("event_display_name") or "event"),
                    "date": date_text,
                    "y": y_val,
                    "count": int(row.get("no_show_count") or 0),
                    "participants": int(row.get("total_participants") or 0),
                }
            )

        points.sort(key=lambda p: p["date"])

        if not points:
            return {
                "data": [],
                "layout": {
                    "paper_bgcolor": "#0f172a",
                    "plot_bgcolor": "#0f172a",
                    "font": {"color": "#cbd5e1", "size": 11},
                    "margin": {"l": 36, "r": 12, "t": 10, "b": 30},
                    "annotations": [
                        {
                            "text": "No event data",
                            "x": 0.5,
                            "y": 0.5,
                            "showarrow": False,
                            "font": {"size": 12, "color": "#64748b"},
                        }
                    ],
                },
            }

        x_vals = [p["date"] for p in points]
        no_show_vals = [p["y"] for p in points]
        participant_vals = [p["participants"] for p in points]
        no_show_hover = [
            (
                f"<b>{p['event']}</b><br>Date: {p['date']}"
                f"<br>No-show: {p['y']:.1f}%<br>No-shows: {p['count']}"
            )
            for p in points
        ]
        participant_hover = [
            (
                f"<b>{p['event']}</b><br>Date: {p['date']}"
                f"<br>Participants: {p['participants']}<br>No-show: {p['y']:.1f}%"
            )
            for p in points
        ]

        return {
            "data": [
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "x": x_vals,
                    "y": no_show_vals,
                    "line": {"color": "#38bdf8", "width": 2},
                    "marker": {
                        "size": 7,
                        "color": "#0f172a",
                        "line": {"width": 2, "color": "#38bdf8"},
                    },
                    "hovertext": no_show_hover,
                    "hovertemplate": "%{hovertext}<extra></extra>",
                    "name": "No-show %",
                    "yaxis": "y",
                },
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "x": x_vals,
                    "y": participant_vals,
                    "line": {"color": "#34d399", "width": 2},
                    "marker": {
                        "size": 6,
                        "color": "#0f172a",
                        "line": {"width": 2, "color": "#34d399"},
                    },
                    "hovertext": participant_hover,
                    "hovertemplate": "%{hovertext}<extra></extra>",
                    "name": "Participants",
                    "yaxis": "y2",
                }
            ],
            "layout": {
                "paper_bgcolor": "#0f172a",
                "plot_bgcolor": "#0f172a",
                "font": {"color": "#cbd5e1", "size": 11},
                "margin": {"l": 44, "r": 16, "t": 8, "b": 48},
                "xaxis": {"showgrid": False, "tickangle": -20, "automargin": True},
                "yaxis": {
                    "title": "No-show %",
                    "ticksuffix": "%",
                    "rangemode": "tozero",
                    "showgrid": True,
                    "gridcolor": "#1e293b",
                },
                "yaxis2": {
                    "title": "Participants",
                    "overlaying": "y",
                    "side": "right",
                    "rangemode": "tozero",
                    "showgrid": False,
                },
                "legend": {"orientation": "h", "x": 0, "y": 1.16, "font": {"size": 10}},
            },
        }

    @app.callback(
        Output("insights-view-players", "style"),
        Output("insights-view-games", "style"),
        Output("insights-view-events", "style"),
        Output("insights-view-earnings", "style"),
        Output("insights-view-duplicates", "style"),
        Output("insights-top-game", "style"),
        Input("insights-subtabs", "value"),
    )
    def toggle_insights_focus_view(view_mode):
        base_visible = {"display": "block"}
        hidden = {"display": "none"}
        top_game_visible = {"display": "none"}
        mode = (view_mode or "").strip().lower()

        if mode == "players":
            return base_visible, hidden, hidden, hidden, hidden, hidden
        if mode == "games":
            return hidden, base_visible, hidden, hidden, hidden, top_game_visible
        if mode == "events":
            return hidden, hidden, base_visible, hidden, hidden, top_game_visible
        if mode == "earnings":
            return hidden, hidden, hidden, base_visible, hidden, hidden
        if mode == "duplicates":
            return hidden, hidden, hidden, hidden, base_visible, hidden
        return base_visible, hidden, hidden, hidden, hidden, hidden

    @app.callback(
        Output("insights-kpi-help", "children"),
        Input("insights-card-total", "n_clicks"),
        Input("insights-card-ready", "n_clicks"),
        Input("insights-card-member", "n_clicks"),
        Input("insights-card-guest", "n_clicks"),
        Input("insights-card-startgg", "n_clicks"),
        Input("insights-card-retention", "n_clicks"),
        Input("insights-card-revenue", "n_clicks"),
        Input("insights-card-slots", "n_clicks"),
        Input("insights-card-avggames", "n_clicks"),
        Input("insights-card-multigame", "n_clicks"),
        Input("insights-card-new", "n_clicks"),
        Input("insights-card-returning", "n_clicks"),
        Input("insights-card-coreplayers", "n_clicks"),
        Input("insights-card-lifetime", "n_clicks"),
        Input("insights-card-growth", "n_clicks"),
        Input("insights-card-churn", "n_clicks"),
        Input("insights-card-noshow", "n_clicks"),
        Input("insights-card-manual", "n_clicks"),
        Input("insights-card-checkinspeed", "n_clicks"),
        Input("insights-card-duration", "n_clicks"),
    )
    def show_kpi_help(
        _total,
        _ready,
        _member,
        _guest,
        _startgg,
        _retention,
        _revenue,
        _slots,
        _avggames,
        _multigame,
        _new,
        _returning,
        _coreplayers,
        _lifetime,
        _growth,
        _churn,
        _noshow,
        _manual,
        _checkinspeed,
        _duration,
    ):
        help_map = {
            "insights-card-total": "Participants: total participant entries in selected period/scope.",
            "insights-card-ready": "Ready Rate: ready participants divided by total participants at archive time.",
            "insights-card-member": "Member Rate: members divided by total participants.",
            "insights-card-guest": "Guest Share: guests divided by total participants.",
            "insights-card-startgg": "Start.gg Account Rate (excl guests): (startgg_count - guest_count) divided by total participants.",
            "insights-card-retention": "Retention: returning-player share, weighted by event size.",
            "insights-card-revenue": "Total Revenue: summed event revenue in selected scope.",
            "insights-card-slots": "Checked-in Slots: total game entries for checked-in participants (3 games = 3 slots).",
            "insights-card-avggames": "Avg Games / Player: total slots divided by total participants.",
            "insights-card-multigame": "Multi-Game Players: share of participants playing 2+ games.",
            "insights-card-new": "New Players: first-time participants in selected scope.",
            "insights-card-returning": "Returning: players who attended at least one previous event.",
            "insights-card-coreplayers": "Core Players: players with 3+ distinct events in the rolling 6-month window.",
            "insights-card-lifetime": "Player Lifetime: average total events attended by players in selected scope.",
            "insights-card-growth": "Growth Rate: unique attendee growth vs previous period of same length.",
            "insights-card-churn": "Churn Rate: scoped players whose last_seen is older than 3 months from anchor date.",
            "insights-card-noshow": "No-Show Rate: Start.gg-registered players who did not check in.",
            "insights-card-manual": "Manual Share: check-ins added manually via dashboard vs total.",
            "insights-card-checkinspeed": "Check-in Speed: checked-in participants per minute from check-in opened timestamp.",
            "insights-card-duration": "Tournament Duration: elapsed time between event start and end timestamps.",
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
    # Duplicates - scan, merge, undo
    # -------------------------------------------------------------------------
    @app.callback(
        Output("duplicates-list", "children"),
        Output("duplicates-feedback", "children"),
        Output("duplicates-title", "children"),
        Input("btn-scan-duplicates", "n_clicks"),
        prevent_initial_call=True,
    )
    def scan_duplicates(n_clicks):
        if not n_clicks:
            return no_update, no_update, no_update
        return _build_candidates_list()

    @app.callback(
        Output("merge-confirm-dialog", "displayed"),
        Output("merge-confirm-dialog", "message"),
        Output("merge-keep-uuid", "data"),
        Output("merge-remove-uuid", "data"),
        Input({"type": "btn-merge", "keep": ALL, "remove": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def prompt_merge_confirm(n_clicks_list):
        if not n_clicks_list or not any(n_clicks_list):
            return False, "", None, None

        triggered = ctx.triggered_id
        if not triggered:
            return False, "", None, None

        keep_uuid = triggered.get("keep", "")
        remove_uuid = triggered.get("remove", "")

        msg = (
            f"Merge players?\n\n"
            f"KEEP: {keep_uuid[:12]}...\n"
            f"REMOVE: {remove_uuid[:12]}...\n\n"
            f"The removed player's history will be absorbed into the kept player. "
            f"This can be undone from the merge history."
        )

        return True, msg, keep_uuid, remove_uuid

    @app.callback(
        Output("duplicates-feedback", "children", allow_duplicate=True),
        Output("merge-history-list", "children", allow_duplicate=True),
        Output("duplicates-list", "children", allow_duplicate=True),
        Output("duplicates-title", "children", allow_duplicate=True),
        Input("merge-confirm-dialog", "submit_n_clicks"),
        State("merge-keep-uuid", "data"),
        State("merge-remove-uuid", "data"),
        prevent_initial_call=True,
    )
    def execute_merge(submit_clicks, keep_uuid, remove_uuid):
        if not submit_clicks or not keep_uuid or not remove_uuid:
            return no_update, no_update, no_update, no_update

        try:
            merge_fn = getattr(storage_api, "merge_players", None)
            if not merge_fn:
                return "Merge not available.", no_update, no_update, no_update

            result = merge_fn(
                keep_uuid,
                remove_uuid,
                reason="Manual merge via dashboard",
            )
            feedback = html.Div(
                [
                    html.Span(
                        "Merged! ",
                        style={"color": COLORS["accent_green"], "fontWeight": "600"},
                    ),
                    html.Span(
                        f"Kept {result.get('keep_player_tag', '?')}, "
                        f"removed {result.get('removed_player_tag', '?')}. "
                        f"Archive rows updated: {result.get('archive_rows_updated', 0)}.",
                    ),
                ],
                style={"fontSize": "0.8rem"},
            )
        except ValueError as e:
            # Player already merged/removed — not a crash, just stale UI
            feedback = html.Div(
                f"Already handled: {e}",
                style={"color": COLORS["accent_yellow"], "fontSize": "0.8rem"},
            )
        except Exception as e:
            logger.exception(f"Merge failed: {e}")
            feedback = html.Div(
                f"Merge failed: {e}",
                style={"color": COLORS["accent_red"], "fontSize": "0.8rem"},
            )

        # Auto-refresh both history and candidate list
        history_children = _build_merge_history_list()
        candidates_list, _, candidates_title = _build_candidates_list()

        return feedback, history_children, candidates_list, candidates_title

    @app.callback(
        Output("duplicates-feedback", "children", allow_duplicate=True),
        Output("merge-history-list", "children", allow_duplicate=True),
        Output("duplicates-list", "children", allow_duplicate=True),
        Output("duplicates-title", "children", allow_duplicate=True),
        Input({"type": "btn-undo-merge", "merge_id": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def undo_merge_callback(n_clicks_list):
        if not n_clicks_list or not any(n_clicks_list):
            return no_update, no_update, no_update, no_update

        triggered = ctx.triggered_id
        if not triggered:
            return no_update, no_update, no_update, no_update

        merge_id = triggered.get("merge_id")
        if not merge_id:
            return no_update, no_update, no_update, no_update

        try:
            undo_fn = getattr(storage_api, "undo_merge", None)
            if not undo_fn:
                return "Undo not available.", no_update, no_update, no_update

            result = undo_fn(int(merge_id))
            feedback = html.Div(
                [
                    html.Span(
                        "Undo complete! ",
                        style={"color": COLORS["accent_yellow"], "fontWeight": "600"},
                    ),
                    html.Span(
                        f"Restored {result.get('restored_tag', '?')} ({result.get('restored_name', '?')}). "
                        f"Archive rows reverted: {result.get('archive_reverted', 0)}.",
                    ),
                ],
                style={"fontSize": "0.8rem"},
            )
        except Exception as e:
            logger.exception(f"Undo merge failed: {e}")
            feedback = html.Div(
                f"Undo failed: {e}",
                style={"color": COLORS["accent_red"], "fontSize": "0.8rem"},
            )

        history_children = _build_merge_history_list()
        candidates_list, _, candidates_title = _build_candidates_list()
        return feedback, history_children, candidates_list, candidates_title

    def _build_merge_history_list():
        """Build merge history UI from merge_log."""
        try:
            history_fn = getattr(storage_api, "get_merge_history", None)
            if not history_fn:
                return [html.Span("Not available.", style={"color": COLORS["text_secondary"]})]
            history = history_fn(limit=20) or []
        except Exception:
            return [html.Span("Failed to load.", style={"color": COLORS["text_secondary"]})]

        if not history:
            return [
                html.Span(
                    "No merges yet.",
                    style={"color": COLORS["text_secondary"], "fontSize": "0.8rem"},
                )
            ]

        items = []
        for entry in history:
            is_undone = entry.get("undone", False)
            merged_at = (entry.get("merged_at") or "")[:16].replace("T", " ")

            text = (
                f"{merged_at}  —  "
                f"Removed {entry.get('removed_tag', '?')} ({entry.get('removed_name', '?')}) "
                f"into {entry.get('keep_uuid', '?')[:8]}... "
                f"({entry.get('archive_rows_updated', 0)} rows)"
            )

            row_children = [
                html.Span(
                    text,
                    style={
                        "fontSize": "0.75rem",
                        "color": COLORS["text_secondary"] if is_undone else COLORS["text_primary"],
                        "textDecoration": "line-through" if is_undone else "none",
                    },
                ),
            ]

            if is_undone:
                row_children.append(
                    html.Span(
                        "  (undone)",
                        style={
                            "color": COLORS["accent_yellow"],
                            "fontSize": "0.7rem",
                            "fontStyle": "italic",
                        },
                    )
                )
            else:
                row_children.append(
                    html.Button(
                        "Undo",
                        id={"type": "btn-undo-merge", "merge_id": entry["id"]},
                        style={
                            "backgroundColor": "transparent",
                            "color": COLORS["accent_yellow"],
                            "border": f"1px solid {COLORS['accent_yellow']}",
                            "padding": "0.15rem 0.5rem",
                            "borderRadius": "4px",
                            "cursor": "pointer",
                            "fontSize": "0.68rem",
                            "marginLeft": "0.5rem",
                        },
                    )
                )

            items.append(
                html.Div(
                    row_children,
                    style={
                        "padding": "0.3rem 0",
                        "borderBottom": f"1px solid {COLORS['border']}",
                        "display": "flex",
                        "alignItems": "center",
                    },
                )
            )

        return items

    def _build_candidates_list():
        """Re-scan and build the candidates card list. Returns (cards, feedback, title)."""
        try:
            candidates_fn = getattr(storage_api, "find_duplicate_candidates", None)
            if not candidates_fn:
                return [], "", "Potential duplicates"
            candidates = candidates_fn(limit=30) or []
        except Exception:
            return [], "", "Potential duplicates"

        if not candidates:
            return (
                [
                    html.Div(
                        "No potential duplicates found.",
                        style={"color": COLORS["accent_green"], "padding": "1rem 0"},
                    )
                ],
                "",
                "Potential duplicates (0)",
            )

        cards = []
        for c in candidates:
            a = c["player_a"]
            b = c["player_b"]
            conf = c["confidence"]
            reasons = c["reasons"]

            conf_color = {
                "high": COLORS["accent_green"],
                "medium": COLORS["accent_yellow"],
                "low": COLORS["text_secondary"],
            }.get(conf, COLORS["text_secondary"])

            reason_text = ", ".join(r.replace("_", " ") for r in reasons)

            card = html.Div(
                style={
                    "backgroundColor": COLORS["bg_card"],
                    "border": f"1px solid {COLORS['border']}",
                    "borderRadius": "8px",
                    "padding": "0.8rem 1rem",
                    "marginBottom": "0.6rem",
                },
                children=[
                    html.Div(
                        style={
                            "display": "flex",
                            "justifyContent": "space-between",
                            "alignItems": "flex-start",
                            "gap": "1rem",
                        },
                        children=[
                            html.Div(
                                style={"flex": "1"},
                                children=[
                                    html.Div(
                                        a.get("name", "?"),
                                        style={
                                            "color": COLORS["text_primary"],
                                            "fontWeight": "600",
                                            "fontSize": "0.85rem",
                                        },
                                    ),
                                    html.Div(
                                        f"Tag: {a.get('tag', '-')}",
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.75rem",
                                        },
                                    ),
                                    html.Div(
                                        f"Phone: {a.get('telephone', '-') or '-'}  |  "
                                        f"Events: {a.get('total_events', 0)}",
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.72rem",
                                        },
                                    ),
                                ],
                            ),
                            html.Div(
                                style={
                                    "display": "flex",
                                    "flexDirection": "column",
                                    "alignItems": "center",
                                    "minWidth": "80px",
                                },
                                children=[
                                    html.Div(
                                        "\u2194",
                                        style={
                                            "fontSize": "1.2rem",
                                            "color": COLORS["text_secondary"],
                                        },
                                    ),
                                    html.Div(
                                        conf.upper(),
                                        style={
                                            "color": conf_color,
                                            "fontSize": "0.65rem",
                                            "fontWeight": "700",
                                            "letterSpacing": "0.05em",
                                        },
                                    ),
                                    html.Div(
                                        reason_text,
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.65rem",
                                            "textAlign": "center",
                                        },
                                    ),
                                ],
                            ),
                            html.Div(
                                style={"flex": "1", "textAlign": "right"},
                                children=[
                                    html.Div(
                                        b.get("name", "?"),
                                        style={
                                            "color": COLORS["text_primary"],
                                            "fontWeight": "600",
                                            "fontSize": "0.85rem",
                                        },
                                    ),
                                    html.Div(
                                        f"Tag: {b.get('tag', '-')}",
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.75rem",
                                        },
                                    ),
                                    html.Div(
                                        f"Phone: {b.get('telephone', '-') or '-'}  |  "
                                        f"Events: {b.get('total_events', 0)}",
                                        style={
                                            "color": COLORS["text_secondary"],
                                            "fontSize": "0.72rem",
                                        },
                                    ),
                                ],
                            ),
                        ],
                    ),
                    html.Div(
                        style={
                            "display": "flex",
                            "gap": "0.5rem",
                            "marginTop": "0.6rem",
                            "justifyContent": "flex-end",
                        },
                        children=[
                            html.Button(
                                f"Keep {a.get('tag', '?')}",
                                id={"type": "btn-merge", "keep": a["uuid"], "remove": b["uuid"]},
                                style={
                                    "backgroundColor": COLORS["accent_green"],
                                    "color": "#fff",
                                    "border": "none",
                                    "padding": "0.3rem 0.7rem",
                                    "borderRadius": "5px",
                                    "cursor": "pointer",
                                    "fontSize": "0.72rem",
                                    "fontWeight": "500",
                                },
                            ),
                            html.Button(
                                f"Keep {b.get('tag', '?')}",
                                id={"type": "btn-merge", "keep": b["uuid"], "remove": a["uuid"]},
                                style={
                                    "backgroundColor": COLORS["accent_blue"],
                                    "color": "#fff",
                                    "border": "none",
                                    "padding": "0.3rem 0.7rem",
                                    "borderRadius": "5px",
                                    "cursor": "pointer",
                                    "fontSize": "0.72rem",
                                    "fontWeight": "500",
                                },
                            ),
                            html.Button(
                                "Not same",
                                id={"type": "btn-not-same", "a": a["uuid"], "b": b["uuid"]},
                                style={
                                    "backgroundColor": "transparent",
                                    "color": COLORS["text_secondary"],
                                    "border": f"1px solid {COLORS['border']}",
                                    "padding": "0.3rem 0.7rem",
                                    "borderRadius": "5px",
                                    "cursor": "pointer",
                                    "fontSize": "0.72rem",
                                },
                            ),
                        ],
                    ),
                ],
            )
            cards.append(card)

        return cards, "", f"Potential duplicates ({len(candidates)})"

    @app.callback(
        Output("duplicates-feedback", "children", allow_duplicate=True),
        Output("duplicates-list", "children", allow_duplicate=True),
        Output("duplicates-title", "children", allow_duplicate=True),
        Input({"type": "btn-not-same", "a": ALL, "b": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def dismiss_not_same(n_clicks_list):
        if not n_clicks_list or not any(n_clicks_list):
            return no_update, no_update, no_update

        triggered = ctx.triggered_id
        if not triggered:
            return no_update, no_update, no_update

        a_uuid = triggered.get("a", "")
        b_uuid = triggered.get("b", "")

        # Log the dismissal to audit_log for traceability
        try:
            log_fn = getattr(storage_api, "log_action", None)
            if log_fn:
                log_fn(
                    {"user_id": "", "user_name": "TO", "user_email": ""},
                    "duplicate_dismissed",
                    "players",
                    target_player=a_uuid,
                    details=json.dumps({"pair": [a_uuid, b_uuid]}),
                    reason="TO marked as not same player",
                )
        except Exception:
            pass  # non-critical

        feedback = html.Div(
            "Dismissed pair. They will reappear on next scan (dismiss state is not persisted yet).",
            style={"color": COLORS["text_secondary"], "fontSize": "0.8rem", "fontStyle": "italic"},
        )

        # Re-scan (pair still shows since we don't persist dismissals yet)
        candidates_list, _, candidates_title = _build_candidates_list()
        return feedback, candidates_list, candidates_title

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

        # Payment toggle should also set payment_amount/payment_expected so revenue is tracked.
        if col_id == "payment_valid":
            try:
                per_game = int(settings.get("swish_expected_per_game") or 0)
            except Exception:
                per_game = 0

            raw_games = row.get("tournament_games_registered")
            if isinstance(raw_games, list):
                game_count = len([g for g in raw_games if str(g or "").strip()])
            elif isinstance(raw_games, str):
                game_count = len([g for g in raw_games.split(",") if str(g or "").strip()])
            else:
                game_count = 0

            expected_amount = per_game * max(game_count, 1)
            if new_val:
                # Mark as paid: set amount to expected so archive revenue is correct.
                update_data["payment_expected"] = expected_amount
                update_data["payment_amount"] = expected_amount
            else:
                # Unmark paid: clear paid amount but keep expected for UI context.
                update_data["payment_expected"] = expected_amount
                update_data["payment_amount"] = 0

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
            if "payment_amount" in update_data:
                table_data[row_idx]["payment_amount"] = update_data["payment_amount"]
            if "payment_expected" in update_data:
                table_data[row_idx]["payment_expected"] = update_data["payment_expected"]
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
        Output("input-manual-games", "value"),
        Input("btn-manual-checkin", "n_clicks"),
        State("input-manual-name", "value"),
        State("input-manual-tag", "value"),
        State("input-manual-games", "value"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def add_manual_checkin(n_clicks, input_name, input_tag, input_games, selected_slug, auth_state):
        """Manually add a missing Start.gg participant to active check-ins."""
        if not n_clicks:
            return no_update, no_update, no_update, no_update

        if not selected_slug or selected_slug == "__ALL__":
            return (
                html.Span("⚠️ Select a specific event first.", style={"color": "#f59e0b"}),
                no_update,
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
                no_update,
            )

        if not name:
            name = tag

        settings = get_active_settings() or {}
        require_membership = settings.get("require_membership") is True
        require_payment = settings.get("require_payment") is True

        selected_games = input_games or []

        payload = {
            "name": name,
            "tag": tag,
            "startgg": True,
            "is_guest": False,
            "member": require_membership,
            "payment_valid": require_payment,
            "added_via": "manual_dashboard",
            "tournament_games_registered": selected_games,
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
                    details=json.dumps({"tag": tag, "created": created, "games": selected_games}),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for manual check-in: {e}")

            games_label = f" ({', '.join(selected_games)})" if selected_games else ""
            label = "added" if created else "updated"
            feedback = html.Span(
                f"✅ Manual check-in {label}: {name}{games_label}",
                style={"color": "#10b981"},
            )
            return feedback, "", "", []
        except Exception as e:
            logger.exception(f"Manual check-in failed for '{name}': {e}")
            return (
                html.Span(f"❌ Manual check-in failed: {e}", style={"color": "#ef4444"}),
                no_update,
                no_update,
                no_update,
            )

    # -------------------------------------------------------------------------
    # Re-check Start.gg - re-validate registration + sync games for selected player
    # -------------------------------------------------------------------------
    @app.callback(
        Output("recheck-startgg-feedback", "children", allow_duplicate=True),
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
    # Bulk Re-check Start.gg for all players
    # -------------------------------------------------------------------------
    @app.callback(
        Output("recheck-startgg-feedback", "children", allow_duplicate=True),
        Input("btn-bulk-recheck-startgg", "n_clicks"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def bulk_recheck_startgg(n_clicks, selected_slug, auth_state):
        if not n_clicks:
            return no_update

        try:
            resp = requests.post(
                "http://backend:8000/api/admin/bulk-recheck-startgg",
                json={},
                timeout=120,
            )

            if resp.status_code >= 400:
                error_detail = resp.text[:200]
                return html.Span(
                    f"Bulk re-check failed: {error_detail}",
                    style={"color": "#ef4444"},
                )

            data = resp.json()
            total = data.get("total", 0)
            checked = data.get("checked", 0)
            emails_found = data.get("emails_found", 0)
            errors = data.get("errors", [])

            parts = [
                f"Bulk re-check done: {checked}/{total} players checked, "
                f"{emails_found} emails found."
            ]
            if errors:
                parts.append(f" ({len(errors)} errors)")

            try:
                storage_api.log_action(
                    {
                        "user_id": (auth_state or {}).get("user_id", ""),
                        "user_name": (auth_state or {}).get("user_name", "system"),
                        "user_email": (auth_state or {}).get("user_email", ""),
                    },
                    "admin_bulk_recheck_startgg",
                    "active_event_data",
                    target_event=selected_slug or "",
                    details=json.dumps(
                        {"total": total, "checked": checked, "emails_found": emails_found}
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log for bulk recheck: {e}")

            return html.Span(
                "".join(parts),
                style={"color": "#22c55e", "fontWeight": "600"},
            )

        except requests.exceptions.Timeout:
            return html.Span(
                "Bulk re-check timed out — too many players or Start.gg is slow.",
                style={"color": "#f59e0b"},
            )
        except Exception as e:
            logger.exception(f"Bulk re-check Start.gg failed: {e}")
            return html.Span(
                f"Bulk re-check failed: {e}",
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
        State("collect-acquisition-source-toggle", "value"),
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
        collect_acquisition_source,
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
            "collect_acquisition_source": bool(collect_acquisition_source),
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
            if collect_acquisition_source:
                enabled.append("Acquisition Source")

            if enabled:
                summary = f"Requiring: {', '.join(enabled)}"
            else:
                summary = "No requirements (all players auto-Ready)"

            # Update the store with new values
            new_store = {
                "require_payment": bool(req_payment),
                "require_membership": bool(req_membership),
                "require_startgg": bool(req_startgg),
                "collect_acquisition_source": bool(collect_acquisition_source),
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
                            "collect_acquisition_source": {
                                "old": bool(previous.get("collect_acquisition_source")),
                                "new": bool(collect_acquisition_source),
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
        collect_source = requirements.get("collect_acquisition_source", False)

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

        if collect_source:
            badges.append(
                html.Span(
                    "🧭 Source",
                    style={
                        **badge_style_active,
                        "backgroundColor": "rgba(56, 189, 248, 0.2)",
                        "color": "#38bdf8",
                    },
                )
            )

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
        Output("collect-acquisition-source-toggle", "value"),
        Input("requirements-store", "data"),
    )
    def sync_checkboxes_with_store(requirements):
        """Sync the settings checkboxes with the requirements store."""
        if not requirements:
            # Default all to True if no store data
            return [True], [True], [True], []

        # Default to True (checked) unless explicitly False
        payment_val = [True] if requirements.get("require_payment", True) else []
        membership_val = [True] if requirements.get("require_membership", True) else []
        startgg_val = [True] if requirements.get("require_startgg", True) else []
        source_val = [True] if requirements.get("collect_acquisition_source", False) else []

        return payment_val, membership_val, startgg_val, source_val

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

    @app.callback(
        Output("input-checkin-opened-at", "value"),
        Output("input-event-started-at", "value"),
        Output("input-event-ended-at", "value"),
        Input("btn-set-checkin-opened-now", "n_clicks"),
        Input("btn-set-event-started-now", "n_clicks"),
        Input("btn-set-event-ended-now", "n_clicks"),
        State("input-checkin-opened-at", "value"),
        State("input-event-started-at", "value"),
        State("input-event-ended-at", "value"),
        prevent_initial_call=True,
    )
    def set_ops_timing_now(
        _opened_clicks,
        _started_clicks,
        _ended_clicks,
        opened_value,
        started_value,
        ended_value,
    ):
        now_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
        trig = ctx.triggered_id
        if trig == "btn-set-checkin-opened-now":
            return now_local, started_value, ended_value
        if trig == "btn-set-event-started-now":
            return opened_value, now_local, ended_value
        if trig == "btn-set-event-ended-now":
            return opened_value, started_value, now_local
        return opened_value, started_value, ended_value

    @app.callback(
        Output("ops-timing-feedback", "children"),
        Input("btn-save-ops-timing", "n_clicks"),
        State("input-checkin-opened-at", "value"),
        State("input-event-started-at", "value"),
        State("input-event-ended-at", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def save_ops_timing(n_clicks, checkin_opened_at, event_started_at, event_ended_at, auth_state):
        if not n_clicks:
            return no_update

        settings_data = get_active_settings_with_id()
        if not settings_data:
            return html.Span("❌ No active settings found", style={"color": "#ef4444"})

        record_id = settings_data.get("record_id")
        if not record_id:
            return html.Span("❌ Could not find settings record", style={"color": "#ef4444"})

        local_tz = datetime.now().astimezone().tzinfo

        def _to_iso(v):
            if not v:
                return None
            txt = str(v).strip()
            if not txt:
                return None
            txt = txt.replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            return dt.isoformat()

        try:
            opened_iso = _to_iso(checkin_opened_at)
            started_iso = _to_iso(event_started_at)
            ended_iso = _to_iso(event_ended_at)
        except Exception:
            return html.Span(
                "❌ Invalid datetime format. Use YYYY-MM-DDTHH:MM",
                style={"color": "#ef4444"},
            )

        if started_iso and ended_iso and ended_iso < started_iso:
            return html.Span(
                "❌ Event ended-at cannot be earlier than started-at",
                style={"color": "#ef4444"},
            )

        update_data = {
            "checkin_opened_at": opened_iso,
            "event_started_at": started_iso,
            "event_ended_at": ended_iso,
        }
        result = update_settings(record_id, update_data)
        if not result:
            return html.Span("❌ Failed to save timing", style={"color": "#ef4444"})

        try:
            prev_fields = settings_data.get("fields", {}) or {}
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "admin_update_event_timing",
                "settings",
                target_event=get_active_slug() or "",
                details=json.dumps(
                    {
                        "checkin_opened_at": {
                            "old": prev_fields.get("checkin_opened_at"),
                            "new": opened_iso,
                        },
                        "event_started_at": {
                            "old": prev_fields.get("event_started_at"),
                            "new": started_iso,
                        },
                        "event_ended_at": {
                            "old": prev_fields.get("event_ended_at"),
                            "new": ended_iso,
                        },
                    }
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for ops timing save: {e}")

        return html.Span("✅ Operations timing saved", style={"color": "#10b981"})

    @app.callback(
        Output("live-ops-feedback", "children"),
        Input("btn-live-checkin-opened-now", "n_clicks"),
        Input("btn-live-event-started-now", "n_clicks"),
        Input("btn-live-event-ended-now", "n_clicks"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def live_set_ops_now(_open_clicks, _start_clicks, _end_clicks, auth_state):
        trig = ctx.triggered_id
        field_map = {
            "btn-live-checkin-opened-now": "checkin_opened_at",
            "btn-live-event-started-now": "event_started_at",
            "btn-live-event-ended-now": "event_ended_at",
        }
        labels = {
            "checkin_opened_at": "Check-in opened",
            "event_started_at": "Event started",
            "event_ended_at": "Event ended",
        }

        key = field_map.get(trig)
        if not key:
            return no_update

        settings_data = get_active_settings_with_id()
        if not settings_data:
            return html.Span("❌ No active settings found", style={"color": "#ef4444"})

        record_id = settings_data.get("record_id")
        if not record_id:
            return html.Span("❌ Could not find settings record", style={"color": "#ef4444"})

        now_iso = datetime.now().astimezone().isoformat()
        result = update_settings(record_id, {key: now_iso})
        if not result:
            return html.Span("❌ Failed to update live timing", style={"color": "#ef4444"})

        try:
            prev_fields = settings_data.get("fields", {}) or {}
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "admin_update_event_timing",
                "settings",
                target_event=get_active_slug() or "",
                details=json.dumps({key: {"old": prev_fields.get(key), "new": now_iso}}),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for live timing update: {e}")

        return html.Span(f"✅ {labels.get(key, 'Timing')} set to now", style={"color": "#10b981"})

    @app.callback(
        Output("live-ops-status", "children"),
        Input("interval-refresh", "n_intervals"),
        Input("tabs", "value"),
        Input("event-dropdown", "value"),
    )
    def update_live_ops_status(_n_intervals, selected_tab, selected_slug):
        if selected_tab != "tab-checkins":
            return no_update

        settings = get_active_settings() or {}
        slug = selected_slug
        if not slug or slug == "__ALL__":
            slug = settings.get("active_event_slug")

        opened_at = settings.get("checkin_opened_at")
        started_at = settings.get("event_started_at")
        ended_at = settings.get("event_ended_at")
        now_utc = datetime.now(timezone.utc)

        chips: List[Any] = []

        def _chip(label: str, value: str, color: str = "#94a3b8") -> Any:
            return html.Div(
                style={
                    "display": "inline-flex",
                    "gap": "0.35rem",
                    "alignItems": "center",
                    "backgroundColor": "#0f172a",
                    "border": "1px solid #1e293b",
                    "borderRadius": "999px",
                    "padding": "0.28rem 0.58rem",
                },
                children=[
                    html.Span(
                        label,
                        style={
                            "fontSize": "0.68rem",
                            "color": "#94a3b8",
                            "textTransform": "uppercase",
                            "letterSpacing": "0.04em",
                        },
                    ),
                    html.Span(
                        value,
                        style={"fontSize": "0.76rem", "fontWeight": "600", "color": color},
                    ),
                ],
            )

        if slug and slug != "__ALL__":
            try:
                active_rows = get_checkins(slug) or []
            except Exception:
                active_rows = []
            participant_count = len(active_rows)
            chips.append(_chip("Participants", str(participant_count), "#22d3ee"))

            if opened_at and participant_count > 0:
                try:
                    if not isinstance(opened_at, datetime):
                        opened_at = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
                    minutes = max((now_utc - opened_at).total_seconds() / 60.0, 1.0)
                    speed = participant_count / minutes
                    chips.append(_chip("Check-in speed", f"{speed:.2f}/min", "#34d399"))
                except Exception:
                    chips.append(_chip("Check-in speed", "-"))
            else:
                chips.append(_chip("Check-in speed", "-"))

        if started_at:
            try:
                if not isinstance(started_at, datetime):
                    started_at = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if ended_at:
                    if not isinstance(ended_at, datetime):
                        ended_at = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
                    elapsed = max((ended_at - started_at).total_seconds(), 0.0)
                    duration_label = "Duration"
                else:
                    elapsed = max((now_utc - started_at).total_seconds(), 0.0)
                    duration_label = "Elapsed"
                hours = elapsed / 3600.0
                chips.append(_chip(duration_label, f"{hours:.2f} h", "#60a5fa"))
            except Exception:
                chips.append(_chip("Duration", "-"))
        else:
            chips.append(_chip("Duration", "-"))

        return chips

    # -------------------------------------------------------------------------
    # Archive current event to event_archive + event_stats
    # -------------------------------------------------------------------------
    @app.callback(
        Output("recompute-event-feedback", "children"),
        Input("btn-recompute-event-stats", "n_clicks"),
        State("event-dropdown", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def recompute_selected_event_stats(n_clicks, selected_slug, auth_state):
        if not n_clicks:
            return no_update

        if not selected_slug or selected_slug == "__ALL__":
            return html.Span(
                "❌ Select a specific event first.", style={"color": "#ef4444"}
            )

        recompute_fn = getattr(storage_api, "recompute_event_stats", None)
        if not recompute_fn:
            return html.Span(
                "❌ Recompute is unavailable on current data backend.",
                style={"color": "#ef4444"},
            )

        try:
            result = recompute_fn(
                selected_slug,
                user={
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
            )
        except Exception as e:
            logger.exception(f"Recompute failed for {selected_slug}: {e}")
            return html.Span(f"❌ Recompute failed: {e}", style={"color": "#ef4444"})

        warns = result.get("integrity_warnings") or []
        children = [
            html.Div(
                f"✅ Recomputed stats for {result.get('event_slug', selected_slug)}",
                style={"color": "#10b981"},
            ),
            html.Div(
                f"Participants: {result.get('participants', 0)} | "
                f"New: {result.get('new_players', 0)} | "
                f"Returning: {result.get('returning_players', 0)} | "
                f"Revenue: {result.get('total_revenue', 0)}",
                style={"color": "#10b981"},
            ),
        ]
        if warns:
            children.append(
                html.Div(
                    f"⚠ Integrity warnings: {'; '.join(str(w) for w in warns)}",
                    style={"color": "#f59e0b", "marginTop": "0.25rem"},
                )
            )
        return html.Div(children=children, style={"lineHeight": "1.5"})

    @app.callback(
        Output("scan-integrity-feedback", "children"),
        Output("integrity-scan-table", "data"),
        Input("btn-scan-event-integrity", "n_clicks"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def scan_archived_event_integrity(n_clicks, auth_state):
        if not n_clicks:
            return no_update, no_update

        scan_fn = getattr(storage_api, "scan_event_stats_integrity", None)
        if not scan_fn:
            return (
                html.Span(
                    "❌ Integrity scan is unavailable on current data backend.",
                    style={"color": "#ef4444"},
                ),
                [],
            )

        try:
            rows = scan_fn() or []
        except Exception as e:
            logger.exception(f"Integrity scan failed: {e}")
            return html.Span(f"❌ Integrity scan failed: {e}", style={"color": "#ef4444"}), []

        table_rows = [
            {
                "event_slug": r.get("event_slug", ""),
                "warnings_count": int(r.get("warnings_count") or 0),
                "warnings_text": "; ".join(str(w) for w in (r.get("warnings") or [])),
                "archived_at": r.get("archived_at", ""),
            }
            for r in rows
        ]

        try:
            storage_api.log_action(
                {
                    "user_id": (auth_state or {}).get("user_id", ""),
                    "user_name": (auth_state or {}).get("user_name", "system"),
                    "user_email": (auth_state or {}).get("user_email", ""),
                },
                "event_stats_integrity_scanned",
                "event_stats",
                details=json.dumps({"scan": True, "issues_found": len(table_rows)}),
            )
        except Exception as e:
            logger.warning(f"Failed to write audit log for integrity scan: {e}")

        if not table_rows:
            return (
                html.Span("✅ No integrity warnings found.", style={"color": "#10b981"}),
                [],
            )

        return (
            html.Span(
                f"⚠ Found warnings in {len(table_rows)} archived event(s).",
                style={"color": "#f59e0b"},
            ),
            table_rows,
        )

    @app.callback(
        Output("archive-feedback", "children", allow_duplicate=True),
        Output("event-dropdown", "value", allow_duplicate=True),
        Output("reopen-event-selector", "options"),
        Input("btn-archive-event-quick", "n_clicks"),
        Input("btn-archive-event", "n_clicks"),
        State("event-dropdown", "value"),
        State("archive-clear-active-toggle", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
        running=[
            (Output("btn-archive-event-quick", "disabled"), True, False),
            (Output("btn-archive-event", "disabled"), True, False),
            (Output("btn-clear-current-event", "disabled"), True, False),
        ],
    )
    def archive_current_event(n_clicks_quick, n_clicks, selected_slug, clear_flags, auth_state):
        if not n_clicks and not n_clicks_quick:
            return no_update, no_update, no_update

        if not selected_slug or selected_slug == "__ALL__":
            return (
                html.Span(
                    "❌ Select a specific event before archiving.", style={"color": "#ef4444"}
                ),
                no_update,
                no_update,
            )

        clear_active = "clear" in (clear_flags or [])

        settings = get_active_settings() or {}
        is_selected_active = (settings.get("active_event_slug") or "") == selected_slug
        payload = {
            "event_slug": selected_slug,
            "event_date": settings.get("event_date") if is_selected_active else None,
            "event_display_name": settings.get("event_display_name", "") if is_selected_active else "",
            "swish_expected_per_game": settings.get("swish_expected_per_game", 0),
            "startgg_snapshot": settings.get("events_json") if is_selected_active else None,
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
                return (
                    html.Span(f"❌ Archive failed: {e}", style={"color": "#ef4444"}),
                    no_update,
                    no_update,
                )
        else:
            try:
                resp = requests.post(
                    f"{BACKEND_INTERNAL_URL}/api/archive/event",
                    json=payload,
                    timeout=30,
                )
                if not resp.ok:
                    return (
                        html.Span(
                            f"❌ Archive failed ({resp.status_code}): {resp.text}",
                            style={"color": "#ef4444"},
                        ),
                        no_update,
                        no_update,
                    )
                result = resp.json()
            except Exception as e:
                logger.exception(f"Archive API call failed for {selected_slug}: {e}")
                return (
                    html.Span(f"❌ Archive API failed: {e}", style={"color": "#ef4444"}),
                    no_update,
                    no_update,
                )

        clear_dropdown = no_update
        if clear_active:
            # Clear only dashboard selection; keep global active event in settings/auth
            # so users are not redirected out of admin.
            clear_dropdown = None

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

        # Rebuild reopen dropdown options with fresh data from event_history
        try:
            history = storage_api.get_event_history() or []
        except Exception:
            history = []
        reopen_options = []
        for ev in history:
            slug = ev.get("event_slug") if isinstance(ev, dict) else None
            if slug:
                name = ev.get("event_display_name") or slug.replace("-", " ").title()
                date = ev.get("event_date") or ""
                players = ev.get("total_participants", 0)
                label = f"{name}"
                if date:
                    label += f"  ({date})"
                if players:
                    label += f"  — {players} players"
                reopen_options.append({"label": label, "value": slug})

        return (
            html.Div(
                style={"color": "#10b981", "lineHeight": "1.5"},
                children=[
                    html.Div(f"✅ Archived event: {result.get('event_slug', selected_slug)}"),
                    html.Div(
                        f"Participants: {result.get('archived', 0)} | "
                        f"Revenue: {result.get('total_revenue', 0)} | "
                        f"New: {result.get('new_players', 0)} | Returning: {result.get('returning_players', 0)}"
                    ),
                    html.Div(
                        "⚠ Integrity warnings: "
                        + "; ".join(str(w) for w in (result.get("integrity_warnings") or [])),
                        style={
                            "color": "#f59e0b",
                            "display": (
                                "block"
                                if bool(result.get("integrity_warnings"))
                                else "none"
                            ),
                        },
                    ),
                    html.Div(
                        f"Replaced rows: {result.get('replaced_rows', 0)} | "
                        f"Cleared active: {result.get('cleared_active', 0)}"
                    ),
                ],
            ),
            clear_dropdown,
            reopen_options,
        )

    # -------------------------------------------------------------------------
    # Reopen archived event and optionally restore active check-ins
    # -------------------------------------------------------------------------
    @app.callback(
        Output("reopen-feedback", "children"),
        Output("event-dropdown", "options", allow_duplicate=True),
        Output("event-dropdown", "value", allow_duplicate=True),
        Output("reopen-event-selector", "value"),
        Input("btn-reopen-event", "n_clicks"),
        State("reopen-event-selector", "value"),
        State("reopen-restore-active-toggle", "value"),
        State("auth-store", "data"),
        prevent_initial_call=True,
    )
    def reopen_archived_event(n_clicks, selected_slug, restore_flags, auth_state):
        if not n_clicks:
            return no_update, no_update, no_update, no_update

        if not selected_slug:
            return (
                html.Span(
                    "⚠️ Select an archived event from the dropdown first.",
                    style={"color": "#f59e0b"},
                ),
                no_update,
                no_update,
                no_update,
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
                return (
                    html.Span(f"❌ Reopen failed: {e}", style={"color": "#ef4444"}),
                    no_update,
                    no_update,
                    no_update,
                )
        else:
            try:
                resp = requests.post(
                    f"{BACKEND_INTERNAL_URL}/api/archive/reopen",
                    json=payload,
                    timeout=30,
                )
                if not resp.ok:
                    return (
                        html.Span(
                            f"❌ Reopen failed ({resp.status_code}): {resp.text}",
                            style={"color": "#ef4444"},
                        ),
                        no_update,
                        no_update,
                        no_update,
                    )
                result = resp.json()
            except Exception as e:
                logger.exception(f"Reopen API call failed for {selected_slug}: {e}")
                return (
                    html.Span(f"❌ Reopen API failed: {e}", style={"color": "#ef4444"}),
                    no_update,
                    no_update,
                    no_update,
                )

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

        # Update main event dropdown with the reopened slug
        from shared.storage import get_all_event_slugs

        all_slugs = get_all_event_slugs() or []
        if selected_slug not in all_slugs:
            all_slugs = [selected_slug] + all_slugs
        dropdown_options = [{"label": s.replace("-", " ").title(), "value": s} for s in all_slugs]

        return (
            html.Div(
                style={"color": "#10b981", "lineHeight": "1.5"},
                children=[
                    html.Div(f"✅ Reopened event: {result.get('event_slug', selected_slug)}"),
                    html.Div(f"Restored rows: {result.get('restored_rows', 0)}"),
                ],
            ),
            dropdown_options,
            selected_slug,
            None,  # Clear the reopen selector
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

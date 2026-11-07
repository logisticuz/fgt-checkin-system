"""Read-only Start.gg game metadata with explicit unavailable results."""

import json
import os
import re
import time
from urllib.parse import urlparse

import requests

_CACHE = {}
_TTL = 600


def _event_names(events, slug):
    if not isinstance(events, list):
        return None
    names = []
    for event in events:
        if not isinstance(event, dict):
            return None
        name = event.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        event_slug = event.get("slug")
        if event_slug:
            if not isinstance(event_slug, str):
                return None
            parent = event_slug.split("/event/", 1)[0].removeprefix("tournament/")
            if parent != slug:
                return None
        if name not in names:
            names.append(name)
    return names


def games_from_settings(settings, slug):
    """Use saved event metadata only when it belongs to the selected event."""
    if settings.get("active_event_slug") != slug:
        return None
    link = settings.get("startgg_event_url")
    if link:
        match = re.search(r"/tournament/([^/]+)", urlparse(str(link)).path)
        if match is None or match[1] != slug:
            return None
    events = settings.get("events_json")
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except (TypeError, ValueError):
            return None
    if isinstance(events, dict):
        events = events.get("events")
    names = _event_names(events, slug)
    return names or None


def fetch_tournament_games(slug, api_key):
    """Return (raw event names, unavailable reason); an empty reason means success."""
    if not isinstance(slug, str) or not slug.strip() or slug == "__ALL__":
        return [], "select a specific event"
    if os.getenv("INTEGRATION_ENGINE", "n8n") != "n8n":
        return [], "lookup is disabled"
    if not api_key:
        return [], "lookup is not configured"
    cached = _CACHE.get(slug)
    if cached and time.monotonic() - cached[0] < _TTL:
        return list(cached[1]), ""
    query = {
        "query": """query TournamentEvents($slug: String!) {
            tournament(slug: $slug) { id name events { id name } }
        }""",
        "variables": {"slug": slug},
    }
    try:
        response = requests.post(
            "https://api.start.gg/gql/alpha",
            json=query,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        if type(response.status_code) is not int or not 200 <= response.status_code < 300:
            return [], "provider returned an HTTP error"
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or "errors" in payload
            or "error" in payload
            or payload.get("request_result") not in (None, "success")
        ):
            return [], "provider returned an invalid response"
        data = payload.get("data")
        tournament = data.get("tournament") if isinstance(data, dict) else None
        names = (
            _event_names(tournament.get("events"), slug) if isinstance(tournament, dict) else None
        )
        if names is None:
            return [], "provider event data is unavailable"
    except (requests.RequestException, TypeError, ValueError):
        return [], "provider lookup failed"
    _CACHE[slug] = (time.monotonic(), tuple(names))
    return names, ""

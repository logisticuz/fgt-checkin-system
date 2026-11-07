"""Shared backend configuration, clients, helpers and single-instance state."""

from fastapi import Depends, FastAPI, Request, HTTPException


from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse


from fastapi.templating import Jinja2Templates


from fastapi.staticfiles import StaticFiles


from fastapi.middleware.cors import CORSMiddleware


import os


import json


import logging


import secrets


import time


import asyncio


from typing import Set


from contextlib import asynccontextmanager


import httpx


import requests


from requests.adapters import HTTPAdapter


from urllib3.util.retry import Retry


from shared.storage import (
    get_players as storage_get_players,
    get_event_history as storage_get_event_history,
    get_active_settings as storage_get_active_settings,
    get_checkin_by_name,
    get_checkin_by_tag,
    update_checkin,
    delete_checkin,
)


try:
    from shared.storage import (
        get_checkin_by_record_id,
        compute_checkin_status,
    )
except ImportError:
    get_checkin_by_record_id = None
    compute_checkin_status = None


import shared.storage as storage_api


from shared.membership_flow import get_membership_flow


from validation import sanitize_checkin_payload, validate_checkin_payload


from status_api import lookup_checkin_status, status_context


logging.basicConfig(level=logging.INFO)


logger = logging.getLogger(__name__)


DATA_BACKEND = os.getenv("DATA_BACKEND", "postgres").lower().strip()


INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN")


N8N_INTERNAL = os.getenv("N8N_INTERNAL_URL", "http://n8n:5678")


INTEGRATION_ENGINE = os.getenv("INTEGRATION_ENGINE", "n8n").lower().strip()


EBAS_REGISTER_TIMEOUT_SECONDS = float(os.getenv("EBAS_REGISTER_TIMEOUT_SECONDS", "75"))


N8N_WEBHOOK_TOKEN = os.getenv("N8N_WEBHOOK_TOKEN")  # optional shared secret for webhook calls


SSE_TOKEN = os.getenv("SSE_TOKEN")  # token for SSE authentication (used instead of Basic Auth)


ADMIN_AUTH_COOKIE_TOKEN = os.getenv("ADMIN_AUTH_COOKIE_TOKEN")


STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")  # for fetching tournament events


N8N_BASIC_AUTH_USER = os.getenv("N8N_BASIC_AUTH_USER")


N8N_BASIC_AUTH_PASSWORD = os.getenv("N8N_BASIC_AUTH_PASSWORD")


class SSEManager:
    """Manages SSE connections and broadcasts events to all connected clients."""

    def __init__(self):
        self.clients: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def connect(self) -> asyncio.Queue:
        """Register a new SSE client."""
        queue = asyncio.Queue()
        async with self._lock:
            self.clients.add(queue)
        logger.info(f"SSE client connected. Total clients: {len(self.clients)}")
        return queue

    async def disconnect(self, queue: asyncio.Queue):
        """Remove a disconnected SSE client."""
        async with self._lock:
            self.clients.discard(queue)
        logger.info(f"SSE client disconnected. Total clients: {len(self.clients)}")

    async def broadcast(self, event: str, data: dict):
        """Send an event to all connected clients."""
        message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        async with self._lock:
            disconnected = []
            for queue in self.clients:
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    disconnected.append(queue)
            # Clean up full queues (likely dead connections)
            for queue in disconnected:
                self.clients.discard(queue)
        if self.clients:
            logger.debug(f"Broadcasted '{event}' to {len(self.clients)} clients")


sse_manager = SSEManager()


async def require_internal_soft(request: Request):
    """Observe missing/invalid internal tokens while allowing existing callers."""
    if not INTERNAL_API_TOKEN:
        return
    supplied = request.headers.get("X-Internal-Token", "")
    if not secrets.compare_digest(supplied.encode("utf-8"), INTERNAL_API_TOKEN.encode("utf-8")):
        logger.warning(
            "Internal request has a missing or invalid token: path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )


templates = Jinja2Templates(directory="templates")


httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))  # Increased for duplicate check


SESSION = requests.Session()


RETRY = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
)


ADAPTER = HTTPAdapter(max_retries=RETRY)


SESSION.mount("http://", ADAPTER)


SESSION.mount("https://", ADAPTER)


DEFAULT_TIMEOUT = 5


def safe_get(url: str, **kwargs):
    """GET with timeout + raise_for_status; return None on failure."""
    try:
        r = SESSION.get(url, timeout=DEFAULT_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        logger.warning(f"safe_get failed: {e}")
        return None


def _to_float(x):
    """Best-effort float parser that tolerates commas, strings and None."""
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return 0.0


def _to_str_phone(x):
    """Normalize phone-like values to a clean string (preserve leading zeros, drop trailing .0)."""
    s = "" if x is None else str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def slug_to_display_name(slug: str) -> str:
    """
    Convert a Start.gg event slug to a nice display name.

    "fight-night-17-at-backstage-rockbar-fgc-trollh-ttan"
    → "Fight Night 17"
    """
    if not slug:
        return "Tournament"

    # Take everything before "-at-" (venue info)
    if "-at-" in slug:
        slug = slug.split("-at-")[0]

    # Replace hyphens with spaces and title case
    name = slug.replace("-", " ").title()

    return name


def expand_game_display_name(name: str) -> str:
    """Expand common short game names to participant-friendly full names."""
    if not name:
        return ""

    normalized = str(name).strip().upper()
    expansions = {
        "SSBU": "SUPER SMASH BROS ULTIMATE - SINGLES",
        "T8": "TEKKEN 8 TOURNAMENT",
        "SF6": "STREET FIGHTER 6 TOURNAMENT",
    }
    return expansions.get(normalized, name)


def compute_requirements(settings: dict) -> dict:
    """
    Compute which requirements are active based on settings.

    We now require explicit True to enable a requirement.
    Missing/None = requirement is OFF (disabled).

    This means TOs must explicitly ENABLE each requirement they want.
    """
    return {
        "require_payment": settings.get("require_payment") is True,
        "require_membership": settings.get("require_membership") is True,
        "require_startgg": settings.get("require_startgg") is True,
    }


def compute_ready_and_missing(status: dict, requirements: dict) -> tuple:
    """
    Calculate if player is READY and what requirements are missing.

    READY formula (from system contract):
        READY = (NOT require_payment OR payment_valid)
            AND (NOT require_membership OR member)
            AND (NOT require_startgg OR startgg)

    Returns: (ready: bool, missing: list[str])
    """
    computed = compute_checkin_status(
        {
            "member": status.get("member"),
            "payment_valid": status.get("payment"),
            "startgg": status.get("startgg"),
        },
        requirements,
    )
    return computed["ready"], computed["missing"]


def get_active_settings() -> dict:
    """
    Fetch active event settings from the configured storage backend.
    Returns dict with swish_number, swish_expected_per_game, active_event_slug,
    and configurable check-in requirements.
    Uses shared storage facade.
    """
    fields = storage_get_active_settings() or {}
    return {
        "swish_number": fields.get("swish_number", "123 456 78 90"),
        "swish_expected_per_game": int(fields.get("swish_expected_per_game", 25)),
        "active_event_slug": fields.get("active_event_slug", ""),
        "startgg_event_ids": fields.get("startgg_event_ids", []),
        "event_display_name": fields.get("event_display_name", ""),
        # Configurable check-in requirements
        "require_payment": fields.get("require_payment"),
        "require_membership": fields.get("require_membership"),
        "require_startgg": fields.get("require_startgg"),
        # Optional features
        "offer_membership": fields.get("offer_membership"),
        "collect_acquisition_source": fields.get("collect_acquisition_source"),
    }


STARTGG_CACHE = {}


STARTGG_CACHE_TTL = 600  # 10 minutes


def get_tournament_events(tournament_slug: str) -> list:
    """
    Fetch events (games) for a tournament from Start.gg with caching.
    Returns list of dicts: [{"id": "123", "name": "Street Fighter 6"}, ...]

    Cache TTL: 10 minutes (events don't change during a tournament)
    Fallback: Returns empty list if API fails or no token configured
    """
    def _events_from_settings_fallback() -> list:
        """Build event list from active settings when Start.gg is unavailable."""
        try:
            settings = storage_get_active_settings() or {}
        except Exception:
            return []

        active_slug = (settings.get("active_event_slug") or "").strip()
        if active_slug and tournament_slug and active_slug != tournament_slug:
            return []

        out = []
        raw_events = settings.get("events_json")
        events = []
        if isinstance(raw_events, dict):
            events = raw_events.get("events") or []
        elif isinstance(raw_events, list):
            events = raw_events

        for idx, ev in enumerate(events):
            if not isinstance(ev, dict):
                continue
            name = (ev.get("name") or "").strip()
            if not name:
                continue
            ev_id = ev.get("id")
            out.append({"id": str(ev_id) if ev_id is not None else f"settings-{idx+1}", "name": name})

        if out:
            return out

        default_games = settings.get("default_game") or []
        for idx, game_name in enumerate(default_games):
            name = str(game_name or "").strip()
            if name:
                out.append({"id": f"default-{idx+1}", "name": name})
        return out

    if not tournament_slug:
        logger.warning("No tournament slug provided for event fetch")
        return []

    if not STARTGG_API_KEY:
        logger.warning("STARTGG_API_KEY not configured, cannot fetch events")
        return _events_from_settings_fallback()

    now = time.time()

    # Check cache
    if tournament_slug in STARTGG_CACHE:
        cached_data, timestamp = STARTGG_CACHE[tournament_slug]
        if now - timestamp < STARTGG_CACHE_TTL:
            logger.debug(f"Cache hit for tournament: {tournament_slug}")
            return cached_data

    # Cache miss - fetch from Start.gg
    logger.info(f"Fetching events from Start.gg for tournament: {tournament_slug}")

    query = {
        "query": """
            query TournamentEvents($slug: String!) {
                tournament(slug: $slug) {
                    id
                    name
                    events {
                        id
                        name
                    }
                }
            }
        """,
        "variables": {"slug": tournament_slug}
    }

    headers = {
        "Authorization": f"Bearer {STARTGG_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        resp = SESSION.post(
            "https://api.start.gg/gql/alpha",
            json=query,
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()

        data = resp.json()

        if "errors" in data:
            logger.error(f"Start.gg GraphQL errors: {data['errors']}")
            fallback_events = _events_from_settings_fallback()
            if fallback_events:
                logger.info("Using settings fallback events after Start.gg GraphQL errors")
                return fallback_events
            return []

        tournament = data.get("data", {}).get("tournament")
        if not tournament:
            logger.warning(f"Tournament not found: {tournament_slug}")
            fallback_events = _events_from_settings_fallback()
            if fallback_events:
                logger.info("Using settings fallback events for missing tournament")
                return fallback_events
            return []

        events = tournament.get("events") or []
        event_list = [
            {"id": str(e.get("id")), "name": e.get("name")}
            for e in events
            if e.get("id") and e.get("name")
        ]

        # Store in cache
        STARTGG_CACHE[tournament_slug] = (event_list, now)
        logger.info(f"Cached {len(event_list)} events for tournament: {tournament_slug}")

        return event_list

    except requests.RequestException as e:
        logger.error(f"Start.gg API request failed: {e}")
        fallback_events = _events_from_settings_fallback()
        if fallback_events:
            logger.info("Using settings fallback events after Start.gg request failure")
            return fallback_events
        return []
    except Exception as e:
        logger.error(f"Error parsing Start.gg response: {e}")
        fallback_events = _events_from_settings_fallback()
        if fallback_events:
            logger.info("Using settings fallback events after parsing error")
            return fallback_events
        return []


def check_participant_status(name: str) -> dict:
    """
    Fetch and evaluate a participant's registration status from current storage backend.
    Uses flexible match on name/tag fields.
    """
    status = {
        "name": name,
        "summary": "Not found",
        "member": False,
        "payment": False,
        "startgg": False,
    }

    query = (name or "").strip()
    if not query:
        return status

    try:
        active_slug = get_active_settings().get("active_event_slug", "")
        record = None
        if active_slug:
            record = get_checkin_by_tag(query, active_slug) or get_checkin_by_name(query, active_slug)
        if not record:
            record = get_checkin_by_name(query)

        if not record:
            return status

        f = (record.get("fields") or {}) if isinstance(record, dict) else {}

        # Check membership status
        status["member"] = bool(f.get("member"))

        # Check Start.gg registration.
        # Keep this aligned with dashboard/backend status logic: explicit Start.gg
        # verification only (not merely having selected games / event ID).
        startgg_ok = False
        for key in ("startgg", "startgg_registered"):
            val = f.get(key)
            if isinstance(val, bool) and val:
                startgg_ok = True
                break
            if isinstance(val, (int, str)) and str(val).strip().lower() in {"1", "true", "yes"}:
                startgg_ok = True
                break
        status["startgg"] = startgg_ok

        # Check payment status
        payment_ok = compute_checkin_status(f, {"require_payment": True})["ready"]
        status["payment"] = payment_ok

        # Build summary
        if all([status["member"], status["payment"], status["startgg"]]):
            status["summary"] = "Ready"
        elif not status["member"]:
            status["summary"] = "Missing membership"
        elif not status["payment"]:
            status["summary"] = "Missing payment"
        elif not status["startgg"]:
            status["summary"] = "Missing tournament registration"
        else:
            status["summary"] = "Partially complete"
    except Exception as e:
        logger.warning(f"Participant status lookup failed: {e}")
        status["summary"] = "Storage error"

    return status


def get_participant_details(namn: str) -> dict:
    """
    Fetch full participant details from current storage backend for status display.
    Returns tag, event_name, games, etc.
    """
    query = (namn or "").strip()
    if not query:
        return {}

    try:
        active_slug = get_active_settings().get("active_event_slug", "")
        record = None
        if active_slug:
            record = get_checkin_by_tag(query, active_slug) or get_checkin_by_name(query, active_slug)
        if not record:
            record = get_checkin_by_name(query)

        if record:
            f = (record.get("fields") or {}) if isinstance(record, dict) else {}
            games = f.get("tournament_games_registered", [])
            if isinstance(games, str):
                games = [g.strip() for g in games.split(",") if g.strip()]
            games = [expand_game_display_name(g) for g in games]
            event_slug = f.get("event_slug", "FGC Weekly")
            return {
                "tag": f.get("tag", ""),
                "event_name": event_slug.replace("-", " ").title(),
                "games": games,
                "payment_expected": f.get("payment_expected", 0),
            }
    except Exception as e:
        logger.warning(f"get_participant_details error: {e}")

    return {}

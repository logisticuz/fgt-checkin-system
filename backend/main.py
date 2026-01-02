# main.py

# Import FastAPI, templating, static files, and other dependencies
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import os
import json
import logging
import time
import asyncio
from urllib.parse import quote
from typing import Set
from contextlib import asynccontextmanager

import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from airtable_api import (
    get_players as airtable_get_players,
    get_event_history as airtable_get_event_history,
    get_active_settings as airtable_get_active_settings,
    get_checkin_by_name,
    get_checkin_by_tag,
    update_checkin,
    delete_checkin,
)
from validation import sanitize_checkin_payload, validate_checkin_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Config ===
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
# Updated to match your new table name in Airtable
AIRTABLE_TABLE = "active_event_data"
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
STARTGG_CLIENT_ID = os.getenv("STARTGG_CLIENT_ID")
STARTGG_CLIENT_SECRET = os.getenv("STARTGG_CLIENT_SECRET")
STARTGG_REDIRECT_URI = "https://checkin.fgctrollhattan.se/auth/callback"
OAUTH_TOKEN_PATH = "/app/data/startgg_token.json"  # stored in mounted backend/data
OAUTH_ADMIN_KEY = os.getenv("OAUTH_ADMIN_KEY", "supersecret")  # simple protection
N8N_INTERNAL = os.getenv("N8N_INTERNAL_URL", "http://n8n:5678")
N8N_WEBHOOK_TOKEN = os.getenv("N8N_WEBHOOK_TOKEN")  # optional shared secret for webhook calls
STARTGG_API_KEY = os.getenv("STARTGG_API_KEY") or os.getenv("STARTGG_TOKEN")  # for fetching tournament events

# If n8n is protected with Basic Auth, set these for proxy + health
N8N_BASIC_AUTH_USER = os.getenv("N8N_BASIC_AUTH_USER")
N8N_BASIC_AUTH_PASSWORD = os.getenv("N8N_BASIC_AUTH_PASSWORD")

assert AIRTABLE_API_KEY, "❌ Missing AIRTABLE_API_KEY in environment!"
assert AIRTABLE_BASE_ID, "❌ Missing AIRTABLE_BASE_ID in environment!"

# === SSE (Server-Sent Events) for real-time dashboard updates ===
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

# === App ===
app = FastAPI()

# CORS – strict in prod, allow localhost in dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://checkin.fgctrollhattan.se",
        "https://admin.fgctrollhattan.se",
        "http://localhost:8000",  # dev
        "http://localhost:8050",  # dev (dash)
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static & templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# === HTTP clients ===
# httpx for proxy to n8n
httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))  # Increased for duplicate check

# requests Session for Airtable with retries
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

# --- CHANGED ---
# === Helpers: normalization ===
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


# === Helpers: requirement logic (Task 1.1) ===
# See: docs/9_Systemkontrakt_och_Invarianter.md
def compute_requirements(settings: dict) -> dict:
    """
    Compute which requirements are active based on settings.

    Airtable checkbox semantics: default ON unless explicitly False.
    - Checkbox checked = True
    - Checkbox unchecked = field missing (None)
    - We treat None as True (requirement is ON by default)

    This ensures TOs must explicitly disable requirements.
    """
    return {
        "require_payment": settings.get("require_payment") is not False,
        "require_membership": settings.get("require_membership") is not False,
        "require_startgg": settings.get("require_startgg") is not False,
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
    missing = []

    if requirements["require_membership"] and not status.get("member"):
        missing.append("Membership")
    if requirements["require_payment"] and not status.get("payment"):
        missing.append("Payment")
    if requirements["require_startgg"] and not status.get("startgg"):
        missing.append("Start.gg")

    ready = len(missing) == 0
    return ready, missing
# --- /CHANGED ---

# === n8n proxy ===
@app.api_route("/n8n/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_n8n(path: str, request: Request):
    """
    Simple pass-through proxy to the internal n8n service.
    - Do NOT stream the upstream response (avoid chunk/encoding mismatch).
    - Drop hop-by-hop and now-invalid headers (Content-Length, Content-Encoding, ETag, Date, etc).
    - Inject Basic Auth when configured and no Authorization header present.
    - Optional: enforce a shared webhook token if N8N_WEBHOOK_TOKEN is set.
    """
    url = f"{N8N_INTERNAL}/{path}"

    # Copy incoming headers but never forward Host
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    # Force identity encoding so upstream returns plain bytes (safer for proxying)
    headers["accept-encoding"] = "identity"

    # Inject Basic Auth to n8n if configured and not already present
    if (
        N8N_BASIC_AUTH_USER
        and N8N_BASIC_AUTH_PASSWORD
        and "authorization" not in {k.lower() for k in headers.keys()}
    ):
        import base64
        token = base64.b64encode(f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()).decode()
        headers["authorization"] = f"Basic {token}"

    # Optional shared secret for webhook endpoints
    if N8N_WEBHOOK_TOKEN:
        # Only enforce for webhook paths to avoid breaking UI API calls
        if path.startswith("webhook") or "/webhook" in path:
            supplied = request.query_params.get("token") or request.headers.get("x-n8n-token")
            if supplied != N8N_WEBHOOK_TOKEN:
                raise HTTPException(status_code=401, detail="Invalid webhook token")

    body = await request.body()

    # Validate and sanitize JSON payloads for webhook POST requests
    if request.method == "POST" and body and (path.startswith("webhook") or "/webhook" in path):
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = json.loads(body)

                # Only validate check-in webhooks (not eBas, startgg, etc.)
                is_checkin_webhook = "checkin" in path.lower()

                if is_checkin_webhook:
                    # Validate check-in payload
                    errors = validate_checkin_payload(payload)
                    if errors:
                        logger.warning(f"Validation errors: {errors}")
                        raise HTTPException(status_code=400, detail={"errors": errors})

                # Sanitize all webhook payloads (phone, personnummer, etc.)
                sanitized = sanitize_checkin_payload(payload)
                body = json.dumps(sanitized).encode("utf-8")

                # Update content-length header
                headers["content-length"] = str(len(body))
                logger.debug(f"Sanitized payload for {path}: {sanitized}")

            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        resp = await httpx_client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )

        # Drop hop-by-hop + headers that can be invalid after proxying
        drop = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade",
            "content-length", "content-encoding", "etag", "date",
        }
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in drop}

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out_headers,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"n8n proxy error: {e}")

# === Domain logic ===
def get_active_settings() -> dict:
    """
    Fetch active event settings from Airtable settings table.
    Returns dict with swish_number, swish_expected_per_game, active_event_slug,
    and configurable check-in requirements.
    Uses shared airtable_api module.
    """
    fields = airtable_get_active_settings() or {}
    return {
        "swish_number": fields.get("swish_number", "123 456 78 90"),
        "swish_expected_per_game": int(fields.get("swish_expected_per_game", 25)),
        "active_event_slug": fields.get("active_event_slug", ""),
        "startgg_event_ids": fields.get("startgg_event_ids", []),
        # Configurable check-in requirements
        "require_payment": fields.get("require_payment"),
        "require_membership": fields.get("require_membership"),
        "require_startgg": fields.get("require_startgg"),
    }


# === Start.gg Event Cache ===
# Cache tournament events to avoid excessive API calls (rate limit: 80 req/min)
STARTGG_CACHE = {}
STARTGG_CACHE_TTL = 600  # 10 minutes


def get_tournament_events(tournament_slug: str) -> list:
    """
    Fetch events (games) for a tournament from Start.gg with caching.
    Returns list of dicts: [{"id": "123", "name": "Street Fighter 6"}, ...]

    Cache TTL: 10 minutes (events don't change during a tournament)
    Fallback: Returns empty list if API fails or no token configured
    """
    if not tournament_slug:
        logger.warning("No tournament slug provided for event fetch")
        return []

    if not STARTGG_API_KEY:
        logger.warning("STARTGG_API_KEY not configured, cannot fetch events")
        return []

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
            return []

        tournament = data.get("data", {}).get("tournament")
        if not tournament:
            logger.warning(f"Tournament not found: {tournament_slug}")
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
        return []
    except Exception as e:
        logger.error(f"Error parsing Start.gg response: {e}")
        return []


def check_participant_status(name: str) -> dict:
    """
    Fetch and evaluate a participant's registration status from Airtable.
    Uses flexible match on name/gametag fields.
    """
    status = {
        "name": name,
        "summary": "Not found",
        "member": False,
        "payment": False,
        "startgg": False,
    }

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    # Escape for Airtable formula (double single-quotes) + lower-case match
    def _esc_for_formula(s: str) -> str:
        return (s or "").replace("'", "''").lower()

    q = _esc_for_formula(name)

    # Match both name and tag fields
    formula = f"OR(LOWER(name)='{q}',LOWER(tag)='{q}')"

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?filterByFormula={quote(formula)}"
    r = safe_get(url, headers=headers)
    if not r:
        status["summary"] = "Airtable error"
        return status

    try:
        records = r.json().get("records", [])
        if records:
            f = records[0].get("fields", {}) or {}

            # Check membership status
            status["member"] = bool(f.get("member"))

            # Check Start.gg registration
            startgg_ok = False
            for key in ("startgg", "startgg_registered", "startgg_event_id"):
                val = f.get(key)
                if isinstance(val, bool) and val:
                    startgg_ok = True
                    break
                if isinstance(val, (int, str)) and str(val).strip():
                    startgg_ok = True
                    break
            status["startgg"] = startgg_ok

            # Check payment status
            amt = _to_float(f.get("payment_amount"))
            exp = _to_float(f.get("payment_expected"))
            payment_ok = (f.get("payment_valid") is True) or (exp > 0 and amt >= exp)
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
        logger.warning(f"Airtable parse error: {e}")
        status["summary"] = "Airtable error"

    return status

# === Health ===
@app.get("/health", tags=["System"])
async def health_check():
    """
    Lightweight health check for Docker/orchestration.
    Does NOT call external APIs (Airtable) to avoid burning API quota.
    Use /health/deep for full diagnostics.
    """
    # n8n health – Basic Auth if enabled; only 2xx is OK
    try:
        auth = None
        if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
            auth = (N8N_BASIC_AUTH_USER, N8N_BASIC_AUTH_PASSWORD)
        resp = await httpx_client.get(f"{N8N_INTERNAL}/healthz", auth=auth)
        n8n_ok = 200 <= resp.status_code < 300
    except Exception:
        n8n_ok = False

    return {
        "status": "ok" if n8n_ok else "degraded",
        "components": {"checkin": True, "dashboard": True, "n8n": n8n_ok},
        "version": "1.0.0",
    }


@app.get("/health/deep", tags=["System"])
async def health_check_deep():
    """
    Full health check including external APIs.
    Use this manually for diagnostics – NOT for automated polling.
    """
    # Airtable check
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    test_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?maxRecords=1"
    airtable_ok = bool(safe_get(test_url, headers=headers))

    # n8n health
    try:
        auth = None
        if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
            auth = (N8N_BASIC_AUTH_USER, N8N_BASIC_AUTH_PASSWORD)
        resp = await httpx_client.get(f"{N8N_INTERNAL}/healthz", auth=auth)
        n8n_ok = 200 <= resp.status_code < 300
    except Exception:
        n8n_ok = False

    return {
        "status": "ok" if airtable_ok and n8n_ok else "degraded",
        "components": {"checkin": True, "dashboard": True, "airtable": airtable_ok, "n8n": n8n_ok},
        "version": "1.0.0",
    }

# === Views ===
def get_participant_details(namn: str) -> dict:
    """
    Fetch full participant details from Airtable for status display.
    Returns tag, event_name, games, etc.
    """
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    def _esc(s: str) -> str:
        return (s or "").replace("'", "''").lower()

    q = _esc(namn)
    formula = f"OR(LOWER(name)='{q}',LOWER(tag)='{q}')"
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?filterByFormula={quote(formula)}"

    r = safe_get(url, headers=headers)
    if not r:
        return {}

    try:
        records = r.json().get("records", [])
        if records:
            f = records[0].get("fields", {})
            games = f.get("tournament_games_registered", [])
            if isinstance(games, str):
                games = [g.strip() for g in games.split(",") if g.strip()]
            return {
                "tag": f.get("tag", ""),
                "event_name": f.get("event_slug", "FGC Weekly").replace("-", " ").title(),
                "games": games,
                "payment_expected": f.get("payment_expected", 0),
            }
    except Exception as e:
        logger.warning(f"get_participant_details error: {e}")

    return {}


@app.get("/status/{name}", response_class=HTMLResponse, tags=["Checkin"])
async def status_view(request: Request, name: str):
    status = check_participant_status(name)
    details = get_participant_details(name)
    settings = get_active_settings()

    # Task 1.3: Use centralized READY calculation for template selection
    requirements = compute_requirements(settings)
    ready, _ = compute_ready_and_missing(status, requirements)

    template_name = "status_ready.html" if ready else "status_pending.html"
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "name": name,
            "status": status,
            "tag": details.get("tag", name),
            "event_name": details.get("event_name", "FGC Weekly"),
            "games": details.get("games", []),
            "n8n_token": N8N_WEBHOOK_TOKEN or "",
            "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
        },
    )

@app.get("/api/participant/{name}/status", tags=["API"])
async def api_participant_status(name: str):
    """
    JSON API endpoint for polling participant status.
    Used by status_pending.html to check if all requirements are met.
    Respects configurable requirements from settings.
    """
    status = check_participant_status(name)
    details = get_participant_details(name)
    settings = get_active_settings()

    # Use centralized helpers (Task 1.1, 1.2)
    requirements = compute_requirements(settings)
    ready, missing = compute_ready_and_missing(status, requirements)

    return {
        "ready": ready,
        "status": "Ready" if ready else "Pending",
        "missing": missing,
        "member": status.get("member", False),
        "payment": status.get("payment", False),
        "startgg": status.get("startgg", False),
        "name": name,
        "tag": details.get("tag", name),
        "startgg_events": details.get("games", []),
        "payment_expected": details.get("payment_expected", 0),
        # Include requirement settings so frontend can show/hide UI elements
        **requirements,
    }


@app.get("/", response_class=HTMLResponse, tags=["Checkin"])
async def root(request: Request):
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    return templates.TemplateResponse("checkin.html", {
        "request": request,
        "n8n_token": N8N_WEBHOOK_TOKEN or "",
        "require_membership": requirements["require_membership"],
    })

@app.get("/register", response_class=HTMLResponse, tags=["Checkin"])
async def register_form(request: Request):
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    tournament_slug = settings.get("active_event_slug", "")
    games = get_tournament_events(tournament_slug)
    return templates.TemplateResponse("register.html", {
        "request": request,
        "n8n_token": N8N_WEBHOOK_TOKEN or "",
        "swish_number": settings.get("swish_number", "123 456 78 90"),
        "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
        "games": games,
        # Configurable requirements - frontend hides sections that are not required
        **requirements,
    })

# Legacy alias so /register.html keeps working (old links/bookmarks)
@app.get("/register.html", response_class=HTMLResponse, tags=["Checkin"])
async def register_form_alias(request: Request):
    """Alias to support legacy links to /register.html."""
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    tournament_slug = settings.get("active_event_slug", "")
    games = get_tournament_events(tournament_slug)
    return templates.TemplateResponse("register.html", {
        "request": request,
        "n8n_token": N8N_WEBHOOK_TOKEN or "",
        "swish_number": settings.get("swish_number", "123 456 78 90"),
        "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
        "games": games,
        # Configurable requirements
        **requirements,
    })
# --- /CHANGED ---

@app.get("/players", tags=["Dashboard"])
async def get_players():
    return airtable_get_players()


@app.patch("/players/{record_id}/payment", tags=["Dashboard"])
async def update_payment_status(record_id: str, request: Request):
    """
    Update payment_valid status for a player in Airtable.
    Body: { "payment_valid": true/false }
    Used by TO dashboard to mark Swish payments.
    """
    try:
        body = await request.json()
        payment_valid = body.get("payment_valid", False)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Update payment using shared airtable_api
    result = update_checkin(record_id, {"payment_valid": payment_valid})

    if not result:
        raise HTTPException(status_code=500, detail="Airtable update failed")

    # Broadcast SSE update to all connected clients (including player status pages)
    await sse_manager.broadcast("update", {
        "type": "payment_updated",
        "record_id": record_id,
        "payment_valid": payment_valid
    })

    return {"success": True, "record_id": record_id, "payment_valid": payment_valid}

@app.get("/event-history", tags=["Dashboard"])
async def get_event_history():
    return airtable_get_event_history()


@app.patch("/api/player/games", tags=["Checkin"])
async def update_player_games(request: Request):
    """
    Update tournament_games_registered for a player.
    Used when player manually selects games in register.html.
    Body: { "tag": "playertag", "slug": "tournament-slug", "games": ["SF6", "Tekken 8"] }
    """
    try:
        body = await request.json()
        tag = (body.get("tag") or "").strip().lower()
        slug = body.get("slug") or ""
        games = body.get("games") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not tag or not slug:
        raise HTTPException(status_code=400, detail="tag and slug are required")

    if not isinstance(games, list):
        raise HTTPException(status_code=400, detail="games must be an array")

    # Find the player record by tag + slug using shared airtable_api
    checkin = get_checkin_by_tag(tag, slug)
    if not checkin:
        raise HTTPException(status_code=404, detail="Player not found")

    record_id = checkin["record_id"]

    # Update the record with games using shared airtable_api
    fields = {"tournament_games_registered": games} if games else {}
    result = update_checkin(record_id, fields, typecast=True)

    if not result:
        raise HTTPException(status_code=500, detail="Airtable update failed")

    logger.info(f"Updated games for {tag}: {games}")
    return {"success": True, "tag": tag, "games": games}


# === Start.gg OAuth (Admin Only) ===
@app.get("/login", tags=["OAuth"])
async def login(admin_key: str):
    if admin_key != OAUTH_ADMIN_KEY:
        return HTMLResponse("<h1>❌ Unauthorized</h1>", status_code=401)
    if not STARTGG_CLIENT_ID:
        return HTMLResponse("<h1>❌ Missing STARTGG_CLIENT_ID</h1>", status_code=500)

    scopes = "public.identity tournament.read event.read attendee.read"
    auth_url = (
        "https://start.gg/oauth/authorize"
        f"?client_id={STARTGG_CLIENT_ID}"
        f"&redirect_uri={quote(STARTGG_REDIRECT_URI)}"
        "&response_type=code"
        f"&scope={quote(scopes)}"
    )
    return RedirectResponse(url=auth_url)

@app.get("/auth/callback", tags=["OAuth"])
async def auth_callback(code: str, admin_key: str):
    if admin_key != OAUTH_ADMIN_KEY:
        return HTMLResponse("<h1>❌ Unauthorized</h1>", status_code=401)
    if not all([STARTGG_CLIENT_ID, STARTGG_CLIENT_SECRET]):
        return HTMLResponse("<h1>❌ Missing OAuth credentials</h1>", status_code=500)

    token_url = "https://api.start.gg/oauth/token"
    payload = {
        "client_id": STARTGG_CLIENT_ID,
        "client_secret": STARTGG_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": STARTGG_REDIRECT_URI,
        "code": code,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resp = SESSION.post(token_url, data=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return HTMLResponse(f"<h1>❌ Token exchange failed</h1><pre>{e}</pre>", status_code=500)

    tokens = resp.json()
    os.makedirs(os.path.dirname(OAUTH_TOKEN_PATH), exist_ok=True)
    with open(OAUTH_TOKEN_PATH, "w") as f:
        json.dump(tokens, f, indent=2)

    return HTMLResponse("<h1>✅ OAuth success</h1><p>Token saved successfully.</p>")

# === SSE Endpoints ===
@app.get("/api/events/stream", tags=["SSE"])
async def sse_stream(request: Request):
    """
    Server-Sent Events endpoint for real-time dashboard updates.
    Clients connect here and receive events when check-ins happen.
    """
    async def event_generator():
        queue = await sse_manager.connect()
        try:
            # Send initial connection confirmation
            yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    # Wait for events with timeout (sends keepalive)
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield message
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            await sse_manager.disconnect(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@app.post("/api/notify/checkin", tags=["SSE"])
async def notify_checkin(request: Request):
    """
    Endpoint for n8n to call after a check-in is saved.
    Broadcasts a 'checkin' event to all connected dashboard clients.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    # Broadcast to all connected SSE clients
    await sse_manager.broadcast("checkin", {
        "type": "new_checkin",
        "name": data.get("name", "Unknown"),
        "tag": data.get("tag", ""),
        "status": data.get("status", "Pending"),
        "timestamp": time.time(),
    })

    logger.info(f"Broadcasted checkin event for: {data.get('name', 'Unknown')}")
    return {"success": True, "clients_notified": len(sse_manager.clients)}


@app.post("/api/notify/update", tags=["SSE"])
async def notify_update(request: Request):
    """
    Generic update notification endpoint.
    Use for payment approvals, status changes, etc.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    event_type = data.get("event_type", "update")
    await sse_manager.broadcast(event_type, data)

    return {"success": True, "clients_notified": len(sse_manager.clients)}


# === Shutdown ===
@app.on_event("shutdown")
async def shutdown_event():
    await httpx_client.aclose()

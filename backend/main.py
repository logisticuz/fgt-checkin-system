# main.py

# Import FastAPI, templating, static files, and other dependencies
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import os
import json
import logging
from urllib.parse import quote

import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from airtable_api import get_players as airtable_get_players, get_event_history as airtable_get_event_history

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

# If n8n is protected with Basic Auth, set these for proxy + health
N8N_BASIC_AUTH_USER = os.getenv("N8N_BASIC_AUTH_USER")
N8N_BASIC_AUTH_PASSWORD = os.getenv("N8N_BASIC_AUTH_PASSWORD")

assert AIRTABLE_API_KEY, "❌ Missing AIRTABLE_API_KEY in environment!"
assert AIRTABLE_BASE_ID, "❌ Missing AIRTABLE_BASE_ID in environment!"

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
httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

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
# --- /CHANGED ---

# === n8n proxy ===
@app.api_route("/n8n/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_n8n(path: str, request: Request):
    """
    Simple pass-through proxy to the internal n8n service.
    - Do NOT stream the upstream response (avoid chunk/encoding mismatch).
    - Drop hop-by-hop and now-invalid headers (Content-Length, Content-Encoding, ETag, Date, etc).
    - Inject Basic Auth when configured and no Authorization header present.
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

    body = await request.body()

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
def check_participant_status(namn: str) -> dict:
    """
    Fetch and evaluate a participant’s registration status from Airtable.
    Uses flexible match on name/gametag (english fields) and keeps Swedish keys in the JSON for frontend compatibility.
    """
    status = {
        "namn": namn,
        "summary": "❌ Saknas helt",
        "medlem": False,   # frontend-kontrakt
        "swish": False,    # frontend-kontrakt (betalning ok)
        "startgg": False,  # frontend-kontrakt
    }

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    # Escape for Airtable formula (double single-quotes), + lower-case match
    def _esc_for_formula(s: str) -> str:
        return (s or "").replace("'", "''").lower()

    q = _esc_for_formula(namn)

    # Match both name and gametag
    formula = (
        "OR("
        f"LOWER(name)='{q}',"
        f"LOWER(gametag)='{q}'"
        ")"
    )

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?filterByFormula={quote(formula)}"
    r = safe_get(url, headers=headers)
    if not r:
        status["summary"] = "⚠️ Airtable-fel"
        return status

    try:
        records = r.json().get("records", [])
        if records:
            f = records[0].get("fields", {}) or {}

            # English field -> our Swedish keys
            status["medlem"] = bool(f.get("member"))

            # Start.gg – ok if flag or event_id exists
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

            # --- CHANGED: use _to_float for robust parsing ---
            amt = _to_float(f.get("payment_amount"))
            exp = _to_float(f.get("payment_expected"))
            swish_ok = (f.get("payment_valid") is True) or (exp > 0 and amt >= exp)
            status["swish"] = swish_ok
            # --- /CHANGED ---

            # Summary
            if all([status["medlem"], status["swish"], status["startgg"]]):
                status["summary"] = "✅ Klar för deltagande"
            elif not status["medlem"]:
                status["summary"] = "⏳ Saknar medlemskap"
            elif not status["swish"]:
                status["summary"] = "⏳ Saknar betalning"
            elif not status["startgg"]:
                status["summary"] = "⏳ Saknar turneringsregistrering"
            else:
                status["summary"] = "🟡 Delvis komplett"
    except Exception as e:
        logger.warning(f"Airtable parse error: {e}")
        status["summary"] = "⚠️ Airtable-fel"

    return status

# === Health ===
@app.get("/health", tags=["System"])
async def health_check():
    # Airtable check
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    test_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}?maxRecords=1"
    airtable_ok = bool(safe_get(test_url, headers=headers))

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
        "status": "ok" if airtable_ok and n8n_ok else "degraded",
        "components": {"checkin": True, "dashboard": True, "airtable": airtable_ok, "n8n": n8n_ok},
        "version": "1.0.0",
    }

# === Views ===
@app.get("/status/{namn}", response_class=HTMLResponse, tags=["Checkin"])
async def status_view(request: Request, namn: str):
    status = check_participant_status(namn)
    template_name = "status_ready.html" if status["medlem"] and status["swish"] else "status_pending.html"
    return templates.TemplateResponse(template_name, {"request": request, "namn": namn, "status": status})

@app.get("/", response_class=HTMLResponse, tags=["Checkin"])
async def root(request: Request):
    return templates.TemplateResponse("checkin.html", {"request": request})

@app.get("/register", response_class=HTMLResponse, tags=["Checkin"])
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

# --- CHANGED ---
# Legacy alias so /register.html keeps working (old links/bookmarks)
@app.get("/register.html", response_class=HTMLResponse, tags=["Checkin"])
async def register_form_alias(request: Request):
    """Alias to support legacy links to /register.html."""
    return templates.TemplateResponse("register.html", {"request": request})
# --- /CHANGED ---

@app.get("/players", tags=["Dashboard"])
async def get_players():
    return airtable_get_players()

@app.get("/event-history", tags=["Dashboard"])
async def get_event_history():
    return airtable_get_event_history()

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

# === Shutdown ===
@app.on_event("shutdown")
async def shutdown_event():
    await httpx_client.aclose()

"""Backend application bootstrap and compatibility imports."""

import app_state
from app_state import (
    ADAPTER,
    ADMIN_AUTH_COOKIE_TOKEN,
    CORSMiddleware,
    DATA_BACKEND,
    DEFAULT_TIMEOUT,
    Depends,
    EBAS_REGISTER_TIMEOUT_SECONDS,
    FastAPI,
    HTMLResponse,
    HTTPAdapter,
    HTTPException,
    INTEGRATION_ENGINE,
    INTERNAL_API_TOKEN,
    Jinja2Templates,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    N8N_WEBHOOK_TOKEN,
    RETRY,
    RedirectResponse,
    Request,
    Response,
    Retry,
    SESSION,
    SSEManager,
    SSE_TOKEN,
    STARTGG_API_KEY,
    STARTGG_CACHE,
    STARTGG_CACHE_TTL,
    Set,
    StaticFiles,
    StreamingResponse,
    _to_float,
    _to_str_phone,
    asynccontextmanager,
    asyncio,
    check_participant_status,
    compute_checkin_status,
    compute_ready_and_missing,
    compute_requirements,
    delete_checkin,
    expand_game_display_name,
    get_active_settings,
    get_checkin_by_name,
    get_checkin_by_record_id,
    get_checkin_by_tag,
    get_membership_flow,
    get_participant_details,
    get_tournament_events,
    httpx,
    httpx_client,
    json,
    logger,
    logging,
    lookup_checkin_status,
    os,
    requests,
    require_internal_soft,
    safe_get,
    sanitize_checkin_payload,
    secrets,
    slug_to_display_name,
    sse_manager,
    status_context,
    storage_api,
    storage_get_active_settings,
    storage_get_event_history,
    storage_get_players,
    templates,
    time,
    update_checkin,
    validate_checkin_payload,
)
from routers import admin, checkin, ebas_legacy, integrations, pages, sse, system
from routers.pages import (
    status_view,
    root,
    kiosk_mode,
    register_form,
    register_form_alias,
)
from routers.checkin import (
    api_participant_status,
    begin_checkin_endpoint,
    _orchestrate_integrations,
    orchestrate_checkin,
    update_player_games,
)
from routers.integrations import (
    update_startgg_registered_count,
    integration_result_endpoint,
    checkin_member_status_endpoint,
    notify_checkin,
    notify_update,
)
from routers.ebas_legacy import (
    _ebas_registration_response,
    _submit_legacy_ebas,
    ebas_register,
    _ebas_registrations,
    _ebas_registration_lock,
    _ebas_registration_tasks,
    EBAS_REGISTER_WAIT_SECONDS,
)
from routers.admin import (
    get_players,
    update_payment_status,
    get_event_history,
    admin_recheck_startgg,
    admin_bulk_recheck_startgg,
    archive_event_endpoint,
    reopen_event_endpoint,
    delete_archived_event_endpoint,
)
from routers.sse import (
    sse_stream,
)
from routers.system import (
    _integration_engine_health,
    _data_backend_health,
    health_check,
    health_check_deep,
)


app = FastAPI(
    docs_url="/docs" if os.getenv("FGT_API_DOCS") == "1" else None,
    redoc_url="/redoc" if os.getenv("FGT_API_DOCS") == "1" else None,
    openapi_url="/openapi.json" if os.getenv("FGT_API_DOCS") == "1" else None,
)


app.include_router(__import__("membership_api").router)


app.include_router(__import__("status_api").router)


@app.on_event("startup")
async def warn_missing_internal_token():
    if not INTERNAL_API_TOKEN:
        logger.warning("INTERNAL_API_TOKEN is unset; internal token monitoring is disabled.")


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


app.mount("/static", StaticFiles(directory="static"), name="static")


# Matching precedence is preserved within each area; membership/status stay first.
app.include_router(system.router)
app.include_router(pages.router)
app.include_router(checkin.router)
app.include_router(admin.router)
app.include_router(integrations.router)
app.include_router(ebas_legacy.router)
app.include_router(sse.router)


@app.on_event("shutdown")
async def shutdown_event():
    await httpx_client.aclose()

"""Pages routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    HTMLResponse,
    Request,
    check_participant_status,
    compute_ready_and_missing,
    compute_requirements,
    get_active_settings,
    get_membership_flow,
    get_participant_details,
    get_tournament_events,
    logger,
    lookup_checkin_status,
    slug_to_display_name,
    status_context,
    templates,
)

router = APIRouter()


@router.get("/status/{name}", response_class=HTMLResponse, tags=["Checkin"])
async def status_view(request: Request, name: str, kiosk: bool = False):
    capability = status_context(request)
    if not capability and any(key in request.query_params for key in ("t", "checkin_id")):
        return HTMLResponse(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Status link expired</title></head><body><main>"
            "<h1>Status link expired</h1><p>This status link is invalid or has expired. "
            "Please ask a TO for help.</p></main></body></html>",
            status_code=403,
        )
    status_context_fields = {}
    if capability:
        try:
            canonical = lookup_checkin_status(capability["checkin_id"], include_display=True)
        except Exception:
            logger.warning("Capability status page lookup is temporarily unavailable")
            canonical = {"found": False, "ready": False}
        ready = canonical.get("found") is True and canonical.get("ready") is True
        status = {
            **canonical,
            "member": canonical.get("member", False),
            "payment": canonical.get("payment_ok", False),
            "startgg": canonical.get("startgg", False),
            "summary": canonical.get("status", "Not found"),
        }
        details = {"tag": canonical.get("tag", ""), "games": canonical.get("games", [])}
        status_context_fields = {
            "found": canonical.get("found", False),
            "ready": ready,
            "missing": canonical.get("missing", []),
        }
        settings = get_active_settings()
        requirements = compute_requirements(settings)
    else:
        status = check_participant_status(name)
        details = get_participant_details(name)
        settings = get_active_settings()
        requirements = compute_requirements(settings)
        ready, _ = compute_ready_and_missing(status, requirements)

    template_name = "status_ready.html" if ready else "status_pending.html"
    event_slug = settings.get("active_event_slug", "")
    # Use custom display name if set, otherwise convert slug to nice name
    event_name = settings.get("event_display_name") or slug_to_display_name(event_slug)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "request": request,
            "name": name,
            **capability,
            **status_context_fields,
            "status": status,
            "tag": details.get("tag", name),
            "event_name": event_name,
            "event_slug": event_slug,
            "games": details.get("games", []),
            "swish_number": settings.get("swish_number", "123-456 78 90"),
            "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
            # Optional membership offer (shown on Ready page when membership not required AND not already a member)
            "offer_membership": settings.get("offer_membership") is True,
            "is_member": status.get("member", False),
            # Kiosk mode - auto-redirect after showing status
            "kiosk": kiosk,
            # Pass requirements so templates can show/hide elements
            **requirements,
        },
    )


@router.get("/", response_class=HTMLResponse, tags=["Checkin"])
async def root(request: Request):
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    return templates.TemplateResponse(
        request=request,
        name="checkin.html",
        context={
            "request": request,
            "membership_flow": get_membership_flow(),
            "kiosk": False,
            "collect_acquisition_source": settings.get("collect_acquisition_source") is True,
            # Pass all requirements so frontend can compute missing array correctly
            **requirements,
        },
    )


@router.get("/kiosk", response_class=HTMLResponse, tags=["Checkin"])
async def kiosk_mode(request: Request):
    """
    Kiosk mode for self-service check-in stations.
    After check-in, automatically redirects back to this page.
    """
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    return templates.TemplateResponse(
        request=request,
        name="checkin.html",
        context={
            "request": request,
            "membership_flow": get_membership_flow(),
            "kiosk": True,
            "collect_acquisition_source": settings.get("collect_acquisition_source") is True,
            **requirements,
        },
    )


@router.get("/register", response_class=HTMLResponse, tags=["Checkin"])
async def register_form(request: Request):
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    tournament_slug = settings.get("active_event_slug", "")
    games = get_tournament_events(tournament_slug)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "request": request,
            "membership_flow": get_membership_flow(),
            **__import__("membership_api").registration_context(request),
            "swish_number": settings.get("swish_number", "123 456 78 90"),
            "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
            "games": games,
            "collect_acquisition_source": settings.get("collect_acquisition_source") is True,
            # Configurable requirements - frontend hides sections that are not required
            **requirements,
        },
    )


@router.get("/register.html", response_class=HTMLResponse, tags=["Checkin"])
async def register_form_alias(request: Request):
    """Alias to support legacy links to /register.html."""
    settings = get_active_settings()
    requirements = compute_requirements(settings)
    tournament_slug = settings.get("active_event_slug", "")
    games = get_tournament_events(tournament_slug)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "request": request,
            "membership_flow": get_membership_flow(),
            **__import__("membership_api").registration_context(request),
            "swish_number": settings.get("swish_number", "123 456 78 90"),
            "swish_expected_per_game": settings.get("swish_expected_per_game", 25),
            "games": games,
            "collect_acquisition_source": settings.get("collect_acquisition_source") is True,
            # Configurable requirements
            **requirements,
        },
    )

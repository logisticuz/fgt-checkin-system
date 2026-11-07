"""Ebas legacy routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    EBAS_REGISTER_TIMEOUT_SECONDS,
    HTTPException,
    INTEGRATION_ENGINE,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    Request,
    Response,
    asyncio,
    get_checkin_by_tag,
    get_membership_flow,
    httpx,
    httpx_client,
    json,
    logger,
    sanitize_checkin_payload,
    time,
)

router = APIRouter()


EBAS_REGISTER_WAIT_SECONDS = 25.0


_ebas_registrations = {}


_ebas_registration_lock = asyncio.Lock()


_ebas_registration_tasks = set()


def _ebas_registration_response(status_code, **data):
    return Response(
        content=json.dumps(data), status_code=status_code, media_type="application/json"
    )


async def _submit_legacy_ebas(checkin_id, started_at, payload):
    """Finish one upstream request even after its browser request returns processing."""
    try:
        if INTEGRATION_ENGINE != "n8n":
            return _ebas_registration_response(
                503,
                error_code="integrations_disabled",
                state="skipped",
                user_message="Membership registration is handled by a TO today. Please ask a TO.",
            )
        headers = {"Content-Type": "application/json"}
        if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
            import base64

            token = base64.b64encode(
                f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()
            ).decode()
            headers["authorization"] = f"Basic {token}"

        response = await httpx_client.post(
            f"{N8N_INTERNAL}/webhook/ebas/register-v2",
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(EBAS_REGISTER_TIMEOUT_SECONDS, connect=5.0),
        )
        if response.status_code < 400:
            return response.json()

        logger.warning(
            "integration_error code=ebas_registration_http_error status=%s",
            response.status_code,
        )
        return _ebas_registration_response(502, detail="eBas registration service error")
    except httpx.TimeoutException:
        # n8n may still finish the request and report membership through its callback.
        logger.info("integration_error code=ebas_registration_timeout status=none")
        return _ebas_registration_response(202, success=True, state="processing")
    except Exception:
        logger.warning("integration_error code=ebas_registration_unavailable status=none")
        return _ebas_registration_response(502, detail="eBas registration service unavailable")
    finally:
        async with _ebas_registration_lock:
            if _ebas_registrations.get(checkin_id) == started_at:
                _ebas_registrations.pop(checkin_id, None)


@router.post("/api/ebas/register", tags=["Checkin"])
async def ebas_register(request: Request):
    """
    eBas membership registration endpoint.

    Replaces the direct n8n webhook call from register.html.
    Flow:
      1. Sanitize personnummer
      2. Find checkin by tag+slug → get checkin_id
      3. Forward to n8n eBas Register v2 webhook
      4. n8n calls Sverok API + reports member status via backend callback
      5. Return result to frontend (same format as old n8n response)
    """
    flow = get_membership_flow()
    if flow == "v2":
        return _ebas_registration_response(
            410,
            error_code="flow_v2",
            user_message=(
                "Membership registration has moved. "
                "Please use the membership step on the check-in page."
            ),
        )
    if flow == "off":
        return _ebas_registration_response(
            503,
            error_code="flow_off",
            user_message="Membership registration is handled by a TO today. Please ask a TO.",
        )

    if INTEGRATION_ENGINE != "n8n":
        return _ebas_registration_response(
            503,
            error_code="integrations_disabled",
            state="skipped",
            user_message="Membership registration is handled by a TO today. Please ask a TO.",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Sanitize
    sanitized = sanitize_checkin_payload(body)

    personnummer = sanitized.get("personnummer") or ""
    email = sanitized.get("email") or ""
    telephone = sanitized.get("telephone") or sanitized.get("telefon") or ""
    tag = (sanitized.get("tag") or "").strip().lower()
    slug = sanitized.get("slug") or ""

    # Find checkin_id by tag+slug (for n8n v2 callback)
    checkin = get_checkin_by_tag(tag, slug) if tag and slug else None
    if checkin and checkin.get("fields", {}).get("member") is True:
        return {"success": True, "already_member": True}
    checkin_id = (checkin or {}).get("record_id")
    if not checkin_id:
        raise HTTPException(status_code=400, detail="Check in before registering membership")
    if not personnummer:
        raise HTTPException(status_code=400, detail="Personnummer is required")

    # Forward to n8n eBas Register v2
    n8n_payload = {
        "checkin_id": checkin_id,
        "personnummer": personnummer,
        "email": email,
        "telephone": telephone,
        "tag": tag,
        "slug": slug,
    }

    async with _ebas_registration_lock:
        started_at = time.monotonic()
        expires_after = EBAS_REGISTER_TIMEOUT_SECONDS + 120
        for registered_id, registered_at in list(_ebas_registrations.items()):
            if started_at - registered_at >= expires_after:
                _ebas_registrations.pop(registered_id, None)
        if checkin_id in _ebas_registrations:
            return _ebas_registration_response(202, success=True, state="processing")
        _ebas_registrations[checkin_id] = started_at
        task = asyncio.create_task(_submit_legacy_ebas(checkin_id, started_at, n8n_payload))
        _ebas_registration_tasks.add(task)
        task.add_done_callback(_ebas_registration_tasks.discard)

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=EBAS_REGISTER_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _ebas_registration_response(202, success=True, state="processing")

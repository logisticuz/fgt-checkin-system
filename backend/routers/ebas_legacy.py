"""Legacy registration: durable submission guard and explicit, whitelisted outcomes."""

import re
from datetime import datetime, timezone

from app_state import (
    EBAS_REGISTER_TIMEOUT_SECONDS,
    INTEGRATION_ENGINE,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    HTTPException,
    Request,
    Response,
    asyncio,
    get_active_settings,
    get_checkin_by_record_id,
    get_checkin_by_tag,
    get_membership_flow,
    httpx,
    httpx_client,
    json,
    logger,
    sanitize_checkin_payload,
)
from fastapi import APIRouter
from status_api import verify_capability
from validation import sanitize_personnummer, validate_personnummer

from shared import postgres_api
from shared.ebas_client import _private_transport_logging

router = APIRouter()


EBAS_REGISTER_WAIT_SECONDS = 25.0


# Compatibility exports used by main.py; neither object controls submission now.
# The durable PostgreSQL row, never a process-local TTL, is the authority.
_ebas_registrations = {}
_ebas_registration_lock = asyncio.Lock()
_ebas_registration_tasks = set()

UNKNOWN_MESSAGE = (
    "Your registration may have gone through – a TO will check. Please don't register again."
)
PENDING_MESSAGE = (
    "Your registration was received – a TO will finish it with you. Please don't register again."
)
FIELD_ALIASES = {
    "personnummer": "personnummer",
    "socialsecuritynumber": "personnummer",
    "email": "email",
    "telephone": "telephone",
    "phone1": "telephone",
    "phone": "telephone",
}
PNR_PATTERN = re.compile(r"(?<!\d)(?:\d{12}|\d{10}|\d{8}[-+]\d{4}|\d{6}[-+]\d{4})(?!\d)")


def _ebas_registration_response(status_code, **data):
    return Response(
        content=json.dumps(data), status_code=status_code, media_type="application/json"
    )


def _held_registration(state="processing"):
    message = PENDING_MESSAGE if state == "local_sync_pending" else UNKNOWN_MESSAGE
    return _ebas_registration_response(
        202, success=False, state=state, retry_allowed=False, user_message=message
    )


def _field_errors(value):
    """Only the form's field names and bounded text messages may cross the public API."""
    result = {}
    if not isinstance(value, dict):
        return result
    for field, messages in value.items():
        public_field = FIELD_ALIASES.get(field)
        if public_field is None:
            continue
        messages = [messages] if isinstance(messages, str) else messages
        if not isinstance(messages, list):
            continue
        clean = [
            PNR_PATTERN.sub("[personal ID]", item.strip()[:200])
            for item in messages[:3]
            if isinstance(item, str) and item.strip()
        ]
        if clean:
            result.setdefault(public_field, []).extend(clean)
    return {field: messages[:3] for field, messages in result.items()}


def _provider_outcome(data):
    if not isinstance(data, dict):
        return "unknown"
    if (
        data.get("state") == "rejected"
        and data.get("success") is False
        and data.get("provider_accepted") is not True
        and data.get("verified_rejection") is True
        and data.get("request_result") == "fail"
        and isinstance(data.get("field_errors"), dict)
    ):
        return "rejected"
    if (
        data.get("state") == "local_sync_pending"
        and data.get("success") is False
        and data.get("provider_accepted") is True
    ) or (
        data.get("success") is True
        and data.get("registered") is True
        and data.get("state") == "registered"
        and data.get("member") is True
    ):
        return "accepted"
    return "unknown"


async def _submit_legacy_ebas(checkin_id, started_at, payload):
    """An ambiguous transport result never releases the committed tombstone."""
    # Recheck the emergency switch when queued work starts, before any transport.
    if INTEGRATION_ENGINE != "n8n":
        _ebas_registrations.pop(checkin_id, None)  # Compatibility cache only, not the DB guard.
        return _ebas_registration_response(
            503,
            error_code="integrations_disabled",
            state="skipped",
            user_message="Membership registration is handled by a TO today. Please ask a TO.",
        )
    state, data = "unknown", {}
    try:
        headers = {"Content-Type": "application/json"}
        if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
            import base64

            token = base64.b64encode(
                f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()
            ).decode()
            headers["authorization"] = f"Basic {token}"
        with _private_transport_logging():
            response = await httpx_client.post(
                f"{N8N_INTERNAL}/webhook/ebas/register-v2",
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(EBAS_REGISTER_TIMEOUT_SECONDS, connect=5.0),
            )
        if 200 <= response.status_code < 300:
            data = response.json()
            state = _provider_outcome(data)
        else:
            logger.warning(
                "integration_error code=ebas_registration_http_error status=%s",
                response.status_code,
            )
    except httpx.TimeoutException:
        logger.info("integration_error code=ebas_registration_timeout status=none")
    except Exception:
        logger.warning("integration_error code=ebas_registration_unavailable status=none")
    try:
        canonical = postgres_api.finish_legacy_registration(checkin_id, started_at, state)
    except Exception:
        # A failed completion write leaves the already committed in-flight guard intact.
        logger.warning("integration_error code=ebas_registration_storage_unavailable status=none")
        return _held_registration(
            "local_sync_pending" if state == "accepted" else "outcome_unknown"
        )
    if canonical.get("member") is True:
        return _ebas_registration_response(
            200, success=True, state="registered", registered=True, member=True, retry_allowed=False
        )
    if state == "rejected" and canonical["state"] == "rejected":
        return _ebas_registration_response(
            422,
            success=False,
            state="rejected",
            retry_allowed=True,
            field_errors=_field_errors(data.get("field_errors")),
            user_message="Please check the highlighted details or ask a TO for help.",
        )
    if state == "accepted" or canonical["state"] == "accepted":
        return _held_registration("local_sync_pending")
    return _held_registration("outcome_unknown")


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
    checkin_id = body.get("checkin_id")
    if not verify_capability(checkin_id, body.get("status_token")):
        raise HTTPException(status_code=403, detail="Invalid or expired check-in authorization")
    checkin = get_checkin_by_record_id(checkin_id) if get_checkin_by_record_id else None
    fields = (checkin or {}).get("fields", {})
    slug = fields.get("event_slug")
    if not checkin or not slug or slug != get_active_settings().get("active_event_slug"):
        raise HTTPException(status_code=404, detail="Player not found in active event")
    tag = (fields.get("tag") or "").strip().lower()
    supplied_tag = sanitized.get("tag")
    if (
        supplied_tag is not None
        and (not isinstance(supplied_tag, str) or supplied_tag.strip().lower() != tag)
    ) or (sanitized.get("slug") is not None and sanitized["slug"] != slug):
        raise HTTPException(
            status_code=403, detail="Check-in authorization does not match the target"
        )

    # Validate the untruncated input before committing any submission guard.
    raw_personnummer = body.get("personnummer")
    valid, validation_error = (
        validate_personnummer(raw_personnummer)
        if isinstance(raw_personnummer, str)
        else (False, "Personal ID is required")
    )
    if not valid and fields.get("member") is not True:
        raise HTTPException(status_code=400, detail=validation_error)
    personnummer = sanitize_personnummer(raw_personnummer) if valid else ""

    # Forward to n8n eBas Register v2
    n8n_payload = {
        "checkin_id": checkin_id,
        "personnummer": personnummer,
        "email": email,
        "telephone": telephone,
        "tag": tag,
        "slug": slug,
    }

    try:
        # An already-member lookup may omit the PNR, but a concurrent TO edit
        # must never turn that lookup into an unvalidated provider submission.
        claimed = postgres_api.claim_legacy_registration(checkin_id, slug, can_submit=valid)
    except Exception:
        logger.warning("integration_error code=ebas_registration_storage_unavailable status=none")
        return _ebas_registration_response(
            503,
            success=False,
            state="not_submitted",
            error_code="registration_not_started",
            retry_allowed=False,
            user_message="Registration could not start. Please ask a TO for help.",
        )
    if claimed.get("member") is True:
        return _ebas_registration_response(
            200,
            success=True,
            state="registered",
            registered=True,
            member=True,
            already_member=True,
            retry_allowed=False,
        )
    if claimed["state"] == "missing":
        raise HTTPException(status_code=404, detail="Player not found in active event")
    if claimed["state"] == "invalid_input":
        raise HTTPException(status_code=400, detail=validation_error or "Personal ID is required")
    if claimed["state"] != "claimed":
        held_state = "outcome_unknown"
        if claimed["state"] == "accepted":
            held_state = "local_sync_pending"
        elif claimed["state"] == "in_flight" and claimed.get("started_at"):
            age = (datetime.now(timezone.utc) - claimed["started_at"]).total_seconds()
            if age < EBAS_REGISTER_TIMEOUT_SECONDS:
                held_state = "processing"
        return _held_registration(held_state)
    n8n_payload["tag"] = claimed["tag"]
    task = asyncio.create_task(_submit_legacy_ebas(checkin_id, claimed["started_at"], n8n_payload))
    _ebas_registration_tasks.add(task)
    task.add_done_callback(_ebas_registration_tasks.discard)

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=EBAS_REGISTER_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _held_registration("processing")

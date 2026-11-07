"""Integrations routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    Depends,
    HTTPException,
    Request,
    logger,
    require_internal_soft,
    sse_manager,
    storage_api,
    storage_get_active_settings,
    time,
    update_checkin,
)

router = APIRouter()


@router.post(
    "/api/startgg/registered-count", tags=["Integrations"], dependencies=[Depends(require_internal_soft)]
)
async def update_startgg_registered_count(request: Request):
    """
    Receive Start.gg registered entrant count for current event.

    Callable by n8n workflow or dashboard to refresh tournament_entrants
    in settings.events_json before archiving.

    Body: { "event_slug": "...", "startgg_registered_count": 42 }
    Idempotent: replaces existing tournament_entrants value.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    count = body.get("startgg_registered_count")
    if count is None:
        raise HTTPException(status_code=400, detail="startgg_registered_count is required")

    try:
        count = int(count)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="startgg_registered_count must be an integer")

    # Update events_json.tournament_entrants in active settings
    settings = storage_get_active_settings() or {}
    events_json = settings.get("events_json")

    if isinstance(events_json, dict):
        events_json["tournament_entrants"] = count
    elif isinstance(events_json, list):
        events_json = {"tournament_entrants": count, "events": events_json}
    else:
        events_json = {"tournament_entrants": count, "events": []}

    # Find active settings record to update
    get_settings_fn = getattr(storage_api, "get_active_settings_with_id", None)
    if not get_settings_fn:
        raise HTTPException(status_code=501, detail="Not available for current backend")

    settings_data = get_settings_fn()
    if not settings_data:
        raise HTTPException(status_code=404, detail="No active settings found")

    update_fn = getattr(storage_api, "update_settings", None)
    if not update_fn:
        raise HTTPException(status_code=501, detail="Not available for current backend")

    result = update_fn(settings_data["record_id"], {"events_json": events_json})
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update settings")

    logger.info(f"Updated startgg_registered_count to {count}")
    return {"success": True, "startgg_registered_count": count}


@router.post(
    "/api/integration/result", tags=["Integrations"], dependencies=[Depends(require_internal_soft)]
)
async def integration_result_endpoint(request: Request):
    """Apply integration result from n8n/external checks to a checkin."""
    apply_fn = getattr(storage_api, "apply_integration_result", None)
    if not apply_fn:
        raise HTTPException(status_code=501, detail="integration_result is not available for current backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    checkin_id = (body.get("checkin_id") or "").strip()
    source = (body.get("source") or "").strip()
    if not checkin_id or not source:
        raise HTTPException(status_code=400, detail="checkin_id and source are required")

    ok = bool(body.get("ok", False))
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else None
    fetched_at = body.get("fetched_at")

    try:
        result = apply_fn(
            checkin_id=checkin_id,
            source=source,
            ok=ok,
            data=data,
            error=error,
            fetched_at=fetched_at,
            state=body.get("state"),
            attempt_id=body.get("attempt_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"integration result failed for '{checkin_id}': {e}")
        raise HTTPException(status_code=500, detail=f"integration result failed: {e}")

    return {"success": True, **result}


@router.post(
    "/api/checkin/{checkin_id}/member-status",
    tags=["Integrations"],
    dependencies=[Depends(require_internal_soft)],
)
async def checkin_member_status_endpoint(checkin_id: str, request: Request):
    """Thin endpoint for eBas flow to set member status by checkin_id."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    member = bool(body.get("member", False))

    result = storage_api.apply_legacy_member_status(checkin_id, member)
    if not result:
        raise HTTPException(status_code=500, detail="Storage update failed")

    return {
        "success": True,
        "checkin_id": checkin_id,
        "member": result["fields"].get("member", False),
        "status": result["fields"]["status"],
    }


@router.post("/api/notify/checkin", tags=["SSE"], dependencies=[Depends(require_internal_soft)])
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


@router.post("/api/notify/update", tags=["SSE"], dependencies=[Depends(require_internal_soft)])
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

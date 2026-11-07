"""Admin routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    HTTPException,
    INTEGRATION_ENGINE,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    Request,
    asyncio,
    compute_ready_and_missing,
    compute_requirements,
    get_active_settings,
    httpx,
    httpx_client,
    logger,
    sse_manager,
    storage_api,
    storage_get_event_history,
    storage_get_players,
    time,
    update_checkin,
)

router = APIRouter()


@router.get("/players", tags=["Dashboard"])
async def get_players():
    return storage_get_players()


@router.patch("/players/{record_id}/payment", tags=["Dashboard"])
async def update_payment_status(record_id: str, request: Request):
    """
    Update payment_valid status for a player in storage backend.
    Body: { "payment_valid": true/false }
    Used by TO dashboard to mark Swish payments.
    """
    try:
        body = await request.json()
        payment_valid = body.get("payment_valid", False)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Update payment using shared storage backend
    result = update_checkin(record_id, {"payment_valid": payment_valid})

    if not result:
        raise HTTPException(status_code=500, detail="Storage update failed")

    # Broadcast SSE update to all connected clients (including player status pages)
    await sse_manager.broadcast("update", {
        "type": "payment_updated",
        "record_id": record_id,
        "payment_valid": payment_valid
    })

    return {"success": True, "record_id": record_id, "payment_valid": payment_valid}


@router.get("/event-history", tags=["Dashboard"])
async def get_event_history():
    return storage_get_event_history()


@router.post("/api/admin/recheck-startgg", tags=["Admin"])
async def admin_recheck_startgg(request: Request):
    """
    Re-run Start.gg validation for a single checked-in player.

    Called from dashboard after TO corrects a tag or wants to sync games.
    Uses the n8n Start.gg Check sub-workflow, then applies the result
    and recomputes status.

    Body: { "record_id": "..." }
    """
    if INTEGRATION_ENGINE != "n8n":
        raise HTTPException(
            status_code=503,
            detail={"error_code": "integrations_disabled", "state": "skipped"},
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    record_id = (body.get("record_id") or "").strip()
    if not record_id:
        raise HTTPException(status_code=400, detail="record_id is required")

    # 1. Read existing checkin
    get_by_id = getattr(storage_api, "get_checkin_by_record_id", None)
    if not get_by_id:
        raise HTTPException(status_code=501, detail="Not available for current backend")

    checkin = get_by_id(record_id)
    if not checkin:
        raise HTTPException(status_code=404, detail="Checkin not found")

    fields = checkin.get("fields", {})
    tag = (fields.get("tag") or "").strip()
    slug = (fields.get("event_slug") or "").strip()
    player_name = fields.get("name") or tag or "Unknown"

    if not tag:
        raise HTTPException(status_code=400, detail="Player has no tag — cannot check Start.gg")
    if not slug:
        raise HTTPException(status_code=400, detail="No event_slug on checkin record")

    # Save previous state for response
    prev_startgg = bool(fields.get("startgg"))
    prev_games = fields.get("tournament_games_registered") or []

    # 2. Call n8n Start.gg Check sub-workflow
    n8n_url = f"{N8N_INTERNAL}/webhook/startgg/check"
    n8n_headers = {"Content-Type": "application/json"}
    if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
        import base64
        b64 = base64.b64encode(
            f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()
        ).decode()
        n8n_headers["authorization"] = f"Basic {b64}"

    try:
        attempt_id = storage_api.start_integration_attempt(record_id, sources=("startgg",))
        n8n_resp = await httpx_client.post(
            n8n_url,
            json={"tag": tag, "slug": slug, "attempt_id": attempt_id},
            headers=n8n_headers,
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"n8n Start.gg check failed: {e}")

    if n8n_resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"n8n returned {n8n_resp.status_code}: {n8n_resp.text[:200]}",
        )

    try:
        n8n_data = n8n_resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Start.gg verification unavailable")
    if (
        not isinstance(n8n_data, dict)
        or n8n_data.get("ok", True) is not True
        or n8n_data.get("state") in ("unavailable", "skipped")
        or n8n_data.get("error")
        or not isinstance(n8n_data.get("isRegistered"), bool)
    ):
        raise HTTPException(status_code=502, detail="Start.gg verification unavailable")
    # A successful negative lookup is a verified result, independent of registration.
    is_registered = n8n_data["isRegistered"]
    result_state = "verified_true" if is_registered else "verified_false"
    events = n8n_data.get("events") or []
    startgg_email = n8n_data.get("email") or None

    # 3. Apply result via existing integration mechanism
    apply_fn = getattr(storage_api, "apply_integration_result", None)
    if apply_fn:
        result_data = {"registered": is_registered, "events": events}
        if startgg_email:
            result_data["email"] = startgg_email
        applied = apply_fn(
            checkin_id=record_id,
            source="startgg",
            ok=True,
            data=result_data,
            state=result_state,
            attempt_id=attempt_id,
            recompute_payment_expected=True,
        )

        if applied.get("state") == "stale_rejected":
            raise HTTPException(status_code=409, detail={"state": "stale_rejected"})

    # 4. Read the accepted/current stored result; status and pricing were updated atomically.
    updated_checkin = get_by_id(record_id)
    uf = (updated_checkin or {}).get("fields", {})

    final_status = uf.get("status") or "Pending"
    new_games = uf.get("tournament_games_registered") or []
    is_registered = bool(uf.get("startgg"))

    # 6. Broadcast SSE
    await sse_manager.broadcast("update", {
        "type": "recheck_startgg",
        "record_id": record_id,
        "name": player_name,
        "tag": tag,
        "startgg": is_registered,
        "status": final_status,
        "timestamp": time.time(),
    })

    return {
        "success": True,
        "record_id": record_id,
        "player_name": player_name,
        "startgg": is_registered,
        "events": new_games,
        "status": final_status,
        "previous_startgg": prev_startgg,
        "previous_games": prev_games,
    }


@router.post("/api/admin/bulk-recheck-startgg", tags=["Admin"])
async def admin_bulk_recheck_startgg():
    """
    Re-run Start.gg validation for ALL players in active event.

    Loops through each player with a tag, calls n8n Start.gg Check,
    applies result (email, events, startgg flag), and recomputes status.
    Includes a small delay between calls to respect Start.gg rate limits.
    """
    if INTEGRATION_ENGINE != "n8n":
        raise HTTPException(
            status_code=503,
            detail={"error_code": "integrations_disabled", "state": "skipped"},
        )

    get_checkins = getattr(storage_api, "get_checkins", None)
    get_by_id = getattr(storage_api, "get_checkin_by_record_id", None)
    apply_fn = getattr(storage_api, "apply_integration_result", None)
    if not all([get_checkins, get_by_id, apply_fn]):
        raise HTTPException(status_code=501, detail="Not available for current backend")

    settings = get_active_settings()
    active_slug = settings.get("active_event_slug") or ""
    if not active_slug:
        raise HTTPException(status_code=400, detail="No active event configured")

    checkins = get_checkins(slug=active_slug)
    requirements = compute_requirements(settings)

    n8n_url = f"{N8N_INTERNAL}/webhook/startgg/check"
    n8n_headers = {"Content-Type": "application/json"}
    if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
        import base64
        b64 = base64.b64encode(
            f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()
        ).decode()
        n8n_headers["authorization"] = f"Basic {b64}"

    total = 0
    checked = 0
    emails_found = 0
    errors = []

    for ci in checkins:
        record_id = ci.get("record_id")
        tag = (ci.get("tag") or "").strip()
        slug = (ci.get("event_slug") or "").strip()
        player_name = ci.get("name") or tag or "Unknown"

        if not tag or not slug or not record_id:
            continue

        total += 1

        try:
            attempt_id = storage_api.start_integration_attempt(record_id, sources=("startgg",))
            n8n_resp = await httpx_client.post(
                n8n_url,
                json={"tag": tag, "slug": slug, "attempt_id": attempt_id},
                headers=n8n_headers,
                timeout=httpx.Timeout(15.0, connect=5.0),
            )

            if n8n_resp.status_code >= 400:
                errors.append(f"{tag}: n8n {n8n_resp.status_code}")
                continue

            n8n_data = n8n_resp.json()
            if (
                not isinstance(n8n_data, dict)
                or n8n_data.get("ok", True) is not True
                or n8n_data.get("state") in ("unavailable", "skipped")
                or n8n_data.get("error")
                or not isinstance(n8n_data.get("isRegistered"), bool)
            ):
                errors.append(f"{tag}: Start.gg verification unavailable")
                continue
            is_registered = n8n_data["isRegistered"]
            result_state = "verified_true" if is_registered else "verified_false"
            events = n8n_data.get("events") or []
            startgg_email = n8n_data.get("email") or None

            result_data = {"registered": is_registered, "events": events}
            if startgg_email:
                result_data["email"] = startgg_email
                emails_found += 1

            applied = apply_fn(
                checkin_id=record_id,
                source="startgg",
                ok=True,
                data=result_data,
                state=result_state,
                attempt_id=attempt_id,
                recompute_payment_expected=True,
            )

            if applied.get("state") == "stale_rejected":
                continue

            # Recompute status
            updated = get_by_id(record_id)
            uf = (updated or {}).get("fields", {})
            status_dict = {
                "member": bool(uf.get("member")),
                "payment": bool(uf.get("payment_valid")),
                "startgg": bool(uf.get("startgg")),
            }
            ready, _ = compute_ready_and_missing(status_dict, requirements)
            final_status = "Ready" if ready else "Pending"
            update_checkin(record_id, {"status": final_status})

            checked += 1

        except Exception as e:
            errors.append(f"{tag}: {str(e)[:100]}")

        # Small delay to respect Start.gg rate limits
        await asyncio.sleep(0.3)

    # Broadcast single SSE update when done
    await sse_manager.broadcast("update", {
        "type": "bulk_recheck_startgg",
        "total": total,
        "checked": checked,
        "emails_found": emails_found,
        "timestamp": time.time(),
    })

    return {
        "success": True,
        "total": total,
        "checked": checked,
        "emails_found": emails_found,
        "errors": errors,
    }


@router.post("/api/archive/event", tags=["Dashboard"])
async def archive_event_endpoint(request: Request):
    """Archive current event into event_archive/event_stats using storage backend."""
    archive_fn = getattr(storage_api, "archive_event", None)
    if not archive_fn:
        raise HTTPException(status_code=501, detail="Archive is not available for current backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_slug = (body.get("event_slug") or "").strip()
    if not event_slug:
        raise HTTPException(status_code=400, detail="event_slug is required")

    try:
        result = archive_fn(
            event_slug,
            event_date=body.get("event_date"),
            event_display_name=body.get("event_display_name") or "",
            swish_expected_per_game=body.get("swish_expected_per_game") or 0,
            startgg_snapshot=body.get("startgg_snapshot"),
            user=body.get("user") or None,
            clear_active=bool(body.get("clear_active", False)),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Archive failed for '{event_slug}': {e}")
        raise HTTPException(status_code=500, detail=f"Archive failed: {e}")

    return {"success": True, **result}


@router.post("/api/archive/reopen", tags=["Dashboard"])
async def reopen_event_endpoint(request: Request):
    """Reopen an archived event and optionally restore active check-ins."""
    reopen_fn = getattr(storage_api, "reopen_event", None)
    if not reopen_fn:
        raise HTTPException(status_code=501, detail="Reopen is not available for current backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_slug = (body.get("event_slug") or "").strip()
    if not event_slug:
        raise HTTPException(status_code=400, detail="event_slug is required")

    try:
        result = reopen_fn(
            event_slug,
            restore_active=bool(body.get("restore_active", True)),
            user=body.get("user") or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Reopen failed for '{event_slug}': {e}")
        raise HTTPException(status_code=500, detail=f"Reopen failed: {e}")

    return {"success": True, **result}


@router.post("/api/archive/delete", tags=["Dashboard"])
async def delete_archived_event_endpoint(request: Request):
    """Delete archived historical data for an event (event_archive + event_stats)."""
    delete_fn = getattr(storage_api, "delete_archived_event", None)
    if not delete_fn:
        raise HTTPException(status_code=501, detail="Delete archive is not available for current backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_slug = (body.get("event_slug") or "").strip()
    reason = (body.get("reason") or "").strip()
    if not event_slug:
        raise HTTPException(status_code=400, detail="event_slug is required")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    try:
        result = delete_fn(
            event_slug,
            reason=reason,
            user=body.get("user") or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Delete archive failed for '{event_slug}': {e}")
        raise HTTPException(status_code=500, detail=f"Delete archive failed: {e}")

    return {"success": True, **result}

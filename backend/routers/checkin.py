"""Checkin routes moved from main.py without handler changes."""

from fastapi import APIRouter
from status_api import verify_capability
from shared.ebas_client import _private_transport_logging

from app_state import (
    Depends,
    EBAS_REGISTER_TIMEOUT_SECONDS,
    HTTPException,
    INTEGRATION_ENGINE,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    Request,
    check_participant_status,
    compute_ready_and_missing,
    compute_requirements,
    get_active_settings,
    get_checkin_by_record_id,
    get_checkin_by_tag,
    get_participant_details,
    get_tournament_events,
    httpx,
    httpx_client,
    logger,
    require_internal_soft,
    sanitize_checkin_payload,
    sse_manager,
    storage_api,
    storage_get_active_settings,
    time,
    update_checkin,
    validate_checkin_payload,
)

router = APIRouter()


@router.get("/api/participant/{name}/status", tags=["API"])
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


@router.post("/api/checkin/begin", tags=["Integrations"], dependencies=[Depends(require_internal_soft)])
async def begin_checkin_endpoint(request: Request):
    """Start or upsert a check-in attempt and return checkin_id."""
    begin_fn = getattr(storage_api, "begin_checkin", None)
    if not begin_fn:
        raise HTTPException(status_code=501, detail="begin_checkin is not available for current backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_slug = (body.get("event_slug") or "").strip()
    if not event_slug:
        event_slug = get_active_settings().get("active_event_slug", "")
    if not event_slug:
        raise HTTPException(status_code=400, detail="event_slug is required")

    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    if isinstance(payload, dict) and not payload.get("added_via"):
        payload["added_via"] = "api"

    try:
        result = begin_fn(event_slug, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"checkin begin failed for '{event_slug}': {e}")
        raise HTTPException(status_code=500, detail=f"checkin begin failed: {e}")

    return {"success": True, **result}


def _orchestrate_integrations(response, data, payload):
    """Describe this run without inferring provider availability from stored flags."""
    sources = ("ebas", "startgg")
    if INTEGRATION_ENGINE != "n8n":
        return dict.fromkeys(sources, "skipped"), True

    integrations = dict.fromkeys(sources, "unavailable")
    if not isinstance(data, dict):
        return integrations, True

    results = data.get("results")
    reported = data.get("integrations")
    transport_ok = response is not None and 200 <= response.status_code < 300
    states = {
        "ok": "ok",
        "verified_true": "ok",
        "verified_false": "ok",
        "unavailable": "unavailable",
        "skipped": "skipped",
    }
    for source in sources:
        if isinstance(results, dict) and source in results:
            report = results[source]
        elif isinstance(reported, dict) and source in reported:
            report = reported[source]
        elif isinstance(data.get(source), dict):
            report = data[source]
        else:
            # Legacy summaries conflate provider failures with negative results. Only
            # a positive result establishes availability without explicit telemetry.
            if transport_ok and data.get("integration_complete") is True:
                key = "member" if source == "ebas" else "startgg"
                if source == "ebas" and not payload.get("personnummer"):
                    integrations[source] = "skipped"
                elif data.get(key) is True:
                    integrations[source] = "ok"
            continue
        state = report.get("state") if isinstance(report, dict) else report
        if isinstance(state, str):
            integrations[source] = states.get(state, "unavailable")
    return integrations, "unavailable" in integrations.values()


@router.post("/api/checkin/orchestrate", tags=["Checkin"])
async def orchestrate_checkin(request: Request):
    """
    Main check-in orchestration endpoint.

    Replaces the direct n8n webhook call from the frontend.
    Flow:
      1. Validate & sanitize form data
      2. begin_checkin() in Postgres (dedupe by tag+slug)
      3. Forward to n8n v5 webhook (Start.gg + eBas checks)
      4. n8n calls /api/integration/result during execution
      5. Re-read updated checkin, compute final status
      6. Update status, broadcast SSE, return result

    Returns response compatible with checkin.html:
      { ready, slug, missing[], member, payment_valid, startgg }
    """
    begin_fn = getattr(storage_api, "begin_checkin", None)
    if not begin_fn:
        raise HTTPException(status_code=501, detail="Orchestration requires postgres backend")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Validate check-in payload (same as n8n proxy did)
    errors = validate_checkin_payload(body)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # Sanitize
    sanitized = sanitize_checkin_payload(body)

    # Get active settings
    settings = get_active_settings()
    slug = settings.get("active_event_slug", "")
    if not slug:
        raise HTTPException(status_code=400, detail="No active event configured")

    requirements = compute_requirements(settings)
    per_game = settings.get("swish_expected_per_game", 25)

    # Extract fields
    tag = (sanitized.get("tag") or "").strip()
    name = sanitized.get("namn") or sanitized.get("name") or ""
    telefon = sanitized.get("telefon") or sanitized.get("telephone") or ""
    email = sanitized.get("email") or ""
    personnummer = sanitized.get("personnummer") or ""
    acquisition_source = sanitized.get("acquisition_source")

    # 1. begin_checkin (dedupe by tag+slug)
    try:
        checkin_result = begin_fn(
            slug,
            {
                "name": name,
                "tag": tag,
                "email": email,
                "telephone": telefon,
                "status": "Pending",
                "added_via": "startgg_flow",
                "acquisition_source": acquisition_source,
            },
            public=True,
        )
    except Exception as e:
        logger.exception(f"begin_checkin failed: {e}")
        raise HTTPException(status_code=500, detail=f"Check-in creation failed: {e}")

    checkin_id = checkin_result["checkin_id"]
    ownership_verified = checkin_result.get("ownership_verified") is True
    logger.info(f"Orchestrate: checkin_id={checkin_id}, tag={tag}, slug={slug}")

    # 2. Forward to n8n v5 webhook (synchronous - n8n calls /api/integration/result internally)
    n8n_response_data = {}
    n8n_resp = None
    n8n_payload = {}
    if INTEGRATION_ENGINE == "n8n":
        n8n_payload = {
            "checkin_id": checkin_id,
            "tag": tag,
            "slug": slug,
            "name": name,
        }
        if ownership_verified:
            n8n_payload["personnummer"] = personnummer
            n8n_payload["attempt_id"] = storage_api.start_integration_attempt(checkin_id)
        else:
            n8n_payload["attempt_id"] = storage_api.start_integration_attempt(
                checkin_id, sources=("startgg",)
            )

        n8n_url = f"{N8N_INTERNAL}/webhook/checkin/validate-v5"

        try:
            n8n_headers = {"Content-Type": "application/json"}
            if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
                import base64
                token = base64.b64encode(
                    f"{N8N_BASIC_AUTH_USER}:{N8N_BASIC_AUTH_PASSWORD}".encode()
                ).decode()
                n8n_headers["authorization"] = f"Basic {token}"

            with _private_transport_logging():
                n8n_resp = await httpx_client.post(
                    n8n_url,
                    json=n8n_payload,
                    headers=n8n_headers,
                    timeout=httpx.Timeout(EBAS_REGISTER_TIMEOUT_SECONDS, connect=5.0),
                )

            if n8n_resp.status_code < 400:
                n8n_response_data = n8n_resp.json()
            else:
                logger.warning(
                    "integration_error code=n8n_http_error status=%s", n8n_resp.status_code
                )
        except Exception:
            logger.warning("integration_error code=n8n_transport_error status=none")
            # Continue - checkin is created, integrations just didn't run

    # 3. Re-read the updated checkin record
    get_by_id = getattr(storage_api, "get_checkin_by_record_id", None)
    checkin_record = None
    if get_by_id:
        checkin_record = get_by_id(checkin_id)

    if not checkin_record:
        # Fallback to tag+slug lookup
        checkin_record = get_checkin_by_tag(tag.lower(), slug) if tag else None

    fields = (checkin_record or {}).get("fields", {})

    # 4. Compute final status based on requirements
    member_ok = bool(fields.get("member"))
    startgg_ok = bool(fields.get("startgg"))
    payment_ok = bool(fields.get("payment_valid"))

    status_dict = {
        "member": member_ok,
        "payment": payment_ok,
        "startgg": startgg_ok,
    }
    ready, missing = compute_ready_and_missing(status_dict, requirements)
    final_status = "Ready" if ready else "Pending"

    # 5. Update status in DB
    record_id = (checkin_record or {}).get("record_id", checkin_id)
    update_checkin(record_id, {"status": final_status})

    # 6. Broadcast SSE
    await sse_manager.broadcast("checkin", {
        "type": "new_checkin",
        "name": name,
        "tag": tag,
        "status": final_status,
        "timestamp": time.time(),
    })

    # 7. Return response compatible with checkin.html
    integrations, degraded = _orchestrate_integrations(
        n8n_resp, n8n_response_data, n8n_payload
    )
    if not ownership_verified:
        integrations["ebas"] = "skipped"
        degraded = INTEGRATION_ENGINE != "n8n" or "unavailable" in integrations.values()
    membership_unknown = integrations["ebas"] != "ok"
    response_missing = [
        item for item in missing if not (membership_unknown and item == "Membership")
    ]
    result = {
        "ready": ready,
        "slug": slug,
        "missing": response_missing,
        "member": member_ok if member_ok or not membership_unknown else None,
        "payment_valid": payment_ok,
        "startgg": startgg_ok,
        "status": final_status,
        "name": name,
        "tag": tag,
        "checkin_id": checkin_id,
        "integrations": integrations,
        "degraded": degraded,
    }
    if ownership_verified:
        result["status_token"] = __import__("status_api").capability_token(checkin_id)
    else:
        result["ownership"] = "unverified"
        result["reason"] = "ownership_unverified"
    return result


@router.patch("/api/player/games", tags=["Checkin"])
async def update_player_games(request: Request):
    """
    Update tournament_games_registered and payment_expected for a player.
    Used when player manually selects games in register.html.
    Body: { "checkin_id": "record-id", "games": ["Street Fighter 6", "Tekken 8"] }

    Also calculates payment_expected = len(games) * swish_expected_per_game
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        checkin_id = body.get("checkin_id")
        games = body.get("games", [])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(checkin_id, str) or not checkin_id.strip():
        raise HTTPException(status_code=400, detail="checkin_id is required")

    if not verify_capability(checkin_id.strip(), body.get("status_token")):
        raise HTTPException(status_code=403, detail="Invalid or expired check-in authorization")

    if not isinstance(games, list) or not all(isinstance(game, str) for game in games):
        raise HTTPException(status_code=400, detail="games must be an array of strings")

    checkin = get_checkin_by_record_id(checkin_id.strip())
    if not checkin:
        raise HTTPException(status_code=404, detail="Player not found")

    record_id = checkin["record_id"]
    tag = checkin["fields"].get("tag") or ""

    # Get settings to calculate payment_expected
    settings = storage_get_active_settings() or {}
    slug = settings.get("active_event_slug") or ""
    if not slug:
        raise HTTPException(status_code=400, detail="No active event configured")
    if checkin["fields"].get("event_slug") != slug:
        raise HTTPException(status_code=404, detail="Player not found in active event")

    configured = settings.get("default_game")
    if isinstance(configured, list) and configured:
        allowed_games = list(dict.fromkeys(game for game in configured if isinstance(game, str)))
    else:
        allowed_games = list(
            dict.fromkeys(
                event["name"]
                for event in get_tournament_events(slug)
                if isinstance(event, dict) and isinstance(event.get("name"), str)
            )
        )
    if any(game not in allowed_games for game in games):
        raise HTTPException(
            status_code=400, detail={"error": "Unknown game", "allowed_games": allowed_games}
        )
    games = list(dict.fromkeys(games))
    per_game = settings.get("swish_expected_per_game", 25)
    payment_expected = len(games) * per_game

    # Update the record with games AND payment_expected
    fields = {
        "tournament_games_registered": games,
        "payment_expected": payment_expected,
    }
    result = update_checkin(record_id, fields, typecast=True)

    if not result:
        raise HTTPException(status_code=500, detail="Storage update failed")

    # Broadcast SSE update for dashboard
    await sse_manager.broadcast(
        "update",
        {
            "type": "games_updated",
            "record_id": record_id,
            "tag": tag,
            "games": games,
            "payment_expected": payment_expected,
        },
    )

    logger.info(f"Updated games for {tag}: {games}, payment_expected: {payment_expected}")
    return {"success": True, "tag": tag, "games": games, "payment_expected": payment_expected}

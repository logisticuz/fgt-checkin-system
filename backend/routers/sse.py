"""Sse routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    ADMIN_AUTH_COOKIE_TOKEN,
    HTTPException,
    Request,
    SSE_TOKEN,
    StreamingResponse,
    asyncio,
    json,
    sse_manager,
)

router = APIRouter()


@router.get("/api/events/stream", tags=["SSE"])
async def sse_stream(request: Request, token: str = None):
    """
    Server-Sent Events endpoint for real-time dashboard updates.
    Clients connect here and receive events when check-ins happen.

    In production, requires SSE_TOKEN for authentication (since Basic Auth
    doesn't work with EventSource). Token is passed as query parameter.
    """
    # Validate SSE token if configured
    if SSE_TOKEN and token != SSE_TOKEN:
        cookie_token = request.cookies.get("fgc_admin_auth")
        if not (ADMIN_AUTH_COOKIE_TOKEN and cookie_token == ADMIN_AUTH_COOKIE_TOKEN):
            raise HTTPException(status_code=401, detail="Invalid or missing SSE token")
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

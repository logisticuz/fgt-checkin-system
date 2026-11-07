"""System routes moved from main.py without handler changes."""

from fastapi import APIRouter

from app_state import (
    DATA_BACKEND,
    INTEGRATION_ENGINE,
    N8N_BASIC_AUTH_PASSWORD,
    N8N_BASIC_AUTH_USER,
    N8N_INTERNAL,
    get_membership_flow,
    httpx_client,
    storage_get_active_settings,
)

router = APIRouter()


async def _integration_engine_health() -> bool:
    """Check integration engine status based on configured engine."""
    if INTEGRATION_ENGINE != "n8n":
        return True

    try:
        auth = None
        if N8N_BASIC_AUTH_USER and N8N_BASIC_AUTH_PASSWORD:
            auth = (N8N_BASIC_AUTH_USER, N8N_BASIC_AUTH_PASSWORD)
        resp = await httpx_client.get(f"{N8N_INTERNAL}/healthz", auth=auth)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def _data_backend_health() -> bool:
    """Check data backend status with lightweight call."""
    try:
        storage_get_active_settings()
        return True
    except Exception:
        return False


@router.get("/health", tags=["System"])
async def health_check():
    """
    Lightweight health check for Docker/orchestration.
    Does NOT call external provider APIs to avoid unnecessary traffic.
    Use /health/deep for full diagnostics.
    """
    integration_ok = await _integration_engine_health()

    return {
        "status": "ok" if integration_ok else "degraded",
        "membership_flow": get_membership_flow(),
        "components": {
            "checkin": True,
            "dashboard": True,
            "integration": integration_ok,
            "data_backend": DATA_BACKEND,
            "integration_engine": INTEGRATION_ENGINE,
        },
        "version": "1.0.0",
    }


@router.get("/health/deep", tags=["System"])
async def health_check_deep():
    """
    Full health check including external APIs.
    Use this manually for diagnostics – NOT for automated polling.
    """
    data_ok = _data_backend_health()
    integration_ok = await _integration_engine_health()

    return {
        "status": "ok" if data_ok and integration_ok else "degraded",
        "components": {
            "checkin": True,
            "dashboard": True,
            "data_backend": DATA_BACKEND,
            "data": data_ok,
            "integration_engine": INTEGRATION_ENGINE,
            "integration": integration_ok,
        },
        "version": "1.0.0",
    }

"""
Storage facade - routes all data calls to the configured backend.

Switch backend via the DATA_BACKEND env var:
    DATA_BACKEND=postgres   (default)
    DATA_BACKEND=airtable   (legacy fallback)

Usage:
    from shared.storage import get_checkins, get_active_settings, ...

All public functions from airtable_api / postgres_api are re-exported.
"""

import os

DATA_BACKEND = os.getenv("DATA_BACKEND", "postgres").lower().strip()

if DATA_BACKEND == "postgres":
    from shared.postgres_api import *  # noqa: F401,F403
else:
    from shared.airtable_api import *  # noqa: F401,F403

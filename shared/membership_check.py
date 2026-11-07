"""Private, attempt-scoped eBas checks for check-in orchestration."""

import asyncio
from dataclasses import replace
from datetime import date

from starlette.concurrency import run_in_threadpool

from shared import storage
from shared.ebas_client import create_ebas_client

_checks_in_flight = set()


def _confirm(pnr: str, timeout_seconds: float) -> str:
    try:
        with create_ebas_client() as client:
            client.config = replace(
                client.config,
                confirm_timeout_seconds=min(
                    client.config.confirm_timeout_seconds, timeout_seconds
                ),
            )
            try:
                result = client.confirm_membership(pnr, date.today().year)
            finally:
                pnr = None
            if result.error_code is not None or not isinstance(result.found, bool):
                return "unavailable"
            return "verified_true" if result.found else "verified_false"
    except Exception:
        # Provider exceptions may contain the complete request; only state leaves this boundary.
        return "unavailable"
    finally:
        pnr = None


async def _check_and_apply(
    checkin_id: str, attempt_id: str, pnr: str, timeout_seconds: float
) -> str:
    try:
        state = await run_in_threadpool(_confirm, pnr, timeout_seconds)
    finally:
        pnr = None
    ok = state in ("verified_true", "verified_false")
    try:
        result = await run_in_threadpool(
            storage.apply_integration_result,
            checkin_id=checkin_id,
            source="ebas",
            ok=ok,
            data={"member": state == "verified_true"} if ok else {},
            error=None if ok else {"code": "ebas_unavailable"},
            state=state,
            attempt_id=attempt_id,
        )
    except Exception:
        return "unavailable"
    return "unavailable" if result.get("state") == "stale_rejected" else state


def _finished(task):
    _checks_in_flight.discard(task)
    if not task.cancelled():
        task.exception()  # Consume failures without logging exception text or retaining the task.


def start_membership_check(
    checkin_id: str, attempt_id: str, pnr: str, timeout_seconds: float
):
    """Keep a bounded-client check alive after the HTTP response deadline, like an n8n callback."""
    task = asyncio.create_task(
        _check_and_apply(checkin_id, attempt_id, pnr, timeout_seconds)
    )
    _checks_in_flight.add(task)
    task.add_done_callback(_finished)
    return task

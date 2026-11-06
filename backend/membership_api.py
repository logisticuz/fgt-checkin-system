"""Membership v2 routes; sensitive values stay in memory or the encrypted attempt store."""

import asyncio
import logging
import math
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from shared import (
    membership_crypto as crypto,
    membership_flow,
    membership_store as store,
    postgres_api,
    verification_service as verification,
)
from shared.ebas_client import EbasClient, MemberInput

BYLAWS_VERSION = "2026-11-06"
PRIVACY_VERSION = "2026-11-06-ebas+app-v1"
SUBMIT_WAIT_SECONDS = 20.0
CLEANUP_INTERVAL_SECONDS = 60.0
RECONCILE_INTERVAL_SECONDS = 30
BACKGROUND_RECONCILE_TICK_SECONDS = 30.0
RATE_LIMIT_SECONDS = 900
logger = logging.getLogger(__name__)
_submission_tasks = set()
_maintenance_tasks = set()
_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}")
_STATES = {
    "code_sent",
    "submitting",
    "submitted",
    "failed",
    "outcome_unknown",
    "locked",
    "expired",
    "manual_review",
}
_FIELD_NAMES = {
    "personnummer",
    "email",
    "phone",
    "channel",
    "checkin_id",
    "firstname",
    "lastname",
    "socialsecuritynumber",
    "phone1",
    "phone2",
    "co",
    "street",
    "zip_code_id",
    "city",
    "country_id",
    "renewed",
    "member_nick",
    "discord_user_id",
}
_MESSAGES = {
    "not_found": "Membership attempt not found.",
    "invalid_request": "Please check the request and try again.",
    "consent_required": "Please accept both the bylaws and privacy information.",
    "stale_texts": "The membership information has changed. Please reload and accept it again.",
    "year_changed": "Please start again",
    "invalid_details": "Please check your membership details.",
    "rate_limited": "Please wait before requesting another verification code.",
    "ebas_unavailable": "Membership verification is temporarily unavailable. Please ask a TO.",
    "send_failed": "The verification code could not be sent. Please ask a TO.",
    "wrong_code": "The verification code is incorrect. Please try again.",
    "locked": "Too many incorrect codes. Please ask a TO.",
    "expired": "This verification attempt has expired. Please start again or ask a TO.",
    "failed": "Membership registration could not be confirmed. Please ask a TO.",
    "internal_error": "Membership registration is temporarily unavailable. Please ask a TO.",
}
_STATE_MESSAGES = {
    "code_sent": "A verification code has been sent.",
    "submitting": "Your membership is being registered. Please wait.",
    "submitted": "Your membership has been registered.",
    "outcome_unknown": "We are checking whether your membership was registered. Please wait.",
    "failed": _MESSAGES["failed"],
    "locked": _MESSAGES["locked"],
    "expired": _MESSAGES["expired"],
    "manual_review": "Ask a TO to check your membership",
}


def _field(record, name, default=None):
    return record.get(name, default) if isinstance(record, dict) else getattr(record, name, default)


def _today():
    return date.today()


def _context(record, name, default=None):
    value = _field(record, name)
    if value is None:
        value = _field(_field(record, "context", {}), name, default)
    return default if value is None else value


def _reply(status=200, *, ok=False, state=None, error_code=None, **extra):
    payload = {"ok": ok}
    if state in _STATES:
        payload["state"] = state
    if error_code is not None:
        error_code = error_code if error_code in _MESSAGES else "internal_error"
        payload["error_code"] = error_code
    payload["user_message"] = _MESSAGES.get(
        error_code, _STATE_MESSAGES.get(state, _MESSAGES["internal_error"])
    )
    for name in (
        "attempt_id",
        "masked_destination",
        "tries_left",
        "resend_after_s",
        "retry_after_s",
    ):
        if name in extra:
            payload[name] = extra[name]
    if "field_errors" in extra:
        payload["field_errors"] = sorted(
            {
                name
                for name in extra["field_errors"]
                if isinstance(name, str) and name in _FIELD_NAMES
            }
        )
    headers = {"Retry-After": str(extra["retry_after_s"])} if status == 429 else None
    return JSONResponse(payload, status_code=status, headers=headers)


def _log(attempt_id, state, error_code=None):
    logger.info(
        "membership attempt_id=%s state=%s error_code=%s",
        attempt_id or "-",
        state,
        error_code or "-",
    )


def _unavailable(attempt_id=None):
    _log(attempt_id, "failed", "internal_error")
    return _reply(503, error_code="internal_error")


def _enabled():
    return membership_flow.get_membership_flow() == "v2"


def _attempt_id(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def _normalize_phone(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+0-9\s()-]{1,50}", value):
        return None
    phone = re.sub(r"[\s()-]", "", value)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("0"):
        phone = "+46" + phone[1:]
    if phone.startswith("+460"):
        phone = "+46" + phone[4:]
    return phone if _PHONE.fullmatch(phone) else None


def registration_context(request):
    context = {"membership_year": _today().year}
    checkin_id = _attempt_id(request.query_params.get("checkin_id"))
    if checkin_id is not None:
        context["checkin_id"] = checkin_id
    return context


def _masked_destination(channel, destination):
    if channel == "sms":
        prefix = "+46" if destination.startswith("+46") else "+"
        return prefix + "*" * (len(destination) - len(prefix) - 2) + destination[-2:]
    local, domain = destination.split("@")
    return re.sub(r"[0-9]{10,}", "***", local[0] + "***@" + domain)


@dataclass(frozen=True, repr=False)
class _StartInput:
    member: MemberInput
    channel: str
    checkin_id: str | None


def _validate_start(payload):
    if payload.get("accepted_bylaws") is not True or payload.get("accepted_privacy") is not True:
        return _reply(400, error_code="consent_required")
    if (
        payload.get("bylaws_version") != BYLAWS_VERSION
        or payload.get("privacy_version") != PRIVACY_VERSION
    ):
        return _reply(409, error_code="stale_texts")
    errors = []
    pnr = payload.get("personnummer")
    pnr = pnr.replace("-", "").replace(" ", "") if isinstance(pnr, str) else ""
    email = payload.get("email")
    email = email.strip() if isinstance(email, str) else ""
    phone = _normalize_phone(payload.get("phone"))
    if not email or len(email) > 254 or _EMAIL.fullmatch(email) is None:
        errors.append("email")
    if phone is None:
        errors.append("phone")
    try:
        member = MemberInput(pnr, email, phone or "")
    except ValueError:
        errors.append("personnummer")
        member = None
    channel = payload.get("channel")
    if channel not in ("sms", "email"):
        errors.append("channel")
    checkin_id = payload.get("checkin_id")
    if checkin_id is not None and (
        not isinstance(checkin_id, str) or not checkin_id or len(checkin_id) > 128
    ):
        errors.append("checkin_id")
    if errors:
        return _reply(422, error_code="invalid_details", field_errors=errors)
    return _StartInput(member, channel, checkin_id)


async def _payload(request):
    try:
        value = await request.json()
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _get_ebas_client():
    return EbasClient()


def _destination(attempt):
    return (
        _context(attempt, "phone")
        if _field(attempt, "channel") == "sms"
        else _context(attempt, "email")
    )


def _sent(attempt_id, channel, destination):
    return _reply(
        ok=True,
        state="code_sent",
        attempt_id=attempt_id,
        masked_destination=_masked_destination(channel, destination),
        resend_after_s=60,
    )


def _send(attempt_id, channel, destination, code=None):
    try:
        verification.start_verification(attempt_id, channel, destination, code=code)
    except verification.VerificationServiceError as error:
        if error.error_code == "rate_limited":
            return _reply(429, error_code="rate_limited", retry_after_s=error.retry_after_s or 60)
        if error.error_code == "not_found":
            return _reply(404, error_code="not_found")
        if error.error_code in ("expired", "locked", "manual_review"):
            return _reply(
                state=error.error_code,
                error_code=None if error.error_code == "manual_review" else error.error_code,
            )
        if error.error_code == "send_failed":
            _log(attempt_id, "failed", "send_failed")
            return _reply(502, state="failed", error_code="send_failed")
        return _unavailable(attempt_id)
    _log(attempt_id, "code_sent")
    return _sent(attempt_id, channel, destination)


def _start(details):
    try:
        settings = postgres_api.get_active_settings() or {}
        event_slug = settings.get("active_event_slug")
        if not event_slug:
            return _unavailable()
        if details.checkin_id is not None:
            checkin = postgres_api.get_checkin_by_record_id(details.checkin_id)
            if not checkin or _field(checkin, "fields", {}).get("event_slug") != event_slug:
                return _reply(404, error_code="not_found")
        destination = details.member.phone1 if details.channel == "sms" else details.member.email
        fingerprint = store.contact_fingerprint(details.channel, destination)
        if not store.rate_limit_ok(fingerprint):
            return _reply(429, error_code="rate_limited", retry_after_s=RATE_LIMIT_SECONDS)
        try:
            with _get_ebas_client() as client:
                validated = client.validate_member(details.member)
        except Exception:
            return _reply(503, error_code="ebas_unavailable")
        if _field(validated, "error_code") == "provider_unavailable":
            return _reply(503, error_code="ebas_unavailable")
        if _field(validated, "ok") is not True:
            return _reply(
                422,
                error_code="invalid_details",
                field_errors=_field(validated, "field_errors", []),
            )
        code = crypto.generate_code()
        attempt_id = store.create_attempt(
            pnr=details.member.pnr,
            contact=destination,
            channel=details.channel,
            event_slug=event_slug,
            code=code,
            bylaws_version=BYLAWS_VERSION,
            privacy_version=PRIVACY_VERSION,
            checkin_id=details.checkin_id,
            year=_today().year,
            email=details.member.email,
            phone=details.member.phone1,
            lang="en",
        )
        return _send(attempt_id, details.channel, destination, code)
    except store.RateLimitExceeded:
        return _reply(429, error_code="rate_limited", retry_after_s=RATE_LIMIT_SECONDS)
    except Exception:
        return _unavailable()


def _update_member(attempt):
    checkin_id = _field(attempt, "checkin_id")
    if checkin_id is not None:
        checkin = postgres_api.get_checkin_by_record_id(checkin_id)
        if checkin and _field(checkin, "fields", {}).get("event_slug") == _field(
            attempt, "event_slug"
        ):
            postgres_api.update_checkin(checkin_id, {"member": True})


def _year_changed(attempt_id, *, submitting=False):
    try:
        if submitting:
            store.finish_submit(attempt_id, "failed", "year_changed")
        else:
            store.fail_attempt(attempt_id, "year_changed")
    except Exception:
        _log(attempt_id, "failed", "internal_error")
    return _reply(409, error_code="year_changed")


def _submit(attempt_id, claim, attempt):
    try:
        latest = store.get_attempt(attempt_id, decrypt=True)
        if latest is None:
            return _reply(404, error_code="not_found")
        if latest.state not in ("submitting", "outcome_unknown"):
            return _state_response(latest)
        if latest.pnr is None or latest.context is None:
            store.cleanup_expired()
            return _state_response(store.get_attempt(attempt_id))
        renewed = _today()
        if renewed.year != latest.membership_year:
            return _year_changed(attempt_id, submitting=True)
        member = MemberInput(claim.pnr, claim.context["email"], claim.context["phone"])
        with _get_ebas_client() as client:
            result = client.submit_member_with_lookup(member, renewed)
        outcome = _field(result, "state")
    except Exception:
        outcome = "unavailable"
    try:
        if outcome == "submitted":
            store.finish_submit(attempt_id, "submitted")
            _update_member(attempt)
            _log(attempt_id, "submitted")
            return _reply(ok=True, state="submitted")
        if outcome == "rejected":
            store.finish_submit(attempt_id, "failed", "invalid_details")
            _log(attempt_id, "failed", "invalid_details")
            return _reply(state="failed", error_code="invalid_details")
        store.finish_submit(attempt_id, "outcome_unknown")
        _log(attempt_id, "outcome_unknown")
        return _reply(state="outcome_unknown")
    except Exception:
        return _unavailable(attempt_id)


def _state_response(attempt):
    state = _field(attempt, "state")
    if state == "manual_review":
        return _reply(state="manual_review")
    error = _field(attempt, "outcome_error_code")
    if state in ("locked", "expired", "failed") and error is None:
        error = state
    return _reply(ok=state == "submitted", state=state, error_code=error)


def _verify_claim(attempt_id, code):
    attempt = store.get_attempt(attempt_id)
    if attempt is None:
        return _reply(404, error_code="not_found")
    if _today().year != attempt.membership_year:
        return _year_changed(attempt_id)
    checked = verification.check_verification(attempt_id, attempt_id, code)
    result = checked.submission_claim
    if result is None:
        return _unavailable(attempt_id)
    status = _field(result, "status")
    if status == "submitting" and _field(result, "pnr") is not None:
        return result, attempt
    if status == "not_found":
        return _reply(404, error_code="not_found")
    if status == "already_submitting":
        return _reply(state="submitting")
    if status == "already_submitted":
        _update_member(attempt)
        return _reply(ok=True, state="submitted")
    if status == "invalid_code":
        latest = store.get_attempt(attempt_id)
        tries = max(0, 5 - _field(latest, "verify_attempts", 5))
        return _reply(state="code_sent", error_code="wrong_code", tries_left=tries)
    if status == "locked":
        return _reply(state="locked", error_code="locked", tries_left=0)
    return _reply(
        state=status if status in _STATES else "failed",
        error_code=None if status == "manual_review" else status,
    )


def _resend(attempt_id):
    try:
        attempt = store.get_attempt(attempt_id, decrypt=True)
        if attempt is None:
            return _reply(404, error_code="not_found")
        if _field(attempt, "state") != "code_sent":
            return _state_response(attempt)
        destination = _destination(attempt)
        if not isinstance(destination, str):
            store.fail_attempt(attempt_id, "internal_error")
            return _unavailable(attempt_id)
        return _send(attempt_id, _field(attempt, "channel"), destination)
    except Exception:
        return _unavailable(attempt_id)


def _status(attempt_id):
    try:
        attempt = store.get_attempt(attempt_id)
        if attempt is None:
            return _reply(404, error_code="not_found")
        state = _field(attempt, "state")
        if state == "submitted":
            _update_member(attempt)
        elif state == "outcome_unknown":
            claimed = store.claim_reconcile(attempt_id, min_interval_s=RECONCILE_INTERVAL_SECONDS)
            if claimed:
                pnr = _field(claimed, "pnr")
                if pnr:
                    with _get_ebas_client() as client:
                        result = client.confirm_membership(pnr, attempt.membership_year)
                    if _field(result, "found") is True:
                        store.finish_submit(attempt_id, "submitted")
                        _update_member(attempt)
            attempt = store.get_attempt(attempt_id)
        return _state_response(attempt)
    except Exception:
        return _unavailable(attempt_id)


async def _background_call(function, *args):
    task = asyncio.create_task(run_in_threadpool(function, *args))
    _maintenance_tasks.add(task)
    task.add_done_callback(_maintenance_tasks.discard)
    return await asyncio.shield(task)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await _background_call(store.cleanup_expired)
        except Exception:
            _log(None, "failed", "internal_error")


def _pending_reconciles():
    with postgres_api._get_pool().connection() as connection:
        rows = connection.execute(
            "SELECT attempt.id FROM membership_attempts attempt "
            "LEFT JOIN active_event_data checkin ON checkin.record_id = attempt.checkin_id "
            "AND checkin.event_slug = attempt.event_slug "
            "WHERE attempt.state = 'outcome_unknown' OR "
            "(attempt.state = 'submitted' AND checkin.record_id IS NOT NULL "
            "AND checkin.member IS NOT TRUE) "
            "ORDER BY attempt.last_reconciled_at NULLS FIRST, attempt.id LIMIT 10"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _recovery_timeout():
    try:
        seconds = float(os.getenv("EBAS_SUBMIT_TIMEOUT_SECONDS", "200"))
    except (TypeError, ValueError):
        raise ValueError("Invalid membership submit timeout") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Invalid membership submit timeout")
    return seconds + 60


async def _reconcile_loop():
    semaphore = asyncio.Semaphore(4)

    async def reconcile(attempt_id):
        async with semaphore:
            await _background_call(_status, attempt_id)

    while True:
        await asyncio.sleep(BACKGROUND_RECONCILE_TICK_SECONDS)
        try:
            await _background_call(store.recover_abandoned_submitting, _recovery_timeout())
            pending = await _background_call(_pending_reconciles)
            await asyncio.gather(*(reconcile(attempt_id) for attempt_id in pending))
        except Exception:
            _log(None, "outcome_unknown", "internal_error")


@asynccontextmanager
async def _lifespan(app):
    tasks = [asyncio.create_task(_cleanup_loop()), asyncio.create_task(_reconcile_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        running_loop = asyncio.get_running_loop()
        pending = [
            task
            for task in _submission_tasks | _maintenance_tasks
            if task.get_loop() is running_loop and not task.done()
        ]
        if pending:
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending), return_exceptions=True
            )


router = APIRouter(prefix="/api/membership", tags=["Membership"], lifespan=_lifespan)


def _spawn_submission(attempt_id, claim, attempt):
    work = run_in_threadpool(_submit, attempt_id, claim, attempt)
    try:
        task = asyncio.create_task(work)
    except BaseException:
        work.close()
        raise
    _submission_tasks.add(task)
    task.add_done_callback(_submission_tasks.discard)
    return task


@router.post("/start")
async def start(request: Request):
    if not _enabled():
        return _reply(404, error_code="not_found")
    payload = await _payload(request)
    if payload is None:
        return _reply(400, error_code="invalid_request")
    details = _validate_start(payload)
    return (
        details if isinstance(details, JSONResponse) else await run_in_threadpool(_start, details)
    )


@router.post("/verify")
async def verify(request: Request):
    if not _enabled():
        return _reply(404, error_code="not_found")
    payload = await _payload(request)
    if payload is None or "attempt_id" not in payload or not isinstance(payload.get("code"), str):
        return _reply(400, error_code="invalid_request")
    attempt_id = _attempt_id(payload["attempt_id"])
    if attempt_id is None:
        return _reply(404, error_code="not_found")
    try:
        claimed = await run_in_threadpool(_verify_claim, attempt_id, payload["code"])
    except Exception:
        return _unavailable(attempt_id)
    if isinstance(claimed, JSONResponse):
        return claimed
    result, attempt = claimed
    try:
        task = _spawn_submission(attempt_id, result, attempt)
    except Exception:
        try:
            await run_in_threadpool(store.finish_submit, attempt_id, "outcome_unknown")
        except Exception:
            return _unavailable(attempt_id)
        _log(attempt_id, "outcome_unknown", "internal_error")
        return _reply(state="outcome_unknown")
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=SUBMIT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _reply(202, ok=True, state="submitting")


@router.post("/resend")
async def resend(request: Request):
    if not _enabled():
        return _reply(404, error_code="not_found")
    payload = await _payload(request)
    if payload is None or "attempt_id" not in payload:
        return _reply(400, error_code="invalid_request")
    attempt_id = _attempt_id(payload["attempt_id"])
    return (
        _reply(404, error_code="not_found")
        if attempt_id is None
        else await run_in_threadpool(_resend, attempt_id)
    )


@router.get("/{attempt_id}/status")
async def status(attempt_id: str):
    if not _enabled():
        return _reply(404, error_code="not_found")
    attempt_id = _attempt_id(attempt_id)
    return (
        _reply(404, error_code="not_found")
        if attempt_id is None
        else await run_in_threadpool(_status, attempt_id)
    )

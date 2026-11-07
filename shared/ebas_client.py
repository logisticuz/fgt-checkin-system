"""Typed eBas calls that expose only explicitly approved response fields and values."""

import logging
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import httpx

try:
    from backend.validation import validate_personnummer
except ModuleNotFoundError as error:
    if error.name not in ("backend", "backend.validation"):
        raise
    from validation import validate_personnummer

logger = logging.getLogger(__name__)
_PRIVATE_TRANSPORT_LOGS = ContextVar("ebas_private_transport_logs", default=False)
_DIGITS = re.compile(r"\d{10,}")
_PNR = re.compile(r"[0-9]{12}")
_DUPLICATE_MESSAGE = (
    "Identical request is already being processed. "
    "Wait for the response before making a new request."
)
_MEMBER_FIELDS = frozenset(
    {
        "firstname",
        "lastname",
        "socialsecuritynumber",
        "email",
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
)
# Only the classification documented in the supplied API reference is approved.
_REGISTRATION_SOURCES = frozenset({"Inskriven manuellt"})
SubmitState = Literal["submitted", "rejected", "in_progress", "outcome_unknown", "unavailable"]


def _safe_log(message: str, api_key: str) -> None:
    if api_key:
        message = message.replace(api_key, "[redacted]")
    logger.info("%s", _DIGITS.sub("[redacted]", message))


class _TransportPrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        transport = record.name in ("httpx", "httpcore") or record.name.startswith(
            ("httpx.", "httpcore.")
        )
        return not (transport and _PRIVATE_TRANSPORT_LOGS.get())


@contextmanager
def _private_transport_logging():
    """Suppress provider-controlled diagnostics only in the current eBas call context.

    Handler filters also cover descendant loggers first created during a request:
    parent logger filters alone are not applied to propagated child records.
    """
    transport_loggers = {logging.getLogger("httpx"), logging.getLogger("httpcore")}
    transport_loggers.update(
        item
        for item in list(logging.Logger.manager.loggerDict.values())
        if isinstance(item, logging.Logger) and item.name.startswith(("httpx.", "httpcore."))
    )
    handlers = set()
    for transport_logger in transport_loggers:
        ancestor = transport_logger
        while ancestor is not None:
            handlers.update(ancestor.handlers)
            ancestor = ancestor.parent
    if logging.lastResort is not None:
        handlers.add(logging.lastResort)
    privacy_filter = _TransportPrivacyFilter()
    token = _PRIVATE_TRANSPORT_LOGS.set(True)
    try:
        for transport_logger in transport_loggers:
            transport_logger.addFilter(privacy_filter)
        for handler in handlers:
            handler.addFilter(privacy_filter)
        yield
    finally:
        for transport_logger in transport_loggers:
            transport_logger.removeFilter(privacy_filter)
        for handler in handlers:
            handler.removeFilter(privacy_filter)
        _PRIVATE_TRANSPORT_LOGS.reset(token)


def _validate_pnr(pnr: str) -> None:
    if not isinstance(pnr, str) or _PNR.fullmatch(pnr) is None:
        raise ValueError("Personal ID must contain exactly twelve digits")
    if not validate_personnummer(pnr)[0]:
        raise ValueError("Personal ID failed validation")


@dataclass(frozen=True, repr=False)
class EbasConfig:
    api_key: str
    association_number: str = "F161215-9"
    base_url: str = "https://ebas.sverok.se/apis"
    validate_timeout_seconds: float = 15.0
    submit_timeout_seconds: float = 200.0
    confirm_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("EBAS_API_KEY is required")
        if not isinstance(self.association_number, str) or not self.association_number.strip():
            raise ValueError("EBAS_ASSOCIATION_NUMBER is required")
        try:
            url = httpx.URL(self.base_url)
            valid_url = (
                url.scheme in ("https", "http")
                and bool(url.host)
                and not (url.username or url.password or url.query or url.fragment)
            )
        except Exception:
            raise ValueError("Invalid eBas base URL") from None
        if not valid_url:
            raise ValueError("Invalid eBas base URL")
        for timeout in (
            self.validate_timeout_seconds,
            self.submit_timeout_seconds,
            self.confirm_timeout_seconds,
        ):
            if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("eBas timeouts must be finite positive numbers")

    def __repr__(self) -> str:
        return (
            f"EbasConfig(api_key='[redacted]', association_number={self.association_number!r}, "
            f"base_url={self.base_url!r}, validate_timeout_seconds={self.validate_timeout_seconds!r}, "
            f"submit_timeout_seconds={self.submit_timeout_seconds!r}, "
            f"confirm_timeout_seconds={self.confirm_timeout_seconds!r})"
        )

    @classmethod
    def from_env(cls) -> "EbasConfig":
        try:
            timeout = float(os.getenv("EBAS_SUBMIT_TIMEOUT_SECONDS", "200"))
        except (TypeError, ValueError):
            raise ValueError("EBAS_SUBMIT_TIMEOUT_SECONDS must be a positive number") from None
        return cls(
            api_key=os.getenv("EBAS_API_KEY", ""),
            association_number=os.getenv("EBAS_ASSOCIATION_NUMBER", "F161215-9"),
            base_url=os.getenv("EBAS_BASE_URL", "https://ebas.sverok.se/apis"),
            submit_timeout_seconds=timeout,
        )


@dataclass(frozen=True, repr=False)
class MemberInput:
    pnr: str
    email: str
    phone1: str
    firstname: str | None = None
    lastname: str | None = None

    def __post_init__(self) -> None:
        _validate_pnr(self.pnr)
        if not all(isinstance(value, str) for value in (self.email, self.phone1)):
            raise ValueError("Member contact fields must be strings")
        if not all(
            value is None or isinstance(value, str) for value in (self.firstname, self.lastname)
        ):
            raise ValueError("Member name fields must be strings when supplied")

    def __repr__(self) -> str:
        return f"MemberInput(pnr={self.pnr[:8] + '****'!r})"

    def _fields(self) -> dict:
        return {
            "firstname": self.firstname or "",
            "lastname": self.lastname or "",
            "socialsecuritynumber": self.pnr,
            "email": self.email,
            "phone1": self.phone1,
            "street": "",
            "zip_code_id": "",
            "city": "",
        }


@dataclass(frozen=True)
class ValidateResult:
    ok: bool
    field_errors: list[str] = field(default_factory=list)
    has_warnings: bool = False
    spar_found: bool = False
    error_code: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    state: SubmitState
    field_errors: list[str] = field(default_factory=list)
    has_warnings: bool = False
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "submitted"


@dataclass(frozen=True)
class ConfirmResult:
    found: bool | None
    registration_source: str | None = None
    error_code: str | None = None


def _has_errors(errors) -> bool:
    if isinstance(errors, dict):
        return any(bool(value) for value in errors.values())
    if isinstance(errors, list):
        return any(_has_errors(value) for value in errors)
    return bool(errors)


def _field_errors(errors) -> list[str]:
    groups = errors if isinstance(errors, list) else [errors]
    return sorted(
        {
            name
            for group in groups
            if isinstance(group, dict)
            for name, messages in group.items()
            if name in _MEMBER_FIELDS and messages
        }
    )


def _contains_duplicate(value) -> bool:
    if isinstance(value, str):
        return _DUPLICATE_MESSAGE.casefold() in value.casefold()
    if isinstance(value, dict):
        return any(_contains_duplicate(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_duplicate(item) for item in value)
    return False


def _in_progress(response) -> bool:
    if isinstance(response, str):
        return _contains_duplicate(response)
    if not isinstance(response, dict):
        return False
    return any(
        _contains_duplicate(response.get(name))
        for name in ("error", "errors", "message", "error_message", "member_errors")
    )


class EbasClient:
    def __init__(
        self,
        config: EbasConfig | None = None,
        client: httpx.Client | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config if config is not None else EbasConfig.from_env()
        self._owns_client = client is None
        if client is not None:
            self._client = client
        elif transport is not None:
            self._client = httpx.Client(transport=transport, trust_env=False)
        else:
            self._client = httpx.Client(trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "EbasClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _post(self, action: str, payload: dict, timeout: float) -> tuple[object, str | None]:
        try:
            with _private_transport_logging():
                response = self._client.post(
                    f"{self.config.base_url.rstrip('/')}/{action}.json",
                    json=payload,
                    timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
                    follow_redirects=False,
                )
        except httpx.ReadTimeout:
            return None, "read_timeout"
        except Exception:
            # Provider exception text can echo the complete request. Never expose it.
            return None, "provider_unavailable"
        if not (200 <= response.status_code < 300 or 400 <= response.status_code < 500):
            return None, "provider_unavailable"
        try:
            return response.json(), None
        except Exception:
            if (
                action == "submit_member_with_lookup"
                and response.text.strip() == _DUPLICATE_MESSAGE
            ):
                return {"error": _DUPLICATE_MESSAGE}, None
            return None, "provider_unavailable"

    def validate_member(self, member: MemberInput) -> ValidateResult:
        fields = {**member._fields(), "phone2": "", "co": ""}
        payload = {
            "request": {
                "action": "validate_members",
                "version": "1",
                "association_number": self.config.association_number,
                "api_key": self.config.api_key,
                "data": {"Member": [fields]},
            }
        }
        response, failure = self._post(
            "validate_members", payload, self.config.validate_timeout_seconds
        )
        body = response.get("response") if isinstance(response, dict) else None
        if (
            failure
            or not isinstance(body, dict)
            or body.get("request_result") not in ("success", "fail")
        ):
            _safe_log("eBas validate unavailable", self.config.api_key)
            return ValidateResult(ok=False, error_code="provider_unavailable")
        ok = body["request_result"] == "success" and not _has_errors(body.get("member_errors"))
        result = ValidateResult(
            ok=ok,
            field_errors=_field_errors(body.get("member_errors")),
            has_warnings=bool(body.get("member_warnings")),
            spar_found=bool(body.get("spar_names")),
            error_code=None if ok else "validation_failed",
        )
        _safe_log("eBas validate completed", self.config.api_key)
        return result

    def submit_member_with_lookup(self, member: MemberInput, renewed: date) -> SubmitResult:
        if not isinstance(renewed, date) or isinstance(renewed, datetime):
            raise ValueError("renewed must be a date")
        fields = {
            **member._fields(),
            "renewed": renewed.isoformat(),
            "co": [],
            "country_id": "1",
            "member_nick": None,
            "discord_user_id": None,
        }
        response, failure = self._post(
            "submit_member_with_lookup",
            {"api_key": self.config.api_key, "member": fields},
            self.config.submit_timeout_seconds,
        )
        errors = response.get("member_errors") if isinstance(response, dict) else None
        warnings = bool(response.get("member_warnings")) if isinstance(response, dict) else False
        if failure == "read_timeout":
            result = SubmitResult("outcome_unknown", error_code="outcome_unknown")
        elif failure:
            result = SubmitResult("unavailable", error_code="provider_unavailable")
        elif isinstance(response, dict) and response.get("stored_member") is True:
            result = SubmitResult("submitted", has_warnings=warnings)
        elif _in_progress(response):
            result = SubmitResult("in_progress", error_code="in_progress")
        elif (
            isinstance(response, dict)
            and response.get("request_result") == "fail"
            and _has_errors(errors)
        ):
            result = SubmitResult("rejected", _field_errors(errors), warnings, "member_rejected")
        else:
            result = SubmitResult("unavailable", error_code="provider_unavailable")
        _safe_log(f"eBas submit {result.state}", self.config.api_key)
        return result

    def confirm_membership(self, pnr: str, year: int) -> ConfirmResult:
        _validate_pnr(pnr)
        if type(year) is not int or not 1000 <= year <= 9999:
            raise ValueError("Membership year must be a four-digit integer")
        payload = {
            "request": {
                "action": "confirm_membership",
                "association_number": self.config.association_number,
                "api_key": self.config.api_key,
                "year_id": year,
                "firstname": "",
                "lastname": "",
                "socialsecuritynumber": pnr,
                "email": "",
                "phone1": "",
                "member_nick": None,
                "discord_user_id": None,
            }
        }
        response, failure = self._post(
            "confirm_membership", payload, self.config.confirm_timeout_seconds
        )
        body = response.get("response") if isinstance(response, dict) else None
        if failure or not isinstance(body, dict) or type(body.get("member_found")) is not bool:
            _safe_log("eBas confirm unavailable", self.config.api_key)
            return ConfirmResult(found=None, error_code="provider_unavailable")
        source = body.get("registration_source")
        approved_source = (
            source if isinstance(source, str) and source in _REGISTRATION_SOURCES else None
        )
        _safe_log("eBas confirm completed", self.config.api_key)
        return ConfirmResult(
            body["member_found"], approved_source if body["member_found"] else None
        )


class StubEbasTransport(httpx.BaseTransport):
    """Deterministic local eBas responses; never retain or forward request data."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "validate_members.json":
            data = {"response": {"request_result": "success", "member_errors": []}}
        elif action == "submit_member_with_lookup.json":
            data = {"stored_member": True, "request_result": "success"}
        elif action == "confirm_membership.json":
            data = {"response": {"member_found": True}}
        else:
            return httpx.Response(404, json={}, request=request)
        return httpx.Response(200, json=data, request=request)


def ebas_transport_mode() -> Literal["stub", "live"]:
    """Fail closed on invalid or contradictory transport configuration."""
    mode = os.getenv("EBAS_TRANSPORT", "stub").strip().lower() or "stub"
    if mode not in ("stub", "live"):
        raise ValueError("EBAS_TRANSPORT must be stub or live")
    engine = os.getenv("INTEGRATION_ENGINE", "n8n").strip().lower()
    if (
        mode == "live"
        and engine == "none"
        and os.getenv("EBAS_ALLOW_LIVE_WITH_ENGINE_NONE") != "1"
    ):
        raise ValueError(
            "EBAS_TRANSPORT=live is incompatible with INTEGRATION_ENGINE=none; "
            "deliberate live access requires EBAS_ALLOW_LIVE_WITH_ENGINE_NONE=1"
        )
    return mode


def initialize_ebas_transport() -> str:
    """Validate once at application startup and log only the selected mode."""
    mode = ebas_transport_mode()
    if mode == "live":
        EbasConfig.from_env()
    logger.info("eBas transport: %s", mode)
    return mode


def create_ebas_client() -> EbasClient:
    """The application factory: local stub by default; real transport needs opt-in."""
    if ebas_transport_mode() == "live":
        return EbasClient()
    return EbasClient(
        EbasConfig(api_key="stub", base_url="https://ebas.stub.invalid/apis"),
        transport=StubEbasTransport(),
    )

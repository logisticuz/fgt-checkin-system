"""Local-code verification sender contract and DEV-only console implementation."""

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Channel = Literal["sms", "email"]
Language = Literal["sv", "en"]

MESSAGE_TEMPLATES = {
    "sv": "Din FGC THN-kod: {code}. Giltig i 10 min.",
    "en": "Your FGC THN code: {code}. Valid for 10 min.",
}

logger = logging.getLogger(__name__)
_CODE_PATTERN = re.compile(r"[0-9]{6}")
_SMS_PATTERN = re.compile(r"\+[1-9][0-9]{7,14}")
_EMAIL_PATTERN = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error_code: str | None = None
    provider_ref: str | None = None


class VerificationSender(ABC):
    """Send a locally generated code; report provider failures as a SendResult.

    Provider error codes are invalid_recipient, provider_unavailable, rate_limited,
    and unsupported_channel. Invalid local codes return invalid_code.
    """

    @abstractmethod
    def send_code(self, channel: Channel, to: str, code: str, lang: Language) -> SendResult:
        """Send a six-digit code without including a name in the message."""


class ConsoleSender(VerificationSender):
    """Log codes for DEV while masking their recipients."""

    def __init__(self) -> None:
        production = any(
            os.getenv(name, "").strip().lower() in {"prod", "production"}
            for name in ("APP_ENV", "ENV")
        )
        if production and os.getenv("ALLOW_CONSOLE_SENDER") != "1":
            raise RuntimeError(
                "ConsoleSender is DEV-only; production requires ALLOW_CONSOLE_SENDER=1."
            )

    def send_code(self, channel: Channel, to: str, code: str, lang: Language) -> SendResult:
        if channel not in ("sms", "email"):
            return SendResult(ok=False, error_code="unsupported_channel")
        if not isinstance(code, str) or _CODE_PATTERN.fullmatch(code) is None:
            return SendResult(ok=False, error_code="invalid_code")
        pattern = _SMS_PATTERN if channel == "sms" else _EMAIL_PATTERN
        if not isinstance(to, str) or pattern.fullmatch(to) is None:
            return SendResult(ok=False, error_code="invalid_recipient")

        if channel == "sms":
            prefix = "+46" if to.startswith("+46") else "+"
            masked = prefix + "*" * (len(to) - len(prefix) - 2) + to[-2:]
        else:
            local, domain = to.split("@")
            masked = local[0] + "***@" + domain
        logger.info("[verification] channel=%s to=%s code=%s", channel, masked, code)
        return SendResult(ok=True)


class ElksSender(VerificationSender):
    def send_code(self, channel: Channel, to: str, code: str, lang: Language) -> SendResult:
        raise NotImplementedError("ElksSender is not implemented; use console mode in DEV.")


class SmtpSender(VerificationSender):
    def send_code(self, channel: Channel, to: str, code: str, lang: Language) -> SendResult:
        raise NotImplementedError("SmtpSender is not implemented; use console mode in DEV.")


class EbasSender(VerificationSender):
    def send_code(self, channel: Channel, to: str, code: str, lang: Language) -> SendResult:
        raise NotImplementedError(
            "EbasSender is not implemented; its start/verify contract awaits the API decision."
        )


def get_sender() -> VerificationSender:
    """Select the configured sender and fail loudly for unknown providers."""
    name = os.getenv("VERIFICATION_SENDER", "console")
    sender_types = {
        "console": ConsoleSender,
        "elks": ElksSender,
        "smtp": SmtpSender,
        "ebas": EbasSender,
    }
    if name not in sender_types:
        raise ValueError(f"Unknown VERIFICATION_SENDER: {name!r}")
    return sender_types[name]()

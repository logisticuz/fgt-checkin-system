"""Self-contained sender tests: no backend, database, credentials, or network."""

import logging
from string import Formatter

import pytest

from shared.verification_sender import (
    MESSAGE_TEMPLATES,
    ConsoleSender,
    EbasSender,
    ElksSender,
    SendResult,
    SmtpSender,
    VerificationSender,
    get_sender,
)


@pytest.fixture(autouse=True)
def clean_sender_environment(monkeypatch):
    for name in ("APP_ENV", "ENV", "ALLOW_CONSOLE_SENDER", "VERIFICATION_SENDER"):
        monkeypatch.delenv(name, raising=False)


def test_sender_is_abstract():
    with pytest.raises(TypeError):
        VerificationSender()


def test_factory_defaults_to_console():
    assert isinstance(get_sender(), ConsoleSender)


@pytest.mark.parametrize(
    ("name", "sender_type"),
    [("console", ConsoleSender), ("elks", ElksSender), ("smtp", SmtpSender), ("ebas", EbasSender)],
)
def test_factory_selects_sender(monkeypatch, name, sender_type):
    monkeypatch.setenv("VERIFICATION_SENDER", name)
    assert isinstance(get_sender(), sender_type)


@pytest.mark.parametrize("name", ["unknown", ""])
def test_factory_rejects_unknown_sender(monkeypatch, name):
    monkeypatch.setenv("VERIFICATION_SENDER", name)
    with pytest.raises(ValueError, match="Unknown VERIFICATION_SENDER"):
        get_sender()


@pytest.mark.parametrize(
    ("name", "value"),
    [("APP_ENV", "production"), ("ENV", "prod"), ("APP_ENV", " PROD "), ("ENV", "PRODUCTION")],
)
def test_console_refuses_production(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="DEV-only"):
        ConsoleSender()
    with pytest.raises(RuntimeError, match="DEV-only"):
        get_sender()


@pytest.mark.parametrize(("name", "value"), [("APP_ENV", "production"), ("ENV", "prod")])
def test_console_allows_explicit_production_override(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALLOW_CONSOLE_SENDER", "1")
    assert isinstance(get_sender(), ConsoleSender)


def test_console_requires_exact_override(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_CONSOLE_SENDER", "true")
    with pytest.raises(RuntimeError, match="ALLOW_CONSOLE_SENDER=1"):
        ConsoleSender()


@pytest.mark.parametrize(
    ("channel", "recipient", "masked"),
    [
        ("sms", "+4612345612", "+46******12"),
        ("sms", "+12025550123", "+*********23"),
        ("email", "synthetic@example.invalid", "s***@example.invalid"),
        ("email", "a@example.invalid", "a***@example.invalid"),
    ],
)
@pytest.mark.parametrize("lang", ["sv", "en"])
def test_console_masks_recipient_and_logs_code(caplog, channel, recipient, masked, lang):
    with caplog.at_level(logging.INFO, logger="shared.verification_sender"):
        result = ConsoleSender().send_code(channel, recipient, "012345", lang)

    assert result == SendResult(ok=True)
    assert caplog.messages == [f"[verification] channel={channel} to={masked} code=012345"]
    assert recipient not in caplog.text


@pytest.mark.parametrize(
    "code", [None, 123456, "12345", "1234567", "12 456", "abcdef", "123456\n", "１２３４５６"]
)
def test_invalid_codes_return_error_without_logging(caplog, code):
    with caplog.at_level(logging.INFO, logger="shared.verification_sender"):
        result = ConsoleSender().send_code("sms", "+4612345612", code, "sv")
    assert result == SendResult(ok=False, error_code="invalid_code")
    assert caplog.records == []


@pytest.mark.parametrize(
    ("channel", "recipient"),
    [
        ("sms", None),
        ("sms", 4612345612),
        ("sms", ""),
        ("sms", "0701234567"),
        ("sms", "+0123456789"),
        ("sms", "+4612"),
        ("sms", "+4612345678901234"),
        ("sms", "+4612345612\n"),
        ("sms", "+４６１２３４５６１２"),
        ("email", None),
        ("email", ""),
        ("email", "missing-at.example.invalid"),
        ("email", "synthetic@example"),
        ("email", "synthetic @example.invalid"),
        ("email", "synthetic@@example.invalid"),
        ("email", "synthetic@example.invalid\n"),
        ("email", "Name <synthetic@example.invalid>"),
    ],
)
def test_invalid_recipients_return_error_without_logging(caplog, channel, recipient):
    with caplog.at_level(logging.INFO, logger="shared.verification_sender"):
        result = ConsoleSender().send_code(channel, recipient, "123456", "en")
    assert result == SendResult(ok=False, error_code="invalid_recipient")
    assert caplog.records == []


@pytest.mark.parametrize("channel", ["push", "SMS", "", None])
def test_unsupported_channel_returns_error_without_logging(caplog, channel):
    with caplog.at_level(logging.INFO, logger="shared.verification_sender"):
        result = ConsoleSender().send_code(channel, "+4612345612", "123456", "en")
    assert result == SendResult(ok=False, error_code="unsupported_channel")
    assert caplog.records == []


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("sv", "Din FGC THN-kod: 012345. Giltig i 10 min."),
        ("en", "Your FGC THN code: 012345. Valid for 10 min."),
    ],
)
def test_templates_contain_only_code(lang, expected):
    template = MESSAGE_TEMPLATES[lang]
    fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    assert fields == {"code"}
    assert template.format(code="012345") == expected


@pytest.mark.parametrize("sender_type", [ElksSender, SmtpSender, EbasSender])
def test_provider_stubs_fail_explicitly(sender_type):
    with pytest.raises(NotImplementedError, match=f"{sender_type.__name__} is not implemented"):
        sender_type().send_code("sms", "+4612345612", "123456", "sv")

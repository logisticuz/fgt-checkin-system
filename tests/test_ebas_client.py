"""eBas reference responses through MockTransport; no provider network or credentials."""

import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime
from threading import Barrier
from unittest.mock import MagicMock

import httpx
import pytest

from shared.ebas_client import (
    ConfirmResult,
    EbasClient,
    EbasConfig,
    MemberInput,
    SubmitResult,
    ValidateResult,
    _safe_log,
    normalize_check_pnr,
)

PNR = "200001010008"
API_KEY = "synthetic-ebas-secret-key"
SPAR_NAME = "Exempelsson"
DUPLICATE = (
    "Identical request is already being processed. "
    "Wait for the response before making a new request."
)
MEMBER = MemberInput(PNR, "synthetic@example.invalid", "+12025550123")
RENEWED = date(2026, 11, 6)


@pytest.fixture
def make_client():
    transports = []

    def make(response=None, *, status=200, content=None, error=None, config=None):
        calls = []

        def handle(request):
            calls.append(request)
            if error is not None:
                message = f"Provider echoed {PNR}, {API_KEY}, {SPAR_NAME}"
                if issubclass(error, httpx.RequestError):
                    raise error(message, request=request)
                raise error(message)
            if content is not None:
                return httpx.Response(status, content=content)
            return httpx.Response(status, json=response)

        transport = httpx.Client(transport=httpx.MockTransport(handle))
        transports.append(transport)
        return EbasClient(config or EbasConfig(API_KEY), transport), calls

    yield make
    for transport in transports:
        transport.close()


def assert_private(result, caplog):
    outputs = (repr(result), json.dumps(asdict(result), ensure_ascii=False), caplog.text)
    for output in outputs:
        for secret in (PNR, PNR[-10:], API_KEY, SPAR_NAME):
            assert secret not in output
        assert "request_echo" not in output
        assert "spar_names" not in output


def test_config_defaults_and_masked_repr(monkeypatch):
    monkeypatch.setenv("EBAS_API_KEY", API_KEY)
    config = EbasConfig.from_env()
    assert config.base_url == "https://ebas.sverok.se/apis"
    assert config.association_number == "F161215-9"
    assert config.validate_timeout_seconds == 15
    assert config.submit_timeout_seconds == 200
    assert config.confirm_timeout_seconds == 15
    assert API_KEY not in repr(config)
    assert "[redacted]" in repr(config)


def test_config_reads_association_and_submit_timeout(monkeypatch):
    monkeypatch.setenv("EBAS_API_KEY", API_KEY)
    monkeypatch.setenv("EBAS_ASSOCIATION_NUMBER", "F000000-0")
    monkeypatch.setenv("EBAS_SUBMIT_TIMEOUT_SECONDS", "210")
    config = EbasConfig.from_env()
    assert config.api_key == API_KEY
    assert config.association_number == "F000000-0"
    assert config.submit_timeout_seconds == 210


def test_missing_key_fails_without_creating_a_client(monkeypatch):
    monkeypatch.delenv("EBAS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EBAS_API_KEY is required"):
        EbasClient()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_config_rejects_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="finite positive"):
        EbasConfig(API_KEY, submit_timeout_seconds=timeout)


def test_bad_timeout_environment_does_not_echo_sensitive_input(monkeypatch):
    monkeypatch.setenv("EBAS_API_KEY", API_KEY)
    monkeypatch.setenv("EBAS_SUBMIT_TIMEOUT_SECONDS", f"bad {PNR} {API_KEY}")
    with pytest.raises(ValueError) as captured:
        EbasConfig.from_env()
    formatted = "".join(traceback.format_exception(captured.value))
    assert PNR not in formatted
    assert API_KEY not in formatted


@pytest.mark.parametrize(
    "url",
    [
        "not a URL",
        "https://user:secret@example.invalid/apis",
        "https://example.invalid/apis?key=secret",
    ],
)
def test_base_url_cannot_include_credentials_or_query_data(url):
    with pytest.raises(ValueError, match="Invalid eBas base URL"):
        EbasConfig(API_KEY, base_url=url)


def test_member_repr_masks_pnr_and_contact_details():
    assert repr(MEMBER) == "MemberInput(pnr='20000101****')"
    assert PNR not in repr(MEMBER)
    assert MEMBER.email not in repr(MEMBER)
    assert MEMBER.phone1 not in repr(MEMBER)


@pytest.mark.parametrize(
    "pnr", [PNR[-10:], "20000101-0008", "200001010009", "２００００１０１０００８", None]
)
def test_member_rejects_invalid_ids_without_echoing_them(pnr):
    with pytest.raises(ValueError) as captured:
        MemberInput(pnr, "synthetic@example.invalid", "+12025550123")
    if pnr is not None:
        assert pnr not in str(captured.value)


def test_log_helper_redacts_both_id_lengths_and_api_key(caplog):
    with caplog.at_level(logging.INFO):
        _safe_log(f"Synthetic {PNR} / {PNR[-10:]} / {API_KEY}", API_KEY)
    assert caplog.messages == ["Synthetic [redacted] / [redacted] / [redacted]"]


def test_validate_reference_failure_is_strictly_whitelisted(make_client, caplog):
    response = {
        "response": {
            "spar_names": {"firstname": "Test", "lastname": SPAR_NAME},
            "member_errors": [
                {
                    "firstname": ["Får inte vara tom"],
                    "lastname": ["Får inte vara tom"],
                    "socialsecuritynumber": ["Måste vara i formen YYYYMMDDXXXX"],
                    "phone1": [
                        "Måste vara ett giltigt telefonnummer utan mellanslag eller bindestreck"
                    ],
                    "city": ["Får inte vara tom"],
                }
            ],
            "member_warnings": None,
            "request_result": "fail",
        },
        "request_echo": {"api_key": API_KEY, "data": {"socialsecuritynumber": PNR}},
    }
    client, calls = make_client(response)
    with caplog.at_level(logging.DEBUG):
        result = client.validate_member(MEMBER)
    assert result == ValidateResult(
        False,
        ["city", "firstname", "lastname", "phone1", "socialsecuritynumber"],
        spar_found=True,
        error_code="validation_failed",
    )
    assert len(calls) == 1
    assert_private(result, caplog)


def test_validate_success_warnings_do_not_block_saving(make_client, caplog):
    client, _ = make_client(
        {
            "response": {
                "spar_names": {"lastname": SPAR_NAME},
                "member_errors": [{}],
                "member_warnings": [f"Sensitive warning {PNR} {API_KEY}"],
                "request_result": "success",
            }
        }
    )
    result = client.validate_member(MEMBER)
    assert result == ValidateResult(True, has_warnings=True, spar_found=True)
    assert_private(result, caplog)


def test_validate_request_matches_reference_wrapper(make_client):
    client, calls = make_client({"response": {"request_result": "success"}})
    client.validate_member(MEMBER)
    request = calls[0]
    payload = json.loads(request.content)
    assert set(payload) == {"request"}
    wrapper = payload["request"]
    assert set(wrapper) == {"action", "version", "association_number", "api_key", "data"}
    assert wrapper["action"] == "validate_members"
    assert wrapper["version"] == "1"
    assert wrapper["association_number"] == "F161215-9"
    assert wrapper["api_key"] == API_KEY
    assert wrapper["data"]["Member"] == [
        {
            "firstname": "",
            "lastname": "",
            "socialsecuritynumber": PNR,
            "email": MEMBER.email,
            "phone1": MEMBER.phone1,
            "phone2": "",
            "co": "",
            "street": "",
            "zip_code_id": "",
            "city": "",
        }
    ]
    assert request.method == "POST"
    assert request.url.path == "/apis/validate_members.json"
    assert PNR not in str(request.url)
    assert API_KEY not in str(request.url)
    assert request.extensions["timeout"]["read"] == 15


def test_submit_reference_success_and_request_shape(make_client, caplog):
    client, calls = make_client(
        {
            "submit_request_echo": {"api_key": API_KEY, "member": {"socialsecuritynumber": PNR}},
            "stored_member": True,
            "request_result": "success",
            "member_warnings": [],
            "spar_names": {"lastname": SPAR_NAME},
        },
        config=EbasConfig(API_KEY, submit_timeout_seconds=210),
    )
    with caplog.at_level(logging.DEBUG):
        result = client.submit_member_with_lookup(MEMBER, RENEWED)
    assert result == SubmitResult("submitted")
    assert result.ok
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert set(payload) == {"api_key", "member"}
    assert payload["api_key"] == API_KEY
    assert payload["member"]["socialsecuritynumber"] == PNR
    assert payload["member"]["renewed"] == "2026-11-06"
    assert payload["member"]["co"] == []
    assert payload["member"]["country_id"] == "1"
    assert calls[0].url.path == "/apis/submit_member_with_lookup.json"
    assert calls[0].extensions["timeout"]["read"] == 210
    assert calls[0].extensions["timeout"]["connect"] == 15
    assert_private(result, caplog)


def test_submit_reference_failure_returns_only_known_field_names(make_client, caplog):
    client, _ = make_client(
        {
            "submit_request_echo": {"socialsecuritynumber": PNR, "api_key": API_KEY},
            "stored_member": False,
            "member_errors": {
                "firstname": ["Får inte vara tom."],
                "email": [f"Echoed {PNR} {API_KEY} {SPAR_NAME}"],
                PNR: ["untrusted field name"],
                SPAR_NAME: ["untrusted field name"],
            },
            "member_warnings": [f"Warning {PNR}"],
            "request_result": "fail",
        }
    )
    result = client.submit_member_with_lookup(MEMBER, RENEWED)
    assert result == SubmitResult("rejected", ["email", "firstname"], True, "member_rejected")
    assert not result.ok
    assert_private(result, caplog)


@pytest.mark.parametrize(
    "response",
    [
        {"error": DUPLICATE},
        {"message": DUPLICATE},
        {"request_result": "fail", "member_errors": {"socialsecuritynumber": [DUPLICATE]}},
        DUPLICATE,
    ],
)
def test_identical_request_has_a_distinct_state_without_retry(make_client, response, caplog):
    client, calls = make_client(response, status=409)
    result = client.submit_member_with_lookup(MEMBER, RENEWED)
    assert result.state == "in_progress"
    assert len(calls) == 1
    assert_private(result, caplog)


def test_plain_text_identical_request_is_recognised(make_client):
    client, calls = make_client(content=DUPLICATE.encode())
    assert client.submit_member_with_lookup(MEMBER, RENEWED).state == "in_progress"
    assert len(calls) == 1


def test_duplicate_text_in_echo_is_not_treated_as_a_provider_error(make_client):
    client, _ = make_client({"submit_request_echo": {"message": DUPLICATE}})
    assert client.submit_member_with_lookup(MEMBER, RENEWED).state == "unavailable"


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (httpx.ReadTimeout, "outcome_unknown"),
        (httpx.ConnectError, "unavailable"),
        (httpx.ConnectTimeout, "unavailable"),
        (httpx.PoolTimeout, "unavailable"),
        (httpx.ReadError, "unavailable"),
        (RuntimeError, "unavailable"),
    ],
)
def test_submit_transport_failures_never_expose_exception_text(make_client, caplog, error, state):
    client, calls = make_client(error=error)
    with caplog.at_level(logging.DEBUG):
        result = client.submit_member_with_lookup(MEMBER, RENEWED)
    assert result.state == state
    assert len(calls) == 1
    assert_private(result, caplog)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_submit_server_errors_are_unavailable_even_with_success_in_body(
    make_client, status, caplog
):
    client, _ = make_client({"stored_member": True, "spar_names": SPAR_NAME}, status=status)
    result = client.submit_member_with_lookup(MEMBER, RENEWED)
    assert result.state == "unavailable"
    assert_private(result, caplog)


@pytest.mark.parametrize("operation", ["validate", "submit", "confirm"])
def test_invalid_json_never_exposes_body_data(make_client, caplog, operation):
    client, _ = make_client(content=f"invalid JSON {PNR} {API_KEY} {SPAR_NAME}".encode())
    with caplog.at_level(logging.DEBUG):
        if operation == "validate":
            result = client.validate_member(MEMBER)
            assert not result.ok
        elif operation == "submit":
            result = client.submit_member_with_lookup(MEMBER, RENEWED)
            assert result.state == "unavailable"
        else:
            result = client.confirm_membership(PNR, 2026)
            assert result.found is None
    assert result.error_code == "provider_unavailable"
    assert_private(result, caplog)


@pytest.mark.parametrize("response", [None, [], "unknown", {"stored_member": "true"}])
def test_malformed_success_shapes_are_unavailable(make_client, response):
    client, _ = make_client(response)
    assert client.submit_member_with_lookup(MEMBER, RENEWED).state == "unavailable"
    assert client.validate_member(MEMBER).error_code == "provider_unavailable"
    assert client.confirm_membership(PNR, 2026).found is None


def test_confirm_reference_response_and_integer_year_request(make_client, caplog):
    client, calls = make_client(
        {
            "response": {
                "member_found": True,
                "renewed": "20261106",
                "registration_source": "Inskriven manuellt",
                "spar_names": {"lastname": SPAR_NAME},
            },
            "request_echo": {"socialsecuritynumber": PNR, "api_key": API_KEY},
        }
    )
    with caplog.at_level(logging.DEBUG):
        result = client.confirm_membership(PNR, 2026)
    assert result == ConfirmResult(True, "Inskriven manuellt")
    request = json.loads(calls[0].content)["request"]
    assert request == {
        "action": "confirm_membership",
        "association_number": "F161215-9",
        "api_key": API_KEY,
        "year_id": 2026,
        "firstname": "",
        "lastname": "",
        "socialsecuritynumber": PNR,
        "email": "",
        "phone1": "",
        "member_nick": None,
        "discord_user_id": None,
    }
    assert type(request["year_id"]) is int
    assert calls[0].url.path == "/apis/confirm_membership.json"
    assert calls[0].extensions["timeout"]["read"] == 15
    assert_private(result, caplog)


def test_confirm_false_is_distinct_from_unavailable(make_client):
    client, _ = make_client({"response": {"member_found": False}})
    assert client.confirm_membership(PNR, 2026) == ConfirmResult(False)


@pytest.mark.parametrize("status", [199, 300, 401, 403, 429, 500])
@pytest.mark.parametrize("operation", ["validate", "confirm"])
def test_non_success_status_never_confirms_even_when_body_claims_success(
    make_client, caplog, status, operation
):
    client, calls = make_client(
        {"response": {"member_found": False, "request_result": "success"}},
        status=status,
    )
    result = (
        client.validate_member(MEMBER)
        if operation == "validate"
        else client.confirm_membership(PNR, 2026)
    )
    assert result.error_code == "provider_unavailable"
    if operation == "confirm":
        assert result.found is None
    else:
        assert result.ok is False
    assert len(calls) == 1
    assert_private(result, caplog)


@pytest.mark.parametrize("location", ["outer", "inner"])
@pytest.mark.parametrize("marker,value", [("error", None), ("errors", []), ("error", "private"), ("request_result", "error")])
@pytest.mark.parametrize("operation", ["validate", "confirm"])
def test_error_fields_cannot_be_interpreted_as_membership_or_valid_input(
    make_client, caplog, location, marker, value, operation
):
    response = {"response": {"member_found": False, "request_result": "success"}}
    part = response if location == "outer" else response["response"]
    part[marker] = value
    client, _ = make_client(response)
    result = (
        client.validate_member(MEMBER)
        if operation == "validate"
        else client.confirm_membership(PNR, 2026)
    )
    assert result.error_code == "provider_unavailable"
    if operation == "confirm":
        assert result.found is None
    else:
        assert result.ok is False
    assert_private(result, caplog)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3001011232", "203001011232"),
        ("3101011231", "193101011231"),
        ("300101-1232", "203001011232"),
        ("310101-1231", "193101011231"),
        ("000101+1238", "190001011238"),
        ("900101+1239", "189001011239"),
        ("300101+1232", "183001011232"),
        ("310101+1231", "183101011231"),
        ("20000101-0008", PNR),
        (PNR, PNR),
    ],
)
def test_check_normalization_matches_flow_cutoff_and_preserves_100_plus(
    raw, expected, monkeypatch, caplog
):
    from shared import ebas_client

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 7)

    monkeypatch.setattr(ebas_client, "date", FixedDate)
    assert normalize_check_pnr(raw) == expected
    assert raw not in caplog.text and expected not in caplog.text


@pytest.mark.parametrize("raw", ["20000101+0008", "000101-0009", "invalid-" + PNR, "０００１０１０００８", None])
def test_check_normalization_rejects_invalid_input_without_echoing_it(raw, caplog):
    with pytest.raises(ValueError) as error:
        normalize_check_pnr(raw)
    if raw:
        assert raw not in str(error.value) + caplog.text


@pytest.mark.parametrize("source", [SPAR_NAME, PNR, API_KEY, "unverified provider text"])
def test_unapproved_registration_source_text_is_not_returned(make_client, caplog, source):
    client, _ = make_client({"response": {"member_found": True, "registration_source": source}})
    result = client.confirm_membership(PNR, 2026)
    assert result == ConfirmResult(True)
    assert_private(result, caplog)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_validation_and_confirmation_transport_failures_are_unavailable(make_client, caplog, error):
    client, _ = make_client(error=error)
    validation = client.validate_member(MEMBER)
    confirmation = client.confirm_membership(PNR, 2026)
    assert validation.error_code == "provider_unavailable"
    assert confirmation == ConfirmResult(None, error_code="provider_unavailable")
    assert_private(validation, caplog)
    assert_private(confirmation, caplog)


@pytest.mark.parametrize("year", ["2026", True, 0, 999, 10000, None])
def test_confirm_rejects_noninteger_or_invalid_years_before_sending(make_client, year):
    client, calls = make_client()
    with pytest.raises(ValueError) as captured:
        client.confirm_membership(PNR, year)
    assert PNR not in str(captured.value)
    assert calls == []


@pytest.mark.parametrize("renewed", ["2026-11-06", datetime(2026, 11, 6), None])
def test_submit_requires_a_date_before_sending(make_client, renewed):
    client, calls = make_client()
    with pytest.raises(ValueError) as captured:
        client.submit_member_with_lookup(MEMBER, renewed)
    assert PNR not in str(captured.value)
    assert calls == []


def test_injected_client_remains_owned_by_caller(make_client):
    client, _ = make_client()
    client.close()
    assert not client._client.is_closed


def test_owned_client_disables_environment_proxy_credentials_and_is_closed(monkeypatch):
    transport = MagicMock()
    factory = MagicMock(return_value=transport)
    monkeypatch.setattr(httpx, "Client", factory)
    with EbasClient(EbasConfig(API_KEY)) as client:
        assert client.config.api_key == API_KEY
    factory.assert_called_once_with(trust_env=False)
    transport.close.assert_called_once()


@pytest.mark.parametrize("operation", ["validate", "submit", "confirm"])
def test_provider_reason_phrase_and_transport_diagnostics_are_private(caplog, operation):
    echo = f"Provider echo {PNR} {API_KEY} {SPAR_NAME}"
    transport_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    original_levels = [item.level for item in transport_loggers]
    original_filters = [list(item.filters) for item in transport_loggers]

    def handle(request):
        for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.new_ebas_diagnostic"):
            logging.getLogger(name).warning("Provider-controlled metadata: %s", echo)
        logging.getLogger("unrelated.application").info("Unrelated application logging preserved")
        return httpx.Response(
            400,
            json={
                "stored_member": True,
                "request_result": "success",
                "response": {"member_found": True, "request_result": "success"},
            },
            extensions={"reason_phrase": echo.encode("ascii")},
        )

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            client = EbasClient(EbasConfig(API_KEY), transport)
            if operation == "validate":
                result = client.validate_member(MEMBER)
            elif operation == "submit":
                result = client.submit_member_with_lookup(MEMBER, RENEWED)
            else:
                result = client.confirm_membership(PNR, 2026)
        assert_private(result, caplog)
        assert "Unrelated application logging preserved" in caplog.messages
        assert not any(record.name.startswith(("httpx", "httpcore")) for record in caplog.records)
        assert [item.level for item in transport_loggers] == original_levels
        assert [item.filters for item in transport_loggers] == original_filters
        logging.getLogger("httpx").warning("Outside eBas logging preserved")
        assert "Outside eBas logging preserved" in caplog.messages


def test_transport_logging_is_restored_after_request_failure(make_client, caplog):
    client, _ = make_client(error=httpx.ReadTimeout)
    with caplog.at_level(logging.DEBUG):
        result = client.submit_member_with_lookup(MEMBER, RENEWED)
        assert result.state == "outcome_unknown"
        assert_private(result, caplog)
        logging.getLogger("httpcore.connection").warning("Post-failure transport logging preserved")
    assert "Post-failure transport logging preserved" in caplog.messages


def test_unrelated_thread_httpx_logging_is_preserved_during_ebas_call(caplog):
    def handle(request):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                logging.getLogger("httpx").warning, "Other thread logging preserved"
            ).result(timeout=5)
        logging.getLogger("httpcore.http11").warning("Echo %s %s %s", PNR, API_KEY, SPAR_NAME)
        return httpx.Response(200, json={"response": {"member_found": True}})

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            result = EbasClient(EbasConfig(API_KEY), transport).confirm_membership(PNR, 2026)
    assert "Other thread logging preserved" in caplog.messages
    assert_private(result, caplog)


@pytest.mark.parametrize("status", [200, 400])
def test_concurrent_ebas_calls_do_not_leak_provider_metadata(caplog, status):
    barrier = Barrier(2)
    echo = f"Echo {PNR} {API_KEY} {SPAR_NAME}"

    def handle(request):
        barrier.wait(timeout=5)
        logging.getLogger("httpcore.http11").warning("Provider metadata: %s", echo)
        return httpx.Response(
            status,
            json={"response": {"member_found": True}},
            extensions={"reason_phrase": echo.encode("ascii")},
        )

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            client = EbasClient(EbasConfig(API_KEY), transport)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(client.confirm_membership, PNR, 2026)
                    for _ in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]
    expected = (
        ConfirmResult(True)
        if status == 200
        else ConfirmResult(None, error_code="provider_unavailable")
    )
    assert all(result == expected for result in results)
    for result in results:
        assert_private(result, caplog)

"""Synthetic eBas rehearsal outcomes through the actual client and local transport."""

import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from backend.validation import validate_personnummer
from shared import ebas_client as ebas
from shared import ebas_stub_scenario as scenarios

PNR = "189001019802"
OTHER = "189001029819"
RENEWED = date(2026, 11, 7)
SCENARIO = Path(__file__).resolve().parents[1] / "scripts" / "sim" / "ebas_fightbox.json"


@pytest.fixture(autouse=True)
def offline_dev(monkeypatch):
    monkeypatch.setenv("EBAS_TRANSPORT", "stub")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("IS_PROD", raising=False)
    monkeypatch.delenv("EBAS_STUB_SCENARIO", raising=False)
    monkeypatch.delenv("EBAS_STUB_CONFIRM", raising=False)
    sleeps = []
    monkeypatch.setattr(scenarios, "sleep", sleeps.append)
    return sleeps


@pytest.fixture
def install(tmp_path, monkeypatch):
    def write(config, name="scenario"):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("EBAS_STUB_SCENARIO", str(path))
        return path

    return write


def config(**overrides):
    return {"version": 1, "synthetic": True, "seed": 73, **overrides}


def call(client, action, pnr=PNR):
    if action == "confirm":
        return client.confirm_membership(pnr, 2026)
    member = ebas.MemberInput(pnr, "scenario@example.invalid", "0000000000")
    if action == "validate":
        return client.validate_member(member)
    return client.submit_member_with_lookup(member, RENEWED)


@pytest.mark.parametrize("action", ["confirm", "validate", "submit"])
def test_people_have_independent_per_action_positive_and_negative_outcomes(install, action):
    install(config(people={PNR: {action: "member"}, OTHER: {action: "not_member"}}))
    with ebas.create_ebas_client() as client:
        positive = call(client, action)
        negative = call(client, action, OTHER)
    if action == "confirm":
        assert positive.found is True and negative.found is False
    elif action == "validate":
        assert positive.ok is True and negative.error_code == "validation_failed"
        assert negative.field_errors == ["socialsecuritynumber"]
    else:
        assert positive.state == "submitted" and negative.state == "rejected"
        assert negative.field_errors == ["socialsecuritynumber"]


def test_person_rules_override_only_one_action_and_other_people_use_defaults(install):
    install(config(defaults={"confirm": "not_member"}, people={PNR: {"confirm": "member"}}))
    with ebas.create_ebas_client() as client:
        assert call(client, "confirm").found is True
        assert call(client, "confirm", OTHER).found is False
        assert call(client, "validate", OTHER).ok is True
        assert call(client, "submit", OTHER).state == "submitted"


@pytest.mark.parametrize("action", ["confirm", "validate", "submit"])
@pytest.mark.parametrize("outcome", ["error_500", "non_json_503", "timeout"])
def test_provider_failures_never_become_not_a_member(install, offline_dev, action, outcome):
    install(config(people={PNR: {action: outcome}}))
    with ebas.create_ebas_client() as client:
        result = call(client, action)
    if action == "confirm":
        assert result.found is None and result.error_code == "provider_unavailable"
    elif action == "validate":
        assert result.ok is False and result.error_code == "provider_unavailable"
        assert result.field_errors == []
    else:
        assert result.state == ("outcome_unknown" if outcome == "timeout" else "unavailable")
        assert result.error_code == (
            "outcome_unknown" if outcome == "timeout" else "provider_unavailable"
        )
    if outcome == "timeout":
        assert offline_dev == [200 if action == "submit" else 15]


@pytest.mark.parametrize("outcome,status", [("error_500", 500), ("non_json_503", 503)])
def test_raw_error_responses_have_the_requested_status_and_no_request_echo(
    install, outcome, status
):
    install(config(defaults={"confirm": outcome}))
    response = ebas.StubEbasTransport().handle_request(
        httpx.Request(
            "POST",
            "https://ebas.stub.invalid/confirm_membership.json",
            json={"request": {"socialsecuritynumber": PNR}},
        )
    )
    assert response.status_code == status
    assert PNR not in response.text
    if status == 503:
        with pytest.raises(ValueError):
            response.json()


@pytest.mark.parametrize("delay_ms,timed_out", [(10, False), (30, True)])
def test_slow_response_obeys_the_clients_read_deadline(install, offline_dev, delay_ms, timed_out):
    install(config(defaults={"confirm": {"outcome": "member", "delay_ms": delay_ms}}))
    with ebas.EbasClient(
        ebas.EbasConfig("stub", confirm_timeout_seconds=0.02), transport=ebas.StubEbasTransport()
    ) as client:
        result = call(client, "confirm")
    assert result.found is (None if timed_out else True)
    assert offline_dev == [0.02 if timed_out else 0.01]


def test_slow_shorthand_still_returns_a_member(install, offline_dev):
    install(config(defaults={"confirm": "slow(123)"}))
    with ebas.create_ebas_client() as client:
        assert call(client, "confirm").found is True
    assert offline_dev == [0.123]


def test_seeded_global_latency_is_reproducible_across_new_factory_clients(install, offline_dev):
    def replay(seed, name):
        install(config(seed=seed, latency={"p50_ms": 2, "p95_ms": 8, "timeout_rate": 0.2}), name)
        start = len(offline_dev)
        results = []
        for _ in range(30):
            with ebas.create_ebas_client() as client:
                results.append(call(client, "confirm").found)
        return results, offline_dev[start:]

    first = replay(73, "first")
    assert replay(73, "repeat") == first
    assert replay(74, "different") != first
    assert True in first[0] and None in first[0]
    assert len(set(first[1])) > 1
    assert all(delay == 15 or 0 <= delay <= 0.016 for delay in first[1])


@pytest.mark.parametrize(
    "variable,value", [("IS_PROD", "true"), ("APP_ENV", "production"), ("ENV", "prod")]
)
def test_scenario_cannot_be_constructed_in_production(install, monkeypatch, variable, value):
    install(config())
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match="unavailable in production"):
        ebas.create_ebas_client()


def test_scenario_also_refuses_if_environment_changes_after_creation(install, monkeypatch):
    install(config())
    with ebas.create_ebas_client() as client:
        monkeypatch.setenv("IS_PROD", "true")
        assert call(client, "confirm") == ebas.ConfirmResult(None, error_code="stub_transport")
        assert call(client, "validate").error_code == "provider_unavailable"
        assert call(client, "submit").state == "unavailable"


def test_live_transport_ignores_scenario_configuration(monkeypatch):
    monkeypatch.setenv("EBAS_TRANSPORT", "live")
    monkeypatch.setenv("EBAS_ALLOW_LIVE_WITH_ENGINE_NONE", "1")
    monkeypatch.setenv("EBAS_STUB_SCENARIO", "nonexistent-synthetic-scenario.json")
    constructor = Mock(return_value=object())
    monkeypatch.setattr(ebas, "EbasClient", constructor)
    assert ebas.create_ebas_client() is constructor.return_value
    constructor.assert_called_once_with()


@pytest.mark.parametrize(
    "overrides",
    [
        {"synthetic": False},
        {"seed": "private"},
        {"defaults": {"confirm": "typo"}},
        {"defaults": {"submit": {"delay_ms": -1}}},
        {"latency": {"p50_ms": 100, "p95_ms": 10}},
        {"latency": {"timeout_rate": 2}},
        {"people": {PNR: {"validate": PNR}}},
    ],
)
def test_invalid_configuration_fails_closed_without_disclosing_values(install, overrides, caplog):
    install(config(**overrides))
    with pytest.raises(ValueError) as error:
        ebas.create_ebas_client()
    assert str(error.value) == "Invalid synthetic eBas stub scenario"
    assert PNR not in str(error.value) + caplog.text


def test_malformed_or_missing_file_fails_without_echoing_file_contents(tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    for content in (None, '{"private": "' + PNR):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("EBAS_STUB_SCENARIO", str(path))
        with pytest.raises(ValueError, match="^Invalid synthetic eBas stub scenario$"):
            ebas.create_ebas_client()


def test_scenario_logs_and_public_results_never_contain_personal_ids(install, caplog):
    install(
        config(
            people={PNR: {"confirm": "error_500", "validate": "not_member", "submit": "timeout"}}
        )
    )
    with caplog.at_level(logging.DEBUG), ebas.create_ebas_client() as client:
        results = [asdict(call(client, action)) for action in ("confirm", "validate", "submit")]
    output = caplog.text + json.dumps(results)
    assert PNR not in output and PNR[-10:] not in output
    assert "scenario@example.invalid" not in output


@pytest.mark.parametrize(
    "action,setting,status,body",
    [
        (
            "validate_members",
            None,
            200,
            b'{"response":{"request_result":"success","member_errors":[]}}',
        ),
        (
            "submit_member_with_lookup",
            None,
            200,
            b'{"stored_member":true,"request_result":"success"}',
        ),
        ("confirm_membership", "found", 200, b'{"response":{"member_found":true}}'),
        ("confirm_membership", "not_found", 200, b'{"response":{"member_found":false}}'),
        ("confirm_membership", None, 503, b'{"error":"stub_transport"}'),
    ],
)
def test_no_scenario_preserves_default_response_bytes(monkeypatch, action, setting, status, body):
    if setting:
        monkeypatch.setenv("EBAS_STUB_CONFIRM", setting)
    response = ebas.StubEbasTransport().handle_request(
        httpx.Request("POST", f"https://ebas.stub.invalid/{action}.json")
    )
    expected_bytes = httpx.Response(status, json=json.loads(body)).content
    assert response.status_code == status and response.content == expected_bytes
    assert response.extensions == ({"ebas_stub_unavailable": True} if status == 503 else {})


def test_shipped_rehearsal_has_reserved_test_ids_and_one_deliberately_invalid_id(offline_dev):
    settings = json.loads(SCENARIO.read_text(encoding="utf-8"))
    scenarios.load_scenario(SCENARIO)
    people = settings["people"]
    invalid = [rules["key"] for pnr, rules in people.items() if not validate_personnummer(pnr)[0]]
    assert invalid == ["invalid-checksum"]
    assert len(people) == 20
    assert sum(rules.get("confirm") == "not_member" for rules in people.values()) == 3
    assert settings["latency"] == {"p50_ms": 400, "p95_ms": 2500, "timeout_rate": 0.02}


def test_invalid_personal_id_is_rejected_before_the_stub(install, monkeypatch):
    install(config())
    probe = Mock(side_effect=AssertionError("Invalid ID reached transport"))
    monkeypatch.setattr(ebas.StubEbasTransport, "handle_request", probe)
    with ebas.create_ebas_client() as client:
        with pytest.raises(ValueError, match="failed validation"):
            client.confirm_membership("189001209818", 2026)
    probe.assert_not_called()

"""Guard and response-contract tests for the DEV-only FightBox simulator."""

import copy
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from scripts.sim import ebas_http_sim as server
from scripts.sim import fightbox_rehearsal as rehearsal
from shared import ebas_stub_scenario
from tests.test_stats_recompute import private_stats_lock_namespace  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PNR = "189001019802"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("IS_PROD", "false")
    monkeypatch.setattr(ebas_stub_scenario, "sleep", lambda delay: None)
    value = json.loads((ROOT / "scripts/sim/ebas_fightbox.json").read_text(encoding="utf-8"))
    value["latency"] = {}
    return value


@pytest.mark.parametrize(
    "target",
    [
        "https://fgc.example.com",
        "http://prod.example.invalid:8088",
        "http://localhost:80",
        "http://127.0.0.1:5679",
        "https://localhost:8088",
        "http://localhost:bad",
        "http://user:secret@localhost:8088",
        "http://localhost:8088/api",
        "http://localhost:8088?redirect=prod",
    ],
)
def test_prod_and_non_dev_targets_are_refused(target):
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.guard_target(target)


def test_only_the_explicit_dev_loopback_port_is_allowed():
    assert rehearsal.guard_target("http://localhost:8088/") == "http://localhost:8088"
    assert rehearsal.guard_target("http://127.0.0.1:8088") == "http://127.0.0.1:8088"


@pytest.mark.parametrize(
    "container,change",
    [
        ("backend", {"EBAS_TRANSPORT": "live"}),
        ("backend", {"EBAS_STUB_SCENARIO": ""}),
        ("backend", {"INTEGRATION_ENGINE": "none"}),
        ("backend", {"MEMBERSHIP_FLOW": "v2"}),
        ("backend", {"IS_PROD": "true"}),
        ("n8n", {"APP_ENV": "production"}),
        ("n8n", {"EBAS_API_KEY": "synthetic-non-dummy"}),
        ("n8n", {"EBAS_ASSOC": "wrong"}),
        ("n8n", {"STARTGG_TOKEN": "synthetic-non-dummy"}),
    ],
)
def test_live_provider_or_wrong_runtime_is_refused(container, change):
    backend = {
        "EBAS_TRANSPORT": "stub",
        "EBAS_STUB_SCENARIO": "/sim/scenario.json",
        "INTEGRATION_ENGINE": "n8n",
        "MEMBERSHIP_FLOW": "legacy",
    }
    n8n = {"EBAS_API_KEY": "sim-dummy", "EBAS_ASSOC": "sim", "STARTGG_TOKEN": "sim-dummy"}
    rehearsal.guard_environment(backend, n8n)
    (backend if container == "backend" else n8n).update(change)
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.guard_environment(backend, n8n)


def test_non_synthetic_person_is_refused(settings):
    people = server.synthetic_people(settings)
    rehearsal.guard_person(PNR, people)
    with pytest.raises(rehearsal.RehearsalError, match="synthetic"):
        rehearsal.guard_person("200001010008", people)


def test_alternate_identity_catalog_is_refused_before_any_docker_operation(tmp_path, monkeypatch):
    path = tmp_path / "foreign.json"
    path.write_text(
        json.dumps({"synthetic": True, "people": {"200001010008": {"key": "foreign"}}}),
        encoding="utf-8",
    )

    def unexpected_docker():
        raise AssertionError("Docker was reached before identity catalog validation")

    monkeypatch.setattr(rehearsal, "Docker", unexpected_docker)
    with pytest.raises(rehearsal.RehearsalError, match="shipped FightBox"):
        rehearsal.main(["--scenario", str(path)])


@pytest.mark.parametrize(
    "value",
    [
        PNR,
        PNR[-10:],
        "18900101-9802",
        "900101-9802",
        {"pnr_hash": "opaque"},
        {"details": hashlib.sha256(PNR.encode()).hexdigest()},
        {"people": [{"status_token": "private"}]},
    ],
)
def test_report_refuses_personal_ids_tokens_and_reversible_digests(value):
    with pytest.raises(rehearsal.RehearsalError, match="Report refused"):
        rehearsal.guard_report(value)


def test_safe_role_report_is_allowed():
    rehearsal.guard_report({"role": "member-01", "status": "Pending", "latency_ms": 15000.2})


def workflows():
    return [
        {
            "id": "originalRoot",
            "name": "FGC THN - Root",
            "nodes": [
                {"type": "n8n-nodes-base.webhook", "parameters": {"path": "checkin/validate-v5"}},
                {
                    "type": "n8n-nodes-base.httpRequest",
                    "parameters": {"url": "https://api.start.gg/gql/alpha"},
                },
                {
                    "type": "n8n-nodes-base.executeWorkflow",
                    "parameters": {"workflowId": "originalRegister"},
                },
            ],
            "connections": {},
        },
        {
            "id": "originalRegister",
            "name": "FGC THN - Registration",
            "nodes": [
                {"type": "n8n-nodes-base.webhook", "parameters": {"path": "ebas/register-v2"}},
                {
                    "type": "n8n-nodes-base.httpRequest",
                    "parameters": {
                        "url": "https://ebas.sverok.se/apis/submit_member_with_lookup.json"
                    },
                },
            ],
            "connections": {},
        },
    ]


def test_generated_copies_rewrite_providers_and_workflow_references_without_mutating_originals():
    originals = workflows()
    saved = copy.deepcopy(originals)
    copies = rehearsal.rehearsal_flows(originals)
    rehearsal.guard_flows(copies)
    assert originals == saved
    assert copies[0]["nodes"][2]["parameters"]["workflowId"] == copies[1]["id"]
    assert copies[0]["nodes"][1]["parameters"]["url"] == "http://t074-startgg-sim:8080/gql/alpha"
    assert (
        copies[1]["nodes"][1]["parameters"]["url"]
        == "http://t074-ebas-sim:8080/apis/submit_member_with_lookup.json"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://ebas.sverok.se/apis/confirm_membership.json",
        "https://api.start.gg/gql/alpha",
        "http://somewhere.invalid/",
    ],
)
def test_active_published_flow_guard_refuses_any_unapproved_http_destination(url):
    copies = rehearsal.rehearsal_flows(workflows())
    copies[0]["nodes"][1]["parameters"]["url"] = url
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.guard_flows(copies)


def test_active_flow_guard_refuses_original_ids_and_stored_credentials():
    copies = rehearsal.rehearsal_flows(workflows())
    copies[0]["id"] = "originalRoot"
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.guard_flows(copies)
    copies = rehearsal.rehearsal_flows(workflows())
    copies[0]["nodes"][1]["credentials"] = {"httpHeaderAuth": {"id": "synthetic-fixture"}}
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.guard_flows(copies)


def test_tracked_dev_checkin_and_membership_limits_match_prod():
    prod = rehearsal.rate_policy((ROOT / "nginx.conf").read_text(encoding="utf-8"))
    dev = rehearsal.rate_policy((ROOT / "nginx.dev.conf").read_text(encoding="utf-8"))
    assert prod == dev and len(prod) >= 8
    assert prod["= /api/checkin/orchestrate"] == {"rate": "60r/m", "burst": 40, "nodelay": True}
    assert prod["= /api/ebas/register"] == {"rate": "10r/m", "burst": 30, "nodelay": True}


def test_http_ebas_sim_reuses_sverok_shapes_and_rejects_non_synthetic_input(settings):
    simulation = server.Simulation("ebas", settings, "t074-test-")
    member = {"socialsecuritynumber": PNR}
    response = simulation.respond(
        "/apis/confirm_membership.json", {"request": {**member, "api_key": "sim-dummy"}}
    )
    assert response.json() == {"response": {"member_found": True}}
    response = simulation.respond(
        "/apis/submit_member_with_lookup.json", {"member": member, "api_key": "sim-dummy"}
    )
    assert response.json() == {"stored_member": True, "request_result": "success"}
    for payload in (
        {"request": {"socialsecuritynumber": "200001010008", "api_key": "sim-dummy"}},
        {"request": {**member, "api_key": "synthetic-wrong-key"}},
    ):
        response = simulation.respond("/apis/confirm_membership.json", payload)
        assert response.status_code == 403
        assert PNR not in response.text


def test_http_ebas_errors_are_non_membership_responses(settings):
    simulation = server.Simulation("ebas", settings, "t074-test-")
    for person, expected_status in (("non-json-unavailable", 503), ("provider-error", 500)):
        pnr = next(pnr for pnr, entry in settings["people"].items() if entry["key"] == person)
        response = simulation.respond(
            "/apis/confirm_membership.json",
            {"request": {"socialsecuritynumber": pnr, "api_key": "sim-dummy"}},
        )
        assert response.status_code == expected_status
        assert "member_found" not in response.text and pnr not in response.text


def test_fake_startgg_returns_registered_players_guests_and_only_the_mock_event(settings):
    simulation = server.Simulation("startgg", settings, "t074-test-")
    tags = server.person_tags(settings, "t074-test-")
    for role, count in (("member-01", 1), ("member-02", 0)):
        result = simulation.respond(
            "/gql/alpha", {"variables": {"tag": tags[role], "slug": "t074-fightbox-test"}}
        )
        assert len(result.json()["data"]["tournament"]["participants"]["nodes"]) == count
    assert (
        simulation.respond(
            "/gql/alpha", {"variables": {"tag": "real-tag", "slug": "real-event"}}
        ).status_code
        == 400
    )


def test_simulators_refuse_production(settings, monkeypatch):
    monkeypatch.setenv("IS_PROD", "true")
    with pytest.raises(ValueError, match="non-production"):
        server.Simulation("ebas", settings, "t074-test-")


@pytest.mark.db
def test_product_helpers_confirm_a_duplicate_and_roundtrip_archive_with_backfills(
    pg_db, monkeypatch
):
    from shared import migrate
    from shared import postgres_api as pg

    migrate.apply(pg_db.pool)
    pg_db.seed_settings(active_event_slug=None)

    class LocalBackend:
        def run(self, *args, input=None, **kwargs):
            monkeypatch.setattr(sys, "stdin", io.StringIO(input.decode()))
            output = io.StringIO()
            with redirect_stdout(output):
                exec(args[-1], {})
            return output.getvalue().encode()

    client = LocalBackend()
    slug, prefix = "t074-fightbox-helper", "t074-helper-"
    tags = {"member-01": prefix + "01", "member-03": prefix + "03"}
    setup = rehearsal.backend_action(client, "setup", slug, prefix, tags=tags)
    for role in ("member-01", "member-03"):
        pg.begin_checkin(
            slug, {"name": "Synthetic " + role, "tag": tags[role] + "x", "member": True}
        )
    result = rehearsal.backend_action(
        client,
        "duplicates",
        slug,
        prefix,
        candidates=setup["seed_profiles"],
        typos={tag + "x": role for role, tag in tags.items()},
    )
    assert result["hint_count"] >= 2
    rehearsal.backend_action(client, "end", slug, prefix)
    first = rehearsal.backend_action(client, "archive", slug, prefix)["facts"]
    assert len(first) == 2 and all(row["checked_in_at"] for row in first)
    rehearsal.backend_action(client, "reopen", slug, prefix)
    filled = rehearsal.backend_action(client, "backfills", slug, prefix)
    second = rehearsal.backend_action(client, "archive", slug, prefix)["facts"]
    assert len(second) == 4
    assert {
        row["id"]: row["checked_in_at"]
        for row in second
        if row["id"] in {saved["id"] for saved in first}
    } == {row["id"]: row["checked_in_at"] for row in first}
    stats = rehearsal.backend_action(client, "stats", slug, prefix, since=filled["checkpoint"])
    assert stats["stale"] is False and stats["stats"]["total_revenue"] == 0
    assert stats["stale_markers"] == 0 and stats["automatic_refresh_audits"] >= 1
    assert stats["stats"]["total_participants"] == 4


def test_every_shipped_public_payload_fits_the_real_product_validator(settings):
    from backend.validation import validate_checkin_payload

    tags = server.person_tags(settings, "t074-20261107t235959z-")
    phones = {
        person["key"]: f"+1202555{100+index:04d}"
        for index, person in enumerate(settings["people"].values())
    }
    for pnr, person in settings["people"].items():
        role = person["key"]
        for typo in (False, True):
            payload = rehearsal.public_payload(pnr, role, tags, phones, typo=typo)
            assert len(payload["tag"]) <= 30
            errors = validate_checkin_payload(payload)
            assert bool(errors) is (role == "invalid-checksum")


def test_latency_trace_records_actual_scenario_wait_without_personal_data(
    settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("EBAS_TRANSPORT", "stub")
    destination = tmp_path / "trace.jsonl"
    restore = server.install_latency_trace(settings, destination)
    try:
        simulation = server.Simulation("ebas", settings, "t074-trace-")
        for role in ("slow-confirm", "confirm-timeout"):
            pnr = next(pnr for pnr, entry in settings["people"].items() if entry["key"] == role)
            payload = {"request": {"socialsecuritynumber": pnr, "api_key": "sim-dummy"}}
            if role == "confirm-timeout":
                with pytest.raises(httpx.ReadTimeout):
                    simulation.respond("/apis/confirm_membership.json", payload)
            else:
                assert (
                    simulation.respond("/apis/confirm_membership.json", payload).status_code == 200
                )
    finally:
        restore()
    text = destination.read_text(encoding="utf-8")
    entries = [json.loads(line) for line in text.splitlines()]
    assert [entry["injected_ms"] for entry in entries] == [3500, 15000]
    assert entries[1]["timeout"] is True
    assert all(pnr not in text and pnr[-10:] not in text for pnr in settings["people"])
    assert "sim-dummy" not in text
    rehearsal.guard_report(entries)


def test_latency_trace_refuses_live_transport(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("EBAS_TRANSPORT", "live")
    path = tmp_path / "trace.jsonl"
    with pytest.raises(ValueError, match="non-production stub"):
        server.install_latency_trace(settings, path)
    assert not path.exists()

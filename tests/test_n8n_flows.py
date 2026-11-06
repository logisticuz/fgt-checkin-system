"""Repo-only workflow hygiene and semantic invariants against base 28f8219."""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAVE_SETTINGS = {
    "saveDataSuccessExecution": "none",
    "saveDataErrorExecution": "none",
    "saveManualExecutions": False,
    "saveExecutionProgress": False,
}
PNR_PATTERN = re.compile(r"(?<!\d)(?:\d{12}|\d{10}|\d{8}[-+]\d{4}|\d{6}[-+]\d{4})(?!\d)")
BASE = {
    "FGC THN - Checkin Orchestrator v5 (PG).json": {
        "names": {
            "Combine Results",
            "Parse Input",
            "Report Start.gg Result",
            "Report eBas Result",
            "Return Results",
            "Start.gg Check",
            "Wait for Both",
            "Wait for Reports",
            "Webhook",
            "eBas Check",
        },
        "invariant": "3f67dddc84ebe48d8f27c999399d292018140823c36bcb0d020dd6f61c34a971",
        "callbacks": 4,
    },
    "FGC THN - eBas Membership Check.json": {
        "names": {
            "API Call",
            "Call eBas confirm_membership",
            "IF Skip eBas",
            "Normalize",
            "Normalize Personnummer",
            "Overview",
            "Parse Response",
            "Skip - Return False",
            "Webhook - eBas Check",
        },
        "invariant": "47052fcc6b5508ff7af31f8e742981f096c2473283a0de3e06a547007d7fb48d",
        "callbacks": 0,
    },
    "FGC THN - eBas Register v2 (PG).json": {
        "names": {
            "Call eBas API",
            "If Success",
            "Normalize Input",
            "Notify SSE",
            "Parse Response",
            "Report Member Status",
            "Return Error",
            "Return Success",
            "Webhook",
        },
        "invariant": "04ac9f19e6d4ccc0d59db1e9e6995c6a08716f828dcbe82daa1be2ea01120b8b",
        "callbacks": 2,
    },
    "FGC THN - Start.gg Check.json": {
        "names": {
            "Overview",
            "Parse Input",
            "Parse Response",
            "Query",
            "Query Start.gg",
            "Webhook",
        },
        "invariant": "035f0b3758821d50c715cac0108ebc3458b043640201ad5deac353b39e3b979c",
        "callbacks": 0,
    },
}


def versions(flow):
    yield flow
    if isinstance(flow.get("activeVersion"), dict):
        yield flow["activeVersion"]


def backend_call(node):
    return node.get("type") == "n8n-nodes-base.httpRequest" and (
        "http://backend:8000/api/" in node.get("parameters", {}).get("url", "")
    )


@pytest.fixture(params=BASE)
def workflow(request):
    name = request.param
    return name, json.loads((ROOT / "n8n" / "flows" / name).read_text(encoding="utf-8"))


def test_workflow_execution_payload_saving_is_disabled(workflow):
    _, flow = workflow
    for key, expected in SAVE_SETTINGS.items():
        assert flow["settings"][key] == expected
        if isinstance(expected, bool):
            assert flow["settings"][key] is expected


def test_every_backend_callback_sends_only_an_env_backed_internal_token(workflow):
    name, flow = workflow
    count = 0
    for version in versions(flow):
        for node in version["nodes"]:
            if not backend_call(node):
                continue
            count += 1
            parameters = node["parameters"]
            assert parameters["sendHeaders"] is True
            headers = [
                header
                for header in parameters["headerParameters"]["parameters"]
                if header["name"].lower() == "x-internal-token"
            ]
            assert headers == [
                {"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_API_TOKEN }}"}
            ]
    assert count == BASE[name]["callbacks"]


def test_node_names_match_embedded_base_sets(workflow):
    name, flow = workflow
    for version in versions(flow):
        names = [node["name"] for node in version["nodes"]]
        assert set(names) == BASE[name]["names"]
        assert len(names) == len(BASE[name]["names"])


def test_connections_executable_payloads_and_other_fields_match_base(workflow):
    name, flow = workflow
    preserved = deepcopy(flow)
    for key in SAVE_SETTINGS:
        preserved.get("settings", {}).pop(key, None)
    for version in versions(preserved):
        for node in version["nodes"]:
            parameters = node.get("parameters", {})
            if backend_call(node):
                parameters.pop("sendHeaders", None)
                headers = parameters.get("headerParameters")
                if isinstance(headers, dict):
                    headers["parameters"] = [
                        header
                        for header in headers.get("parameters", [])
                        if header.get("name", "").lower() != "x-internal-token"
                    ]
                    if headers == {"parameters": []}:
                        parameters.pop("headerParameters")
            if node.get("type") == "n8n-nodes-base.stickyNote" and node["name"] == "Normalize":
                content = PNR_PATTERN.sub("PNR_FORMAT_PLACEHOLDER", parameters.get("content", ""))
                for placeholder in ("YYYYMMDDXXXX", "YYMMDDXXXX", "YYMMDD-XXXX"):
                    content = content.replace(placeholder, "PNR_FORMAT_PLACEHOLDER")
                parameters["content"] = content
    encoded = json.dumps(
        preserved, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == BASE[name]["invariant"]


def test_flow_has_no_pnr_shaped_string_literals(workflow):
    _, flow = workflow

    def strings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    assert all(PNR_PATTERN.search(value) is None for value in strings(flow))


@pytest.mark.parametrize("name", [".env.example", "n8n/config/n8n.env.example"])
def test_example_tokens_are_empty_and_allowlisted_if_an_allowlist_exists(name):
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    tokens = [line for line in lines if line.startswith("INTERNAL_API_TOKEN=")]
    assert len(tokens) == 1
    assert tokens[0].split("=", 1)[1].split("#", 1)[0].strip() == ""
    for line in lines:
        if line.startswith("N8N_ENV_ACCESS_ALLOWLIST="):
            assert "INTERNAL_API_TOKEN" in line.split("=", 1)[1].split(",")


def test_all_repo_flow_json_files_parse():
    for path in (ROOT / "n8n" / "flows").glob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_operator_documentation_is_short():
    assert (
        len((ROOT / "docs" / "n8n-pnr-hygiene.md").read_text(encoding="utf-8").splitlines()) <= 30
    )

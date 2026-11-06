"""Repo-only workflow hygiene and reviewed T-016/T-028 semantic invariants."""

import hashlib
import json
import re
import shutil
import subprocess
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
PNR_PATTERN = re.compile(
    r"(?<!\d)(?:\d{12}|\d{10}|\d{8}[-+]\d{4}|\d{6}[-+]\d{4})(?!\d)"
)
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
        "invariant": "2fc1ab106f7b7f0bdbd435e0a32f83999ab57279499c8d7fb25a4f138595864c",
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
            "Skip - Return Result",  # T-028: skipped/unavailable no longer means false.
            "Webhook - eBas Check",
        },
        "invariant": "17a9a321edfc29be478fb07e440a03916f81267c8c3fd5d523d5ecb15fbdc59e",
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
            "IF Skip Start.gg",  # T-028: missing input bypasses the external API.
            "Parse Input",
            "Parse Response",
            "Query",
            "Query Start.gg",
            "Webhook",
        },
        "invariant": "e8091a127222a360d37b526cae7c26663fc80f328b6cf0647cefd6b6d2a369bf",
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


def semantic_digest(flow):
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
            if (
                node.get("type") == "n8n-nodes-base.stickyNote"
                and node["name"] == "Normalize"
            ):
                content = PNR_PATTERN.sub(
                    "PNR_FORMAT_PLACEHOLDER", parameters.get("content", "")
                )
                for placeholder in ("YYYYMMDDXXXX", "YYMMDDXXXX", "YYMMDD-XXXX"):
                    content = content.replace(placeholder, "PNR_FORMAT_PLACEHOLDER")
                parameters["content"] = content
    encoded = json.dumps(
        preserved, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_connections_executable_payloads_and_other_fields_match_reviewed_snapshot(
    workflow,
):
    # T-028 intentionally revises three snapshots for matching, timeouts and degraded states.
    # eBas Register remains pinned to T-016's original invariant.
    name, flow = workflow
    assert semantic_digest(flow) == BASE[name]["invariant"]


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
        len(
            (ROOT / "docs" / "n8n-pnr-hygiene.md")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        <= 30
    )


STARTGG = "FGC THN - Start.gg Check.json"
EBAS = "FGC THN - eBas Membership Check.json"
ORCHESTRATOR = "FGC THN - Checkin Orchestrator v5 (PG).json"


def flow_version(name, published=False):
    flow = json.loads((ROOT / "n8n" / "flows" / name).read_text(encoding="utf-8"))
    return flow["activeVersion"] if published else flow


def node_named(version, name):
    return next(node for node in version["nodes"] if node["name"] == name)


def run_node(version, name, current=None, named=None):
    """Execute the exported Code node or body expression against synthetic n8n inputs."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable; static and mirrored Python checks still run")
    parameters = node_named(version, name)["parameters"]
    code = parameters.get("jsCode")
    if code is None:
        expression = parameters["jsonBody"]
        assert expression.startswith("={{") and expression.endswith("}}")
        code = "return (" + expression[3:-2].strip() + ");"
    program = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const lookup = name => {
  if (!(name in input.named)) throw new Error('Synthetic unavailable node');
  return { first: () => ({ json: input.named[name] }) };
};
const result = new Function('$json', '$', '$execution', input.code)(
  input.current, lookup, { id: 'synthetic-execution' });
process.stdout.write(JSON.stringify(result));
"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps(
            {"code": code, "current": current or {}, "named": named or {}}
        ),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    return json.loads(value) if isinstance(value, str) else value[0]["json"]


@pytest.mark.parametrize("name", [STARTGG, EBAS, ORCHESTRATOR])
def test_exported_versions_have_identical_execution_graphs(name):
    draft = flow_version(name)
    published = flow_version(name, published=True)
    assert draft["nodes"] == published["nodes"]
    assert draft["connections"] == published["connections"]


@pytest.mark.parametrize("name", [STARTGG, EBAS, ORCHESTRATOR])
@pytest.mark.parametrize("published", [False, True])
def test_integration_http_requests_are_bounded_and_continue_after_errors(
    name, published
):
    requests = [
        node
        for node in flow_version(name, published)["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest"
    ]
    assert len(requests) == (4 if name == ORCHESTRATOR else 1)
    for node in requests:
        assert 0 < node["parameters"]["options"]["timeout"] <= 10000
        assert (
            node.get("continueOnFail") is True
            or node.get("onError") == "continueRegularOutput"
        )


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "requested, candidates, expected",
    [
        ("Ax", ["Axel"], False),
        ("axel", ["Axel "], True),
        (" Axel ", ["aXeL"], True),
        ("Ax", ["Axel", " aX "], True),
        ("Ax", [], False),
    ],
)
def test_startgg_requires_exact_trimmed_case_insensitive_match(
    published, requested, candidates, expected
):
    version = flow_version(STARTGG, published)
    code = node_named(version, "Parse Response")["parameters"]["jsCode"]
    assert "ctx.tag.trim().toLowerCase()" in code
    assert "participant.gamerTag.trim().toLowerCase() === requestedTag" in code
    assert "participants[0]" not in code
    # Required Python mirror: the comparison is equality, never substring matching.
    assert (
        any(tag.strip().lower() == requested.strip().lower() for tag in candidates)
        is expected
    )
    result = run_node(
        version,
        "Parse Response",
        {
            "data": {
                "tournament": {
                    "name": "Synthetic Event",
                    "participants": {
                        "nodes": [
                            {"gamerTag": tag, "events": [{"name": tag + " game"}]}
                            for tag in candidates
                        ]
                    },
                }
            }
        },
        {"Parse Input": {"tag": requested, "attempt_id": "synthetic-attempt"}},
    )
    assert result["isRegistered"] is expected
    assert result["ok"] is True
    assert result["state"] == ("verified_true" if expected else "verified_false")
    assert result["attempt_id"] == "synthetic-attempt"
    if expected:
        assert result["tag"].strip().lower() == requested.strip().lower()


@pytest.mark.parametrize("published", [False, True])
def test_graphql_variables_are_serialized_in_code_with_special_characters(published):
    version = flow_version(STARTGG, published)
    code = node_named(version, "Parse Input")["parameters"]["jsCode"]
    assert "JSON.stringify({ query, variables: { slug, tag } })" in code
    assert re.search(r"const query = 'query[^\n]+';", code)
    assert (
        node_named(version, "Query Start.gg")["parameters"]["jsonBody"]
        == "={{ $json.requestBody }}"
    )
    tag = 'Ax"\\\n} mutation { synthetic }'
    slug = 'synthetic/"event\\'
    result = run_node(
        version,
        "Parse Input",
        {"body": {"tag": tag, "slug": slug, "attempt_id": "synthetic-attempt"}},
    )
    body = json.loads(result["requestBody"])
    assert body["variables"] == {"tag": tag, "slug": slug}
    assert tag not in body["query"] and slug not in body["query"]
    assert result["attempt_id"] == "synthetic-attempt"


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        {"error": "Synthetic timeout"},
        {
            "errors": [{"message": "Synthetic GraphQL failure"}],
            "data": {"tournament": {"participants": {"nodes": []}}},
        },
        {},
        {"data": {"tournament": None}},
        {"data": {"tournament": {"participants": {}}}},
    ],
)
def test_startgg_errors_are_unavailable_without_registration_boolean(
    published, response
):
    result = run_node(
        flow_version(STARTGG, published),
        "Parse Response",
        response,
        {"Parse Input": {"tag": "Ax", "attempt_id": "synthetic-attempt"}},
    )
    assert result["ok"] is False and result["state"] == "unavailable"
    assert result["error"] == "api_unavailable"
    assert "isRegistered" not in result


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "response, expected",
    [
        ({"response": {"member_found": True}}, True),
        ({"response": {"member_found": False}}, False),
        ({"error": "Synthetic timeout"}, None),
        ({}, None),
        ({"response": {"member_found": "false"}}, None),
    ],
)
def test_ebas_requires_explicit_api_boolean_and_marks_errors_unavailable(
    published, response, expected
):
    result = run_node(
        flow_version(EBAS, published),
        "Parse Response",
        response,
        {"Normalize Personnummer": {"attempt_id": "synthetic-attempt"}},
    )
    if expected is None:
        assert result["ok"] is False and result["state"] == "unavailable"
        assert result["error"] == "api_unavailable" and "isMember" not in result
    else:
        assert result["ok"] is True and result["isMember"] is expected
        assert result["state"] == ("verified_true" if expected else "verified_false")
    assert result["attempt_id"] == "synthetic-attempt"


@pytest.mark.parametrize("published", [False, True])
def test_missing_input_bypasses_external_requests_and_returns_skipped(published):
    startgg = flow_version(STARTGG, published)
    assert (
        startgg["connections"]["Parse Input"]["main"][0][0]["node"]
        == "IF Skip Start.gg"
    )
    assert (
        startgg["connections"]["IF Skip Start.gg"]["main"][0][0]["node"]
        == "Parse Response"
    )
    parsed = run_node(startgg, "Parse Input")
    result = run_node(startgg, "Parse Response", parsed, {"Parse Input": parsed})
    assert (
        result["ok"] is False
        and result["state"] == "skipped"
        and "isRegistered" not in result
    )
    ebas = flow_version(EBAS, published)
    assert (
        ebas["connections"]["IF Skip eBas"]["main"][0][0]["node"]
        == "Skip - Return Result"
    )
    parsed = run_node(ebas, "Normalize Personnummer")
    assert parsed["skipEbas"] is True
    result = run_node(ebas, "Skip - Return Result", parsed)
    assert (
        result["ok"] is False
        and result["state"] == "skipped"
        and "isMember" not in result
    )
    assert "personnummer" not in result and "_pnr_normalized" not in result
    valid_input = run_node(
        ebas, "Normalize Personnummer", {"body": {"personnummer": "0" * 12}}
    )
    assert valid_input["skipEbas"] is False


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "kind", ["positive", "negative", "timeout", "malformed", "skipped", "missing-node"]
)
def test_v5_report_payloads_distinguish_verification_from_errors(published, kind):
    version = flow_version(ORCHESTRATOR, published)
    ctx = {
        "checkin_id": "synthetic-checkin",
        "attempt_id": "synthetic-attempt",
        "_runEbas": True,
        "_runStartgg": True,
    }
    named = {"Parse Input": ctx}
    for title, field in [("eBas", "isMember"), ("Start.gg", "isRegistered")]:
        if kind == "missing-node":
            continue
        if kind in {"positive", "negative"}:
            payload = {
                field: kind == "positive",
                "ok": True,
                "events": ["Synthetic game"],
            }
        elif kind == "timeout":
            payload = {"error": "Synthetic timeout " + "0" * 12, field: False}
        elif kind == "skipped":
            payload = {"state": "skipped", "skipped": True}
        else:
            payload = {}
        named[title + " Check"] = payload
    combined = run_node(version, "Combine Results", named=named)
    expected_state = {
        "positive": "verified_true",
        "negative": "verified_false",
        "skipped": "skipped",
    }.get(kind, "unavailable")
    ok = kind in {"positive", "negative"}
    for title, source, field in [
        ("eBas", "ebas", "member"),
        ("Start.gg", "startgg", "registered"),
    ]:
        report = run_node(
            version, f"Report {title} Result", named={"Combine Results": combined}
        )
        assert report["source"] == source and report["ok"] is ok
        assert report["state"] == expected_state
        assert report["attempt_id"] == "synthetic-attempt"
        assert report["checkin_id"] == "synthetic-checkin"
        if ok:
            assert report["data"][field] is (kind == "positive")
            assert report["error"] is None
        else:
            assert report["data"] == {} and field not in report["data"]
            assert (report["error"] is None) is (kind == "skipped")
        assert "0" * 12 not in json.dumps(report)
        assert "email" not in json.dumps(report)
    summary = run_node(version, "Return Results", named={"Combine Results": combined})
    assert summary["results"] == {
        "ebas": expected_state,
        "startgg": expected_state,
        "attempt_id": "synthetic-attempt",
    }
    expected_boolean = (kind == "positive") if ok else None
    assert summary["member"] is expected_boolean
    assert summary["startgg"] is expected_boolean


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("run_startgg", [True, False])
def test_v5_keeps_integration_states_independent(published, run_startgg):
    version = flow_version(ORCHESTRATOR, published)
    combined = run_node(
        version,
        "Combine Results",
        named={
            "Parse Input": {
                "checkin_id": "synthetic-checkin",
                "attempt_id": "synthetic-attempt",
                "_runEbas": True,
                "_runStartgg": run_startgg,
            },
            "eBas Check": {"ok": True, "isMember": True},
            "Start.gg Check": {"error": "Synthetic timeout"},
        },
    )
    assert combined["_ebasState"] == "verified_true" and combined["_ebasData"] == {
        "member": True
    }
    assert combined["_startggState"] == ("unavailable" if run_startgg else "skipped")
    assert combined["_startggData"] == {} and combined["startgg"] is None


def test_v5_has_no_dead_startgg_email_handling():
    for version in versions(flow_version(ORCHESTRATOR)):
        for node in version["nodes"]:
            source = node.get("parameters", {}).get("jsCode", "") + node.get(
                "parameters", {}
            ).get("jsonBody", "")
            assert "sgg.email" not in source and "startgg_email" not in source

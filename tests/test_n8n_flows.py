"""Repo-only workflow hygiene and reviewed T-016/T-028 semantic invariants."""

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import httpx
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
        "invariant": "983b2bef3f296d2335e2b6a1654440be69f8aeb4d5dc6038a9aefdd9cf78504c",
        "callbacks": 4,
    },
    "FGC THN - eBas Membership Check.json": {
        "names": {
            "API Call",
            "Call eBas confirm_membership",
            "IF Skip eBas",
            "IF Verified Membership",
            "Normalize",
            "Normalize Personnummer",
            "Overview",
            "Parse Response",
            "Respond Membership",
            "Respond Unavailable",
            "Skip - Return Result",  # T-028: skipped/unavailable no longer means false.
            "Webhook - eBas Check",
        },
        "invariant": "22680732f524793dbd847a5bb8cf3c35c67c737955edfe68453671efe01ae173",
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
        "invariant": "b29be5796bb88c9d22e28ae8cff6a6a5fc3bf0805b84d3edbf28533a9b964f6f",
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
        "invariant": "a8c993072dcf5406726d48fcfb17923d5615dfa007940b33af69bba3bc8640c4",
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


def test_event_stats_backend_report_sends_the_private_callback_token():
    flow = json.loads(
        (ROOT / "n8n/flows/FGC THN - Start.gg Event Stats.json").read_text(encoding="utf-8")
    )
    for version in versions(flow):
        callbacks = [node for node in version["nodes"] if backend_call(node)]
        assert len(callbacks) == 1
        parameters = callbacks[0]["parameters"]
        assert parameters["method"] == "POST"
        assert parameters["url"] == "http://backend:8000/api/startgg/registered-count"
        assert parameters["sendHeaders"] is True
        token_headers = [
            header
            for header in parameters["headerParameters"]["parameters"]
            if header["name"].lower() == "x-internal-token"
        ]
        assert token_headers == [
            {"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_API_TOKEN }}"}
        ]


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
            if node.get("type") == "n8n-nodes-base.stickyNote" and node["name"] == "Normalize":
                content = PNR_PATTERN.sub("PNR_FORMAT_PLACEHOLDER", parameters.get("content", ""))
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
    # T-028/T-053/T-054 intentionally revise verification, registration and acknowledgement snapshots.
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
        len((ROOT / "docs" / "n8n-pnr-hygiene.md").read_text(encoding="utf-8").splitlines()) <= 30
    )


STARTGG = "FGC THN - Start.gg Check.json"
EBAS = "FGC THN - eBas Membership Check.json"
ORCHESTRATOR = "FGC THN - Checkin Orchestrator v5 (PG).json"


def flow_version(name, published=False):
    flow = json.loads((ROOT / "n8n" / "flows" / name).read_text(encoding="utf-8"))
    return flow["activeVersion"] if published else flow


def node_named(version, name):
    return next(node for node in version["nodes"] if node["name"] == name)


def run_node(version, name, current=None, named=None, env=None, *, expression=None):
    """Execute the exported Code node or body expression against synthetic n8n inputs."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable; static and mirrored Python checks still run")
    parameters = node_named(version, name)["parameters"]
    code = parameters.get("jsCode") if expression is None else None
    if code is None:
        expression = expression if expression is not None else parameters["jsonBody"]
        assert expression.startswith("={{") and expression.endswith("}}")
        code = "return (" + expression[3:-2].strip() + ");"
    program = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const lookup = name => {
  if (!(name in input.named)) throw new Error('Synthetic unavailable node');
  return { first: () => ({ json: input.named[name] }) };
};
const result = new Function('$json', '$', '$execution', '$env', input.code)(
  input.current, lookup, { id: 'synthetic-execution' }, input.env);
process.stdout.write(JSON.stringify(result));
"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps(
            {"code": code, "current": current or {}, "named": named or {}, "env": env or {}}
        ),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    if isinstance(value, str):
        return json.loads(value)
    return value[0]["json"] if isinstance(value, list) else value


@pytest.mark.parametrize("name", [STARTGG, EBAS, ORCHESTRATOR])
def test_exported_versions_have_identical_execution_graphs(name):
    draft = flow_version(name)
    published = flow_version(name, published=True)
    assert draft["nodes"] == published["nodes"]
    assert draft["connections"] == published["connections"]


@pytest.mark.parametrize("name", [STARTGG, EBAS, ORCHESTRATOR])
@pytest.mark.parametrize("published", [False, True])
def test_integration_http_requests_are_bounded_and_continue_after_errors(name, published):
    version = flow_version(name, published)
    requests = [node for node in version["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
    assert len(requests) == (4 if name == ORCHESTRATOR else 1)
    for node in requests:
        assert 0 < node["parameters"]["options"]["timeout"] <= 10000
        if name == EBAS:
            assert node["onError"] == "continueErrorOutput"
            assert version["connections"][node["name"]]["main"][1] == [
                {"node": "Respond Unavailable", "type": "main", "index": 0}
            ]
        else:
            assert (
                node.get("continueOnFail") is True or node.get("onError") == "continueRegularOutput"
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
    assert any(tag.strip().lower() == requested.strip().lower() for tag in candidates) is expected
    result = run_node(
        version,
        "Parse Response",
        t054_http(
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
            }
        ),
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
def test_startgg_errors_are_unavailable_without_registration_boolean(published, response):
    result = run_node(
        flow_version(STARTGG, published),
        "Parse Response",
        t054_http(response),
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


def ebas_webhook_response(version, provider_response, *, transport_error=False):
    """Run exported parsing/routing expressions and render the configured response.

    This is a repository contract harness, not an n8n runtime or provider call.
    """

    def target(name, output):
        links = version["connections"][name]["main"][output]
        assert len(links) == 1
        assert links[0]["type"] == "main" and links[0]["index"] == 0
        return links[0]["node"]

    if transport_error:
        responder = target("Call eBas confirm_membership", 1)
        result = {}
    else:
        assert target("Call eBas confirm_membership", 0) == "Parse Response"
        result = run_node(
            version,
            "Parse Response",
            provider_response,
            {"Normalize Personnummer": {"attempt_id": "synthetic-attempt"}},
        )
        gate = target("Parse Response", 0)
        conditions = node_named(version, gate)["parameters"]["conditions"]
        assert conditions["combinator"] == "and"
        assert len(conditions["conditions"]) == 1
        condition = conditions["conditions"][0]
        assert condition["rightValue"] is True
        assert condition["operator"] == {"type": "boolean", "operation": "equals"}
        verified = run_node(version, gate, result, expression=condition["leftValue"])
        assert isinstance(verified, bool)
        responder = target(gate, 0 if verified else 1)

    parameters = node_named(version, responder)["parameters"]
    options = parameters["options"]
    headers = {
        entry["name"]: entry["value"]
        for entry in options.get("responseHeaders", {}).get("entries", [])
    }
    if parameters["respondWith"] == "json":
        body = run_node(version, responder, result, expression=parameters["responseBody"])
        return httpx.Response(
            options["responseCode"],
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", **headers},
        )
    assert parameters["respondWith"] == "text"
    return httpx.Response(
        options["responseCode"], content=parameters["responseBody"].encode(), headers=headers
    )


@pytest.mark.parametrize("published", [False, True])
def test_ebas_webhook_has_explicit_private_success_and_error_routes(published):
    version = flow_version(EBAS, published)
    webhook = node_named(version, "Webhook - eBas Check")
    assert webhook["parameters"] == {
        "httpMethod": "POST",
        "path": "ebas/check",
        "responseMode": "responseNode",
        "options": {},
    }
    assert webhook["webhookId"] == "ebas-check"
    request = node_named(version, "Call eBas confirm_membership")
    assert request["parameters"]["options"]["timeout"] == 8000
    assert request.get("retryOnFail", False) is False
    assert request["parameters"]["options"]["redirect"] == {"redirect": {"followRedirects": False}}
    assert request["parameters"]["options"]["response"] == {
        "response": {"fullResponse": True, "neverError": False, "responseFormat": "json"}
    }
    # Every processing-node error goes to a fixed response, never provider data/PNR.
    for node in version["nodes"]:
        if node["type"] in {"n8n-nodes-base.stickyNote", "n8n-nodes-base.webhook"}:
            continue
        if node["name"] == "Respond Unavailable":
            assert node["parameters"] == {
                "respondWith": "text",
                "responseBody": "ebas_unavailable",
                "options": {
                    "responseCode": 503,
                    "responseHeaders": {
                        "entries": [{"name": "Content-Type", "value": "text/plain"}]
                    },
                },
            }
            continue
        assert node["onError"] == "continueErrorOutput"
        output = 2 if node["type"] == "n8n-nodes-base.if" else 1
        assert version["connections"][node["name"]]["main"][output] == [
            {"node": "Respond Unavailable", "type": "main", "index": 0}
        ]
    assert version["connections"]["Skip - Return Result"]["main"][0][0]["node"] == (
        "Respond Unavailable"
    )


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("member", [True, False])
@pytest.mark.parametrize("request_result", [None, "success"])
def test_ebas_success_keeps_the_exact_legacy_json(published, member, request_result):
    provider_response = {"response": {"member_found": member}}
    if request_result is not None:
        provider_response["response"]["request_result"] = request_result
    response = ebas_webhook_response(
        flow_version(EBAS, published),
        {"statusCode": 200, "headers": {}, "body": provider_response},
    )
    expected = {
        "ok": True,
        "state": "verified_true" if member else "verified_false",
        "isMember": member,
        "attempt_id": "synthetic-attempt",
        "message": "Medlem i Sverok" if member else "Ej medlem i Sverok",
    }
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == expected
    assert (
        response.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize(
    "provider_response",
    [
        {},
        {"response": {"member_found": "false"}},
        {"response": {"member_found": 0}},
        {"response": {"member_found": None}},
        {"response": {"member_found": False, "request_result": "fail"}},
        {"response": {"member_found": True, "request_result": "fail"}},
        {
            "statusCode": 200,
            "body": {"response": {"member_found": False, "request_result": "fail"}},
        },
        {"statusCode": 200, "body": {"response": {"member_found": False, "request_result": None}}},
        {"statusCode": 200, "body": {"request_result": False, "response": {"member_found": True}}},
        {"response": {"member_found": False}, "error": None},
        {"response": {"member_found": True}, "errors": []},
        {"response": {"member_found": False, "error": False}},
        {"response": {"member_found": False, "details": [{"errors": "Synthetic failure"}]}},
        {"statusCode": 200, "body": "Synthetic non-JSON body"},
        {"statusCode": 200, "body": None},
        {"statusCode": 200, "body": [{"response": {"member_found": False}}]},
        {"statusCode": 200, "body": {"response": {"member_found": False}, "errors": {}}},
        {"statusCode": 200, "body": {"response": {"member_found": True}}, "error": ""},
        {"statusCode": 201, "body": {"response": {"member_found": False, "errors": None}}},
        {"statusCode": 204, "body": {}},
        {"statusCode": 302, "body": {"response": {"member_found": False}}},
        {"statusCode": 401, "body": {"response": {"member_found": False}}},
        {"statusCode": 429, "body": {"response": {"member_found": True}}},
        {"statusCode": 500, "body": {"response": {"member_found": False}}},
        {"statusCode": "200", "body": {"response": {"member_found": False}}},
        {"body": {"response": {"member_found": False}}},
    ],
)
def test_ebas_ambiguous_and_failed_replies_are_non_json_503(published, provider_response):
    response = ebas_webhook_response(flow_version(EBAS, published), provider_response)
    assert response.status_code == 503
    assert response.headers["content-type"] == "text/plain"
    assert response.content == b"ebas_unavailable"
    with pytest.raises(ValueError):
        response.json()


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("outcome", ["member", "not_member", "timeout", "provider_error"])
def test_member_card_existing_json_decode_pattern_preserves_all_three_outcomes(published, outcome):
    version = flow_version(EBAS, published)
    provider_response = {"response": {"member_found": outcome == "member"}}
    if outcome == "provider_error":
        provider_response["response"]["request_result"] = "fail"
    response = ebas_webhook_response(
        version,
        {"statusCode": 200, "body": provider_response},
        transport_error=outcome == "timeout",
    )

    def handle(request):
        assert request.method == "POST" and request.url.path == "/webhook/ebas/check"
        assert json.loads(request.content) == {"personnummer": "0" * 12}
        return response

    async def caller():
        # Read-only sibling backend/main.py:78-97. Deliberately no raise_for_status().
        try:
            async with httpx.AsyncClient(
                timeout=15.0, transport=httpx.MockTransport(handle)
            ) as client:
                resp = await client.post(
                    "http://synthetic-n8n.invalid/webhook/ebas/check",
                    json={"personnummer": "0" * 12},
                )
                result = resp.json()
        except Exception:
            return 502, {
                "ok": False,
                "error": "Kunde inte verifiera medlemskap just nu. Försök igen.",
            }
        is_member = result.get("isMember", False)
        if not is_member:
            return 404, {
                "ok": False,
                "error": "Inget aktivt medlemskap hittades. Kontakta styrelsen.",
            }
        # The unchanged sibling continues with player/card processing here.
        return 200, {"ok": True}

    status, body = asyncio.run(caller())
    if outcome in {"timeout", "provider_error"}:
        assert status == 502
        assert body == {
            "ok": False,
            "error": "Kunde inte verifiera medlemskap just nu. Försök igen.",
        }
    elif outcome == "not_member":
        assert status == 404
        assert body == {
            "ok": False,
            "error": "Inget aktivt medlemskap hittades. Kontakta styrelsen.",
        }
    else:
        assert status == 200 and body == {"ok": True}


def test_ebas_export_has_a_fresh_consistent_published_version():
    flow = flow_version(EBAS)
    version_id = flow["versionId"]
    assert UUID(version_id).version == 4
    assert version_id != "259229f0-7ee2-4be3-8fa0-47a515ae138a"
    assert flow["activeVersionId"] == flow["activeVersion"]["versionId"] == version_id
    assert all(
        history["versionId"] == version_id
        for history in flow["activeVersion"].get("workflowPublishHistory", [])
    )


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("prefix, century", [("25", "20"), ("30", "20"), ("31", "19")])
def test_ebas_ten_and_twelve_digit_normalization_is_unchanged(published, prefix, century):
    version = flow_version(EBAS, published)
    short = prefix + "0" * 8
    long = century + short
    for raw in [short, short[:6] + "-" + short[6:], long]:
        result = run_node(version, "Normalize Personnummer", {"body": {"personnummer": raw}})
        assert result["skipEbas"] is False
        assert result["_pnr_normalized"] == long


@pytest.mark.parametrize("published", [False, True])
def test_missing_input_bypasses_external_requests_and_returns_skipped(published):
    startgg = flow_version(STARTGG, published)
    assert startgg["connections"]["Parse Input"]["main"][0][0]["node"] == "IF Skip Start.gg"
    assert startgg["connections"]["IF Skip Start.gg"]["main"][0][0]["node"] == "Parse Response"
    parsed = run_node(startgg, "Parse Input")
    result = run_node(startgg, "Parse Response", parsed, {"Parse Input": parsed})
    assert result["ok"] is False and result["state"] == "skipped" and "isRegistered" not in result
    ebas = flow_version(EBAS, published)
    assert ebas["connections"]["IF Skip eBas"]["main"][0][0]["node"] == "Skip - Return Result"
    parsed = run_node(ebas, "Normalize Personnummer")
    assert parsed["skipEbas"] is True
    result = run_node(ebas, "Skip - Return Result", parsed)
    assert result["ok"] is False and result["state"] == "skipped" and "isMember" not in result
    assert "personnummer" not in result and "_pnr_normalized" not in result
    valid_input = run_node(ebas, "Normalize Personnummer", {"body": {"personnummer": "0" * 12}})
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
                "state": "verified_true" if kind == "positive" else "verified_false",
                "events": ["Synthetic game"] if kind == "positive" else [],
            }
        elif kind == "timeout":
            payload = {"error": "Synthetic timeout " + "0" * 12, field: False}
        elif kind == "skipped":
            payload = {"ok": False, "state": "skipped", "skipped": True}
        else:
            payload = {}
        named[title + " Check"] = t054_http(payload)
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
        source_ok = ok and source == "startgg"
        source_state = expected_state if source == "startgg" else "skipped"
        assert report["source"] == source and report["ok"] is source_ok
        assert report["state"] == source_state
        assert report["attempt_id"] == "synthetic-attempt"
        assert report["checkin_id"] == "synthetic-checkin"
        if source_ok:
            assert report["data"][field] is (kind == "positive")
            assert report["error"] is None
        else:
            assert report["data"] == {} and field not in report["data"]
            assert (report["error"] is None) is (source_state == "skipped")
        assert "0" * 12 not in json.dumps(report)
        assert "email" not in json.dumps(report)
    summary = run_node(
        version,
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": t054_ack(combined, ok=ok, updated=ok),
        },
    )
    assert summary["results"] == {
        "ebas": "skipped",
        "startgg": expected_state,
        "attempt_id": "synthetic-attempt",
    }
    expected_boolean = (kind == "positive") if ok else None
    assert summary["member"] is None
    assert summary["startgg"] is expected_boolean
    assert summary["integration_complete"] is ok


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("run_startgg", [True, False])
def test_v5_keeps_startgg_independent_of_the_dead_ebas_branch(published, run_startgg):
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
            "eBas Check": t054_http(
                {"ok": True, "state": "verified_true", "isMember": True}
            ),
            "Start.gg Check": t054_http({"error": "Synthetic timeout"}),
        },
    )
    assert combined["_ebasState"] == "skipped" and combined["_ebasData"] == {}
    assert combined["_startggState"] == ("unavailable" if run_startgg else "skipped")
    assert combined["_startggData"] == {} and combined["startgg"] is None


def test_v5_has_no_dead_startgg_email_handling():
    for version in versions(flow_version(ORCHESTRATOR)):
        for node in version["nodes"]:
            source = node.get("parameters", {}).get("jsCode", "") + node.get("parameters", {}).get(
                "jsonBody", ""
            )
            assert "sgg.email" not in source and "startgg_email" not in source


REGISTER = "FGC THN - eBas Register v2 (PG).json"
REGISTER_CONTEXT = {
    "tag": "synthetic",
    "slug": "synthetic-event",
    "checkin_id": "synthetic-checkin",
}


def test_register_provider_submit_has_no_retry_or_redirect_and_bounded_full_text_response():
    version = flow_version(REGISTER)
    for name, timeout in (("Call eBas API", 75000), ("Report Member Status", 10000)):
        node = node_named(version, name)
        options = node["parameters"]["options"]
        assert options["timeout"] == timeout
        assert options["redirect"]["redirect"]["followRedirects"] is False
        assert options["response"]["response"] == {
            "neverError": True,
            "fullResponse": True,
            "responseFormat": "text",
        }
        assert node["onError"] == "continueRegularOutput"
        assert node["retryOnFail"] is False


@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            {"statusCode": 200, "body": {"stored_member": True, "request_result": "success"}},
            "accepted",
        ),
        (
            {"statusCode": 200, "body": '{"stored_member":true,"request_result":"success"}'},
            "accepted",
        ),
        (
            {
                "statusCode": 200,
                "body": {"request_result": "fail", "member_errors": {"email": ["Check email"]}},
            },
            "rejected",
        ),
        ({"statusCode": 200, "body": ""}, "outcome_unknown"),
        ({"statusCode": 200, "body": "not-json"}, "outcome_unknown"),
        ({"statusCode": 200, "body": []}, "outcome_unknown"),
        (
            {"statusCode": 200, "body": {"stored_member": "true", "request_result": "success"}},
            "outcome_unknown",
        ),
        (
            {
                "statusCode": 200,
                "body": {
                    "stored_member": True,
                    "request_result": "fail",
                    "member_errors": {"email": ["Conflict"]},
                },
            },
            "outcome_unknown",
        ),
        (
            {"statusCode": 200, "body": {"request_result": "fail", "member_errors": {}}},
            "outcome_unknown",
        ),
        (
            {
                "statusCode": 200,
                "body": {"request_result": "fail", "member_errors": {"email": [{"raw": "bad"}]}},
            },
            "outcome_unknown",
        ),
        (
            {"statusCode": 503, "body": {"stored_member": True, "request_result": "success"}},
            "outcome_unknown",
        ),
        (
            {
                "statusCode": 302,
                "body": {"request_result": "fail", "member_errors": {"email": ["Error"]}},
            },
            "outcome_unknown",
        ),
        ({"error": {"message": "synthetic timeout"}}, "outcome_unknown"),
    ],
)
def test_register_distinguishes_explicit_acceptance_rejection_and_unknown(reply, expected):
    result = run_node(
        flow_version(REGISTER), "Parse Response", reply, {"Normalize Input": REGISTER_CONTEXT}
    )
    assert result["state"] == expected
    assert result["checkin_id"] == REGISTER_CONTEXT["checkin_id"]
    assert result.get("retry_allowed", False) is (expected == "rejected")
    assert "raw_response" not in result and "member_errors" not in result
    if expected == "accepted":
        assert result["provider_accepted"] is True
    if expected == "rejected":
        assert result["verified_rejection"] is True and result["request_result"] == "fail"


def test_register_field_errors_are_limited_to_form_fields_and_personal_id_is_redacted():
    result = run_node(
        flow_version(REGISTER),
        "Parse Response",
        {
            "statusCode": 200,
            "body": {
                "request_result": "fail",
                "member_errors": {
                    "socialsecuritynumber": ["Invalid 000000000000"],
                    "email": ["Check email"],
                    "private_debug": ["do-not-publish"],
                },
                "raw_response": "do-not-publish",
            },
        },
        {"Normalize Input": REGISTER_CONTEXT},
    )
    assert result["field_errors"] == {
        "personnummer": ["Invalid [personal ID]"],
        "email": ["Check email"],
    }
    assert "do-not-publish" not in json.dumps(result)


def test_register_callback_sends_typed_truth_and_the_matching_checkin_id():
    body = run_node(flow_version(REGISTER), "Report Member Status", REGISTER_CONTEXT)
    assert body == {
        "member": True,
        "ok": True,
        "state": "verified_true",
        "checkin_id": "synthetic-checkin",
    }


@pytest.mark.parametrize(
    "ack,expected",
    [
        (
            {
                "statusCode": 200,
                "body": {
                    "success": True,
                    "member": True,
                    "checkin_id": "synthetic-checkin",
                    "status": "Pending",
                },
            },
            "registered",
        ),
        (
            {
                "statusCode": 201,
                "body": '{"success":true,"member":true,"checkin_id":"synthetic-checkin","status":"Ready"}',
            },
            "registered",
        ),
        (
            {
                "statusCode": 500,
                "body": {"success": True, "member": True, "checkin_id": "synthetic-checkin"},
            },
            "local_sync_pending",
        ),
        (
            {
                "statusCode": 200,
                "body": {"success": False, "member": True, "checkin_id": "synthetic-checkin"},
            },
            "local_sync_pending",
        ),
        (
            {
                "statusCode": 200,
                "body": {"success": True, "member": False, "checkin_id": "synthetic-checkin"},
            },
            "local_sync_pending",
        ),
        (
            {
                "statusCode": 200,
                "body": {"success": True, "member": "true", "checkin_id": "synthetic-checkin"},
            },
            "local_sync_pending",
        ),
        (
            {
                "statusCode": 200,
                "body": {"success": True, "member": True, "checkin_id": "another-checkin"},
            },
            "local_sync_pending",
        ),
        ({"statusCode": 200, "body": {}}, "local_sync_pending"),
        ({"statusCode": 200, "body": "not-json"}, "local_sync_pending"),
        ({"error": {"message": "synthetic callback timeout"}}, "local_sync_pending"),
    ],
)
def test_register_success_requires_canonical_callback_acknowledgement(ack, expected):
    result = run_node(
        flow_version(REGISTER),
        "Return Success",
        {"error": "SSE failure cannot forge an acknowledgement"},
        {
            "Parse Response": {**REGISTER_CONTEXT, "success": True, "provider_accepted": True},
            "Report Member Status": ack,
        },
    )
    assert result["state"] == expected
    assert result["success"] is (expected == "registered")
    assert result["provider_accepted"] is True and result["retry_allowed"] is False


def test_register_sse_notification_requests_refresh_without_asserting_unacknowledged_membership():
    body = run_node(
        flow_version(REGISTER), "Notify SSE", named={"Parse Response": REGISTER_CONTEXT}
    )
    assert body == {
        "event_type": "member_update",
        "tag": "synthetic",
        "checkin_id": "synthetic-checkin",
    }
    assert "member" not in body


def test_register_provider_body_uses_json_serialization_for_special_characters():
    context = {
        "pnr": "000000000000",
        "renewed": "2026-11-14",
        "email": 'synthetic"quote@example.invalid',
        "phone": "+46000000000",
        "nick": 'A"B\\C',
        "discord": None,
    }
    body = run_node(
        flow_version(REGISTER), "Call eBas API", context, env={"EBAS_API_KEY": "synthetic-only"}
    )
    assert body["api_key"] == "synthetic-only"
    assert body["member"]["email"] == context["email"]
    assert body["member"]["member_nick"] == context["nick"]


@pytest.mark.parametrize("malformed_flag", ["true", 1, None])
def test_register_malformed_storage_flag_cannot_turn_a_failure_into_retryable_rejection(
    malformed_flag,
):
    result = run_node(
        flow_version(REGISTER),
        "Parse Response",
        {
            "statusCode": 200,
            "body": {
                "stored_member": malformed_flag,
                "request_result": "fail",
                "member_errors": {"email": ["Check email"]},
            },
        },
        {"Normalize Input": REGISTER_CONTEXT},
    )
    assert result["state"] == "outcome_unknown"
    assert result["retry_allowed"] is False


# T-054: HTTP status, typed verdicts, optional games and canonical callback acknowledgements.
# Keep this section independent of the eBas-flow tests owned by T-048.
def t054_http(body, status=200):
    return {"statusCode": status, "body": body}


def t054_ack(combined, *, ok=True, updated=True):
    return t054_http(
        {
            "checkin_id": combined["checkin_id"],
            "source": "startgg",
            "success": updated is True,
            "ok": ok,
            "updated": updated,
            "state": combined["_startggState"],
        }
    )


def t054_combine(published, response):
    return run_node(
        flow_version(ORCHESTRATOR, published),
        "Combine Results",
        named={
            "Parse Input": {
                "checkin_id": "synthetic-checkin",
                "attempt_id": "synthetic-attempt",
                "_runEbas": True,
                "_runStartgg": True,
            },
            "Start.gg Check": response,
            "eBas Check": t054_http(
                {"ok": True, "state": "verified_true", "isMember": True}
            ),
        },
    )


def t054_verdict(found, **fields):
    return {
        "ok": True,
        "state": "verified_true" if found else "verified_false",
        "isRegistered": found,
        **fields,
    }


def t054_startgg(published, nodes, status=200):
    return run_node(
        flow_version(STARTGG, published),
        "Parse Response",
        t054_http({"data": {"tournament": {"participants": {"nodes": nodes}}}}, status),
        {"Parse Input": {"tag": "Ax", "attempt_id": "synthetic-attempt"}},
    )


@pytest.mark.parametrize("published", [False, True])
def test_t054_http_nodes_keep_status_without_retrying_provider_calls(published):
    for flow in (ORCHESTRATOR, STARTGG):
        version = flow_version(flow, published)
        for node in version["nodes"]:
            if node["type"] != "n8n-nodes-base.httpRequest":
                continue
            response = node["parameters"]["options"]["response"]["response"]
            assert response["fullResponse"] is response["neverError"] is True
            assert node.get("retryOnFail", False) is False
    version = flow_version(ORCHESTRATOR, published)
    assert version["connections"] == {
        source: {"main": [[{"node": target, "type": "main", "index": 0}]]}
        for source, target in (
            ("Webhook", "Parse Input"),
            ("Parse Input", "Start.gg Check"),
            ("Start.gg Check", "Combine Results"),
            ("Combine Results", "Report Start.gg Result"),
            ("Report Start.gg Result", "Return Results"),
        )
    }
    for name in (
        "eBas Check",
        "Report eBas Result",
        "Wait for Both",
        "Wait for Reports",
    ):
        assert node_named(version, name)["disabled"] is True
    parsed = run_node(
        version,
        "Parse Input",
        {
            "body": {
                "tag": "Ax",
                "slug": "synthetic-event",
                "personnummer": "0" * 12,
            }
        },
        {
            "Webhook": {
                "body": {
                    "tag": "Ax",
                    "slug": "synthetic-event",
                    "personnummer": "0" * 12,
                }
            }
        },
    )
    assert parsed["_runEbas"] is False and parsed["personnummer"] is None
    assert "0" * 12 not in json.dumps(parsed)


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("status", [401, 403, 429, 500])
@pytest.mark.parametrize("found", [True, False])
def test_t054_non_success_boolean_response_is_unavailable(published, status, found):
    combined = t054_combine(
        published, t054_http(t054_verdict(found, events=[]), status)
    )
    assert combined["_startggState"] == "unavailable"
    assert combined["_startggOk"] is False and combined["_startggData"] == {}
    assert combined["startgg"] is None and "events" not in combined


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("body", [
    {"isRegistered": False},
    {"ok": True, "isRegistered": False},
    {"ok": True, "state": "verified_true", "isRegistered": False},
    {"ok": True, "state": "processing", "isRegistered": False},
    {"ok": True, "state": "verified_false", "isRegistered": False, "events": ["Contradictory game"]},
    {"ok": True, "state": "verified_false", "isRegistered": False, "error": None},
    {"ok": True, "state": "verified_false", "isRegistered": False, "errors": []},
    {"ok": True, "state": "verified_false", "isRegistered": False, "errors": {"message": "0" * 12}},
    {"ok": True, "state": "verified_false", "isRegistered": False, "request_result": "error"},
    None, [], "false", "",
])
def test_t054_bare_or_error_envelopes_cannot_manufacture_a_verdict(published, body):
    combined = t054_combine(published, t054_http(body))
    assert combined["_startggState"] == "unavailable" and combined["_startggData"] == {}
    assert combined["startgg"] is None and "0" * 12 not in json.dumps(combined)


@pytest.mark.parametrize("published", [False, True])
def test_t054_a_typed_body_without_http_status_is_not_verified(published):
    combined = t054_combine(published, t054_verdict(False))
    assert combined["_startggState"] == "unavailable" and combined["_startggData"] == {}


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("failure", [
    "stale", "not-updated", "http", "transport", "malformed", "error-envelope",
    "error-result", "wrong-id", "wrong-source", "unexpected-state", "missing-state",
    "null-state", "wrong-verified-state", "missing-success", "null-success", "false-success", "missing",
])
def test_t054_failed_local_ack_is_never_reported_as_completed_verification(
    published, failure
):
    version = flow_version(ORCHESTRATOR, published)
    combined = t054_combine(
        published, t054_http(t054_verdict(True, events=["Synthetic game"]))
    )
    ack = t054_ack(combined)
    if failure == "stale":
        ack["body"].update(state="stale_rejected", updated=False, success=False)
    elif failure == "not-updated":
        ack["body"]["updated"] = False
    elif failure == "http":
        ack["statusCode"] = 500
    elif failure == "transport":
        ack = {"error": "Synthetic callback timeout"}
    elif failure == "malformed":
        ack["body"] = {"updated": True}
    elif failure == "error-envelope":
        ack["body"]["error"] = None
    elif failure == "error-result":
        ack["body"]["request_result"] = "error"
    elif failure == "wrong-id":
        ack["body"]["checkin_id"] = "different-synthetic-checkin"
    elif failure == "wrong-source":
        ack["body"]["source"] = "ebas"
    elif failure == "unexpected-state":
        ack["body"]["state"] = "processing"
    elif failure == "missing-state":
        ack["body"].pop("state")
    elif failure == "null-state":
        ack["body"]["state"] = None
    elif failure == "wrong-verified-state":
        ack["body"]["state"] = "verified_false"
    elif failure == "missing-success":
        ack["body"].pop("success")
    elif failure == "null-success":
        ack["body"]["success"] = None
    elif failure == "false-success":
        ack["body"]["success"] = False
    named = {"Combine Results": combined}
    if failure != "missing":
        named["Report Start.gg Result"] = ack
    result = run_node(version, "Return Results", named=named)
    expected = "stale" if failure == "stale" else "failed"
    assert result["local_sync"] == expected
    assert result["local_sync_sources"] == {"ebas": "skipped", "startgg": expected}
    assert result["integration_complete"] is False and result["startgg"] is None
    assert result["results"]["startgg"] == "unavailable" and "events" not in result
    assert result["member"] is None and result["results"]["ebas"] == "skipped"


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("found", [True, False])
def test_t054_successful_ack_preserves_an_explicit_positive_or_negative(
    published, found
):
    version = flow_version(ORCHESTRATOR, published)
    games = ["Synthetic game"] if found else []
    combined = t054_combine(published, t054_http(t054_verdict(found, events=games)))
    result = run_node(
        version,
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": t054_ack(combined),
            "Report eBas Result": t054_http(
                {"state": "stale_rejected", "updated": False}
            ),
        },
    )
    assert result["local_sync"] == "ok" and result["integration_complete"] is True
    assert result["startgg"] is found and result["events"] == games
    assert result["results"]["startgg"] == (
        "verified_true" if found else "verified_false"
    )
    assert result["member"] is None and result["results"]["ebas"] == "skipped"


@pytest.mark.parametrize("published", [False, True])
def test_t054_neutral_accepted_callback_is_not_a_verified_verdict(published):
    combined = t054_combine(published, t054_http({"ok": False, "state": "unavailable"}))
    result = run_node(
        flow_version(ORCHESTRATOR, published),
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": t054_ack(combined, ok=False, updated=False),
        },
    )
    assert result["local_sync"] == "ok" and result["integration_complete"] is False
    assert result["results"]["startgg"] == "unavailable" and result["startgg"] is None


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("nodes", [[None], [{}], [[]], [{"gamerTag": 1}],
                                   [{"gamerTag": ""}], [{"gamerTag": "  "}],
                                   [{"gamerTag": "Axel"}, None]])
def test_t054_malformed_participant_entries_are_unknown_not_negative(published, nodes):
    result = t054_startgg(published, nodes)
    assert result["ok"] is False and result["state"] == "unavailable"
    assert "isRegistered" not in result and "events" not in result


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("events", ["missing", None, {}, "invalid", [None], [{}],
                                    [{"name": ""}], [{"name": 1}],
                                    [{"name": "Synthetic game"}, None]])
def test_t054_matched_player_with_unknown_games_does_not_erase_stored_games(
    published, events
):
    participant = {"gamerTag": " aX "}
    if events != "missing":
        participant["events"] = events
    verdict = t054_startgg(published, [participant])
    assert verdict["ok"] is True and verdict["state"] == "verified_true"
    assert (
        verdict["isRegistered"] is True
        and "events" not in verdict
        and "eventCount" not in verdict
    )
    combined = t054_combine(published, t054_http(verdict))
    assert combined["_startggData"] == {"registered": True}
    payload = run_node(
        flow_version(ORCHESTRATOR, published),
        "Report Start.gg Result",
        named={"Combine Results": combined},
    )
    assert payload["data"] == {
        "registered": True
    }  # Canonical writer sees no authoritative games list.
    result = run_node(
        flow_version(ORCHESTRATOR, published),
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": t054_ack(combined),
        },
    )
    assert result["integration_complete"] is True and "events" not in result


@pytest.mark.parametrize("published", [False, True])
def test_t054_valid_empty_participant_list_is_a_verified_negative(published):
    result = t054_startgg(published, [])
    assert result["ok"] is True and result["state"] == "verified_false"
    assert result["isRegistered"] is False and result["events"] == []


@pytest.mark.parametrize("published", [False, True])
def test_t054_matched_player_with_valid_empty_events_retains_authoritative_empty(
    published,
):
    result = t054_startgg(published, [{"gamerTag": "Ax", "events": []}])
    assert result["state"] == "verified_true" and result["events"] == []
    combined = t054_combine(published, t054_http(result))
    assert combined["_startggData"] == {"registered": True, "events": []}


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("status", [429, 503])
def test_t054_startgg_http_failure_with_valid_empty_data_is_unknown(published, status):
    result = t054_startgg(published, [], status=status)
    assert result["state"] == "unavailable" and "isRegistered" not in result


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("user,expected", [
    ({"name": "Synthetic display name"}, "Synthetic display name"),
    (None, None), ({"name": {"invalid": "shape"}}, None), ("invalid", None),
])
def test_t054_existing_display_name_metadata_keeps_its_typed_shape(
    published, user, expected
):
    result = t054_startgg(published, [{"gamerTag": "Ax", "events": [], "user": user}])
    assert result["state"] == "verified_true" and result["name"] == expected
    assert result["events"] == []


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("acknowledgement,expected_sync", [
    ("accepted", "ok"), ("stale", "stale"), ("http-error", "failed"),
])
def test_t054_skipped_provider_still_checks_every_executed_callback(
    published, enabled, acknowledgement, expected_sync
):
    version = flow_version(ORCHESTRATOR, published)
    parsed = run_node(
        version,
        "Parse Input",
        named={
            "Webhook": {
                "body": {
                    "checkin_id": "synthetic-checkin",
                    "attempt_id": "synthetic-attempt",
                    "tag": "Ax",
                    **({"slug": "synthetic-event"} if enabled else {}),
                }
            }
        },
    )
    assert parsed["_runStartgg"] is enabled
    combined = run_node(
        version,
        "Combine Results",
        named={
            "Parse Input": parsed,
            "Start.gg Check": t054_http(
                {"ok": False, "state": "skipped", "skipped": True}
            ),
        },
    )
    assert combined["_startggState"] == "skipped" and combined["_startggData"] == {}
    ack = t054_ack(combined, ok=False, updated=False)
    if acknowledgement == "stale":
        ack["body"]["state"] = "stale_rejected"
    elif acknowledgement == "http-error":
        ack["statusCode"] = 500
    result = run_node(
        version,
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": ack,
        },
    )
    assert result["local_sync"] == expected_sync
    assert result["local_sync_sources"]["startgg"] == expected_sync
    assert result["integration_complete"] is False and result["startgg"] is None
    assert result["results"]["startgg"] == (
        "skipped" if expected_sync == "ok" else "unavailable"
    )


@pytest.mark.db
@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("state", ["verified_true", "verified_false", "unavailable", "skipped"])
def test_t054_real_backend_acknowledgement_contract(
    app_client, pg_db, published, state
):
    from shared import postgres_api as postgres

    row = pg_db.seed_checkin(
        member=True,
        startgg=True,
        is_guest=False,
        status="Ready",
        tournament_games_registered=["Existing synthetic game"],
    )
    attempt = postgres.start_integration_attempt(row["record_id"], sources=("startgg",))
    is_verdict = state in ("verified_true", "verified_false")
    found = state == "verified_true"
    verdict = (
        t054_verdict(found, events=["New synthetic game"] if found else [])
        if is_verdict
        else {
            "ok": False,
            "state": state,
            "skipped": state == "skipped",
        }
    )
    version = flow_version(ORCHESTRATOR, published)
    combined = t054_combine(published, t054_http(verdict))
    combined.update(checkin_id=row["record_id"], attempt_id=attempt)
    with pg_db.pool.connection() as connection:
        combined["fetched_at"] = (
            connection.execute("SELECT clock_timestamp()").fetchone()[0].isoformat()
        )
    payload = run_node(
        version, "Report Start.gg Result", named={"Combine Results": combined}
    )
    response = app_client.post("/api/integration/result", json=payload)
    assert response.status_code == 200
    acknowledgement = response.json()
    assert acknowledgement["success"] is acknowledgement["updated"] is is_verdict
    assert acknowledgement["ok"] is is_verdict and acknowledgement["state"] == state
    result = run_node(
        version,
        "Return Results",
        named={
            "Combine Results": combined,
            "Report Start.gg Result": t054_http(acknowledgement, response.status_code),
        },
    )
    assert result["local_sync"] == "ok"
    assert (
        result["integration_complete"] is is_verdict
        and result["results"]["startgg"] == state
    )
    stored = pg_db.checkins()[0]
    if is_verdict:
        assert result["startgg"] is found and stored["startgg"] is found
        assert stored["tournament_games_registered"] == (
            ["New synthetic game"] if found else []
        )
    else:
        assert result["startgg"] is None and "events" not in result
        for key in (
            "member",
            "startgg",
            "is_guest",
            "status",
            "tournament_games_registered",
        ):
            assert stored[key] == row[key]

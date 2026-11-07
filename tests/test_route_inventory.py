"""Compare the base route contract and matching precedence after the split."""

import importlib
import json
import re
from itertools import combinations
from pathlib import Path

from starlette.routing import Match, compile_path

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "route_inventory.json"


def route_contexts(app):
    routes = []
    for route in app.routes:
        if hasattr(route, "effective_route_contexts"):
            routes.extend(
                (context.original_route, context) for context in route.effective_route_contexts()
            )
        else:
            routes.append((route, route))
    return routes


def route_inventory(app):
    inventory = []
    routes = route_contexts(app)
    for route, context in routes:
        response_class = (
            getattr(context, "response_class", None) if hasattr(route, "dependant") else None
        )
        response_class = getattr(response_class, "value", response_class)
        dependency = getattr(context, "dependant", None)
        inventory.append(
            [
                getattr(context, "path", None) or route.path,
                sorted(getattr(route, "methods", []) or []),
                getattr(getattr(route, "endpoint", None), "__name__", None),
                [
                    getattr(item.call, "__name__", type(item.call).__name__)
                    for item in getattr(dependency, "dependencies", [])
                ],
                (
                    f"{response_class.__module__}.{response_class.__qualname__}"
                    if response_class is not None
                    else None
                ),
            ]
        )
    return inventory


def test_route_inventory_matches_the_base_commit(backend_app):
    # MSG-C-164 permits area grouping; the original ordered snapshot stays unchanged.
    baseline = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(route_inventory(backend_app.app)) == sorted(baseline)


def overlapping_path_pairs(paths):
    """Find every intersection for the app's literal/whole-segment str patterns."""
    parameter = re.compile(r"\{[^}:]+\}")
    for path in paths:
        # Fail visibly if a future route needs more general converter intersection logic.
        for segment in path.split("/"):
            assert "{" not in segment or parameter.fullmatch(segment), path
    overlaps = []
    for first, second in combinations(paths, 2):
        left, right = first.split("/"), second.split("/")
        if len(left) != len(right):
            continue
        candidate = []
        for a, b in zip(left, right):
            if parameter.fullmatch(a):
                candidate.append("test-route" if parameter.fullmatch(b) else b)
            elif parameter.fullmatch(b) or a == b:
                candidate.append(a)
            else:
                break
        else:
            witness = "/".join(candidate)
            assert compile_path(first)[0].fullmatch(witness)
            assert compile_path(second)[0].fullmatch(witness)
            overlaps.append((first, second, witness))
    return overlaps


def test_shadow_pair_generator_finds_static_dynamic_and_crossed_parameters():
    paths = ["/status/{name}", "/status/fixed", "/{area}/fixed", "/register", "/register.html"]
    assert overlapping_path_pairs(paths) == [
        ("/status/{name}", "/status/fixed", "/status/fixed"),
        ("/status/{name}", "/{area}/fixed", "/status/fixed"),
        ("/status/fixed", "/{area}/fixed", "/status/fixed"),
    ]


def test_every_route_and_potential_shadow_pair_keeps_base_precedence(backend_app):
    baseline = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    paths = [row[0] for row in baseline if row[1]]
    overlaps = overlapping_path_pairs(paths)
    candidates = {re.sub(r"\{[^}]+\}", "test-route", path) for path in paths}
    candidates.update(witness for _, _, witness in overlaps)
    candidates.update(
        {
            "/register",
            "/register.html",
            "/status/register",
            "/status/register.html",
            "/api/checkin/orchestrate",
            "/api/checkin/begin",
            "/api/checkin/orchestrate/status",
            "/api/checkin/begin/status",
            "/api/checkin/orchestrate/member-status",
            "/api/checkin/begin/member-status",
            "/api/membership/start",
            "/api/membership/verify",
            "/api/membership/resend",
            "/api/membership/start/status",
            "/api/membership/verify/status",
            "/api/membership/resend/status",
        }
    )
    for path in sorted(candidates):
        for method in ("GET", "POST", "PATCH", "DELETE", "HEAD"):
            expected = next(
                (
                    row[2]
                    for row in baseline
                    if method in row[1] and compile_path(row[0])[0].fullmatch(path)
                ),
                None,
            )
            scope = {"type": "http", "path": path, "root_path": "", "method": method}
            actual = next(
                (
                    route.endpoint.__name__
                    for route, _ in route_contexts(backend_app.app)
                    if getattr(route, "methods", None) and route.matches(scope)[0] == Match.FULL
                ),
                None,
            )
            assert actual == expected, (method, path, overlaps)


def test_router_imports_share_single_instances(monkeypatch):
    monkeypatch.chdir(SNAPSHOT.parents[2] / "backend")
    backend = importlib.import_module("backend.main")
    state = backend.app_state
    modules = [
        backend,
        backend.pages,
        backend.checkin,
        backend.admin,
        backend.integrations,
        backend.ebas_legacy,
        backend.sse,
        backend.system,
    ]
    for name in ("sse_manager", "templates", "httpx_client", "SESSION", "STARTGG_CACHE"):
        for module in modules:
            if hasattr(module, name):
                assert getattr(module, name) is getattr(state, name), (module.__name__, name)

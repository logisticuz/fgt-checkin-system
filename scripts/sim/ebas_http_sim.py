"""Private DEV-network eBas/Start.gg stand-ins for the T-074 rehearsal."""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

if Path(__file__).resolve().parents[1].name == "scripts":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.ebas_client import _production_environment
from shared.ebas_stub_scenario import EbasStubScenario

DUMMY_KEY = "sim-dummy"
GAMES = ("SSBU", "SF6", "T8", "Strive")
GUESTS = frozenset({"member-02", "member-04", "register-02", "slow-confirm"})


def synthetic_people(settings):
    if settings.get("synthetic") is not True or not isinstance(settings.get("people"), dict):
        raise ValueError("A synthetic rehearsal scenario is required")
    people = settings["people"]
    if not people or not all(
        isinstance(pnr, str)
        and len(pnr) == 12
        and pnr.isascii()
        and pnr.isdigit()
        and isinstance(person, dict)
        and isinstance(person.get("key"), str)
        for pnr, person in people.items()
    ):
        raise ValueError("Invalid synthetic rehearsal identities")
    return people


def person_tags(settings, prefix):
    return {
        person["key"]: prefix + f"{index:02d}"
        for index, person in enumerate(synthetic_people(settings).values(), 1)
    }


class Simulation:
    def __init__(self, service, settings, prefix, *, timeout=15):
        if _production_environment() or service not in {"ebas", "startgg"}:
            raise ValueError("Only a non-production rehearsal simulator is allowed")
        self.service = service
        self.people = synthetic_people(settings)
        self.scenario = EbasStubScenario(settings)
        self.prefix = prefix
        self.roles_by_tag = {tag: role for role, tag in person_tags(settings, prefix).items()}
        self.timeout = timeout

    def respond(self, path, payload):
        if self.service == "startgg":
            return self._startgg(payload)
        try:
            if path.endswith("confirm_membership.json"):
                member = payload["request"]
            elif path.endswith("submit_member_with_lookup.json"):
                member = payload["member"]
            elif path.endswith("validate_members.json"):
                member = payload["request"]["data"]["Member"][0]
            else:
                return httpx.Response(404, json={"error": "unknown_action"})
            pnr = member["socialsecuritynumber"]
            key = payload.get("api_key", payload.get("request", {}).get("api_key"))
            if pnr not in self.people or key != DUMMY_KEY:
                return httpx.Response(403, json={"error": "synthetic_only"})
        except (KeyError, TypeError, IndexError):
            return httpx.Response(400, json={"error": "invalid_synthetic_request"})
        return self.scenario.handle_request(
            httpx.Request(
                "POST",
                "http://ebas-sim" + path,
                json=payload,
                extensions={"timeout": {"read": self.timeout}},
            )
        )

    def _startgg(self, payload):
        variables = payload.get("variables", {})
        tag = variables.get("tag", "")
        slug = variables.get("slug", "")
        if (
            not isinstance(tag, str)
            or not tag.startswith(self.prefix)
            or not slug.startswith("t074-fightbox-")
        ):
            return httpx.Response(400, json={"errors": [{"message": "synthetic_only"}]})
        role = self.roles_by_tag.get(tag.removesuffix("x"))
        if role is None:
            return httpx.Response(400, json={"errors": [{"message": "unknown_synthetic_player"}]})
        offset = sum(map(ord, role)) % len(GAMES)
        events = [
            {"id": 990001 + index, "name": game, "videogame": {"id": index + 1, "name": game}}
            for index, game in enumerate((GAMES[offset], GAMES[(offset + 1) % len(GAMES)]))
        ]
        nodes = (
            []
            if role in GUESTS
            else [
                {
                    "id": 990100,
                    "gamerTag": tag,
                    "email": tag + "@example.invalid",
                    "user": {"name": "Synthetic " + role},
                    "events": events,
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "tournament": {
                        "id": 990000,
                        "name": "Synthetic FightBox rehearsal",
                        "participants": {"nodes": nodes},
                    }
                }
            },
        )


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Never print request/provider exception data, including malformed input.
        pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._send(
            httpx.Response(200, json={"synthetic": True, "service": self.server.sim.service})
        )

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            response = self.server.sim.respond(self.path, payload)
        except httpx.TimeoutException:
            self.close_connection = True
            return
        except Exception:
            response = httpx.Response(400, json={"error": "invalid_synthetic_request"})
        self._send(response)

    def _send(self, response):
        try:
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", response.headers.get("content-type", "application/json")
            )
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        except (BrokenPipeError, ConnectionResetError):
            pass


def install_latency_trace(settings, destination):
    """Measure the unchanged scenario sleep, retaining only role/action/timing data."""
    from shared import ebas_stub_scenario as engine

    if _production_environment() or os.getenv("EBAS_TRANSPORT") != "stub":
        raise ValueError("Latency tracing requires the non-production stub")
    people = synthetic_people(settings)
    original_handle = engine.EbasStubScenario.handle_request
    original_sleep = engine.sleep
    local = threading.local()
    lock = threading.Lock()
    destination = Path(destination)
    destination.write_text("", encoding="utf-8")

    def traced_sleep(seconds):
        entry = getattr(local, "entry", None)
        if entry is not None:
            entry["injected_ms"] += seconds * 1000
        return original_sleep(seconds)

    def traced_handle(self, request):
        payload = json.loads(request.content)
        action = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if action == "confirm_membership":
            pnr = payload.get("request", {}).get("socialsecuritynumber")
        elif action == "submit_member_with_lookup":
            pnr = payload.get("member", {}).get("socialsecuritynumber")
        else:
            pnr = (
                payload.get("request", {})
                .get("data", {})
                .get("Member", [{}])[0]
                .get("socialsecuritynumber")
            )
        entry = {
            "role": people.get(pnr, {}).get("key", "unmapped"),
            "action": action,
            "injected_ms": 0.0,
            "timeout": False,
        }
        pnr = None
        previous = getattr(local, "entry", None)
        local.entry = entry
        started = time.monotonic()
        try:
            response = original_handle(self, request)
            entry["http_status"] = response.status_code
            return response
        except httpx.TimeoutException:
            entry["timeout"] = True
            raise
        finally:
            entry["transport_ms"] = round((time.monotonic() - started) * 1000, 3)
            entry["injected_ms"] = round(entry["injected_ms"], 3)
            local.entry = previous
            with lock, destination.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry) + "\n")

    engine.EbasStubScenario.handle_request = traced_handle
    engine.sleep = traced_sleep

    def restore():
        engine.EbasStubScenario.handle_request = original_handle
        engine.sleep = original_sleep

    return restore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("ebas", "startgg", "backend"), required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--prefix", default="t074-")
    parser.add_argument("--port", type=int, default=8080)
    options = parser.parse_args(argv)
    if _production_environment():
        parser.error("Production is refused")
    settings = json.loads(options.scenario.read_text(encoding="utf-8"))
    if options.service == "backend":
        import uvicorn

        install_latency_trace(settings, "/tmp/t074-latency.roles.jsonl")
        uvicorn.run("main:app", host="0.0.0.0", port=options.port)
        return
    server = _Server(("0.0.0.0", options.port), _Handler)
    server.sim = Simulation(options.service, settings, options.prefix)
    server.serve_forever()


if __name__ == "__main__":
    main()

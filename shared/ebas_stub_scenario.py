"""Synthetic, local eBas rehearsal responses; never contact or log a provider."""

import json
import math
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import sleep

import httpx

_ACTIONS = {
    "confirm_membership.json": "confirm",
    "validate_members.json": "validate",
    "submit_member_with_lookup.json": "submit",
}
_OUTCOMES = frozenset({"member", "not_member", "error_500", "non_json_503", "timeout"})
_LOAD_LOCK = Lock()
_INVALID = "Invalid synthetic eBas stub scenario"


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(_INVALID)
    return value


@dataclass(frozen=True)
class _Rule:
    outcome: str
    delay_ms: float = 0


def _rule(value):
    if isinstance(value, str) and (slow := re.fullmatch(r"slow\(([0-9]+(?:\.[0-9]+)?)\)", value)):
        return _Rule("member", _number(float(slow[1])))
    if isinstance(value, str):
        value = {"outcome": value}
    if not isinstance(value, dict) or set(value) - {"outcome", "delay_ms"}:
        raise ValueError(_INVALID)
    outcome = value.get("outcome", "member")
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise ValueError(_INVALID)
    return _Rule(outcome, _number(value.get("delay_ms", 0)))


class EbasStubScenario:
    """One seeded request sequence shared by clients using the same scenario file.

    Latency has a piecewise uniform CDF through p50 and p95, with a tail up to
    twice p95. Delays add to per-person delays; timeouts are sampled independently.
    Reproducibility assumes the same request order. No request bodies are retained.
    """

    def __init__(self, config):
        if (
            not isinstance(config, dict)
            or config.get("version") != 1
            or config.get("synthetic") is not True
        ):
            raise ValueError(_INVALID)
        seed = config.get("seed", 0)
        if type(seed) is not int:
            raise ValueError(_INVALID)
        self._rng = random.Random(seed)
        self._lock = Lock()
        defaults = config.get("defaults", {})
        people = config.get("people", {})
        latency = config.get("latency", {})
        if not all(isinstance(value, dict) for value in (defaults, people, latency)):
            raise ValueError(_INVALID)
        if set(defaults) - set(_ACTIONS.values()) or set(latency) - {
            "p50_ms",
            "p95_ms",
            "timeout_rate",
        }:
            raise ValueError(_INVALID)
        self._defaults = {
            action: _rule(defaults.get(action, "member")) for action in _ACTIONS.values()
        }
        self._people = {}
        for pnr, rules in people.items():
            if not isinstance(pnr, str) or re.fullmatch(r"[0-9]{12}", pnr) is None:
                raise ValueError(_INVALID)
            if not isinstance(rules, dict) or set(rules) - {"key", *_ACTIONS.values()}:
                raise ValueError(_INVALID)
            self._people[pnr] = {
                action: _rule(rules[action]) for action in _ACTIONS.values() if action in rules
            }
        self._p50 = _number(latency.get("p50_ms", 0))
        self._p95 = _number(latency.get("p95_ms", self._p50))
        self._timeout_rate = _number(latency.get("timeout_rate", 0))
        if self._p95 < self._p50 or self._timeout_rate > 1:
            raise ValueError(_INVALID)

    def _latency(self):
        with self._lock:
            quantile = self._rng.random()
            timeout = self._rng.random() < self._timeout_rate
        if quantile <= 0.5:
            delay = quantile * 2 * self._p50
        elif quantile <= 0.95:
            delay = self._p50 + (quantile - 0.5) / 0.45 * (self._p95 - self._p50)
        else:
            delay = self._p95 * (1 + (quantile - 0.95) / 0.05)
        return delay / 1000, timeout

    def handle_request(self, request):
        action = _ACTIONS.get(request.url.path.rsplit("/", 1)[-1])
        if action is None:
            return httpx.Response(404, json={}, request=request)
        try:
            payload = json.loads(request.content)
            if action == "confirm":
                pnr = payload["request"]["socialsecuritynumber"]
            elif action == "validate":
                pnr = payload["request"]["data"]["Member"][0]["socialsecuritynumber"]
            else:
                pnr = payload["member"]["socialsecuritynumber"]
            if not isinstance(pnr, str):
                raise ValueError
        except (ValueError, KeyError, TypeError, IndexError):
            return httpx.Response(400, json={"error": "invalid_synthetic_request"}, request=request)
        rule = self._people.get(pnr, {}).get(action, self._defaults[action])
        delay, sampled_timeout = self._latency()
        delay += rule.delay_ms / 1000
        deadline = request.extensions.get("timeout", {}).get("read")
        if (
            rule.outcome == "timeout"
            or sampled_timeout
            or (deadline is not None and delay >= deadline)
        ):
            if deadline is not None and deadline > 0:
                sleep(deadline)
            raise httpx.ReadTimeout("Synthetic eBas read timeout", request=request)
        if delay > 0:
            sleep(delay)
        if rule.outcome == "error_500":
            return httpx.Response(500, json={"error": "synthetic_unavailable"}, request=request)
        if rule.outcome == "non_json_503":
            return httpx.Response(503, text="Synthetic service unavailable", request=request)
        member = rule.outcome == "member"
        if action == "confirm":
            data = {"response": {"member_found": member}}
        elif action == "validate":
            data = {
                "response": {
                    "request_result": "success" if member else "fail",
                    "member_errors": (
                        [] if member else {"socialsecuritynumber": ["Synthetic rejection"]}
                    ),
                }
            }
        elif member:
            data = {"stored_member": True, "request_result": "success"}
        else:
            data = {
                "request_result": "fail",
                "member_errors": {"socialsecuritynumber": ["Synthetic rejection"]},
            }
        return httpx.Response(200, json=data, request=request)


@lru_cache(maxsize=1)
def _load_scenario(path, modified_ns, size):
    # File metadata invalidates the cache; all factory-created clients share its RNG.
    return EbasStubScenario(json.loads(Path(path).read_text(encoding="utf-8")))


def load_scenario(path):
    """Read only explicitly synthetic JSON and report configuration errors without data."""
    try:
        source = Path(path).resolve()
        metadata = source.stat()
        with _LOAD_LOCK:
            return _load_scenario(str(source), metadata.st_mtime_ns, metadata.st_size)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError(_INVALID) from None

"""Slow dashboard storage runs outside the event loop, and outages stay private."""

import ast
import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.test_auth_callback_and_docs import load_application


def test_ten_slow_session_requests_run_concurrently(load_application, monkeypatch):
    module, _ = load_application("dashboard")
    called_threads = []

    def slow_session(_session_id):
        called_threads.append(threading.get_ident())
        time.sleep(0.5)
        return {"user_id": "synthetic-user", "user_name": "Test TO"}

    monkeypatch.setattr(module, "get_session", slow_session)

    async def requests():
        event_loop_thread = threading.get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app),
            base_url="http://testserver",
            cookies={"fgc_session": "synthetic-session"},
        ) as client:
            started = time.perf_counter()
            responses = await asyncio.gather(*(client.get("/auth/me") for _ in range(10)))
            elapsed = time.perf_counter() - started
        return event_loop_thread, elapsed, responses

    loop_thread, elapsed, responses = asyncio.run(requests())
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["user_id"] == "synthetic-user" for response in responses)
    assert len(called_threads) == 20  # Middleware lookup plus handler lookup for each request.
    assert all(thread != loop_thread for thread in called_threads)
    assert elapsed < 3.0, f"Ten requests with two 0.5 s lookups each took {elapsed:.2f} s"


@pytest.mark.parametrize("path", ["/", "/api/probe"])
@pytest.mark.parametrize(
    "error_class", [psycopg.OperationalError, psycopg.InterfaceError, ConnectionError, TimeoutError]
)
def test_database_outage_has_safe_retryable_page_or_json(
    load_application, monkeypatch, caplog, path, error_class
):
    module, _ = load_application("dashboard")
    secret = "synthetic-private-database-error"
    monkeypatch.setattr(module, "get_session", Mock(side_effect=error_class(secret)))
    with (
        caplog.at_level(logging.ERROR),
        TestClient(module.app, cookies={"fgc_session": "synthetic-session"}) as client,
    ):
        response = client.get(path, follow_redirects=False)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"
    assert secret not in response.text
    if path.startswith("/api/"):
        assert response.headers["content-type"].startswith("application/json")
        reference = response.json()["log_id"]
    else:
        assert response.headers["content-type"].startswith("text/html")
        assert "Retrying in 10 s" in response.text
        assert "http-equiv='refresh' content='10'" in response.text
        reference = re.search(r"Reference: ([0-9a-f]{8})", response.text).group(1)
    assert re.fullmatch(r"[0-9a-f]{8}", reference)
    records = [record for record in caplog.records if record.name == module.logger.name]
    assert len(records) == 1
    assert reference in records[0].getMessage()
    assert secret not in caplog.text


def test_session_activity_failure_uses_the_same_database_response(load_application, monkeypatch):
    module, _ = load_application("dashboard")
    monkeypatch.setattr(
        module, "update_session_activity", Mock(side_effect=psycopg.OperationalError("synthetic"))
    )
    with TestClient(module.app, cookies={"fgc_session": "synthetic-session"}) as client:
        response = client.get("/auth/me", follow_redirects=False)
    assert response.status_code == 503
    assert "Retrying in 10 s" in response.text


def test_async_functions_do_not_call_synchronous_io_directly(load_application):
    module, _ = load_application("dashboard")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    blocking_names = {
        "get_session",
        "update_session_activity",
        "cleanup_expired_sessions",
        "create_session",
        "delete_session",
        "get_active_slug",
        "get_all_event_slugs",
        "get_active_settings_with_id",
        "update_settings",
        "log_action",
        "reopen_event",
        "_audit_safe",
        "exchange_code_for_token",
        "get_startgg_user",
        "is_event_admin",
        "check_event_admin",
    }
    direct = [
        (function.name, node.func.id, node.lineno)
        for function in tree.body
        if isinstance(function, ast.AsyncFunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in blocking_names
    ]
    assert direct == []

"""Isolate the real application from credentials, provider networks, and shared data."""

import asyncio
import importlib
import os
import smtplib
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import aiohttp
import httpx
import psycopg
import pytest
import requests
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg_pool import ConnectionPool

from tests.harness import DEFAULT_TEST_DATABASE_URL, TestDatabase, validate_test_database_url
from tests.stubs import EbasStub, N8NStub, SMTPStub, StartGGStub

ROOT = Path(__file__).resolve().parents[1]
_environment = None


def pytest_configure(config):
    global _environment
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    try:
        validate_test_database_url(url, os.environ.get("TEST_DATABASE_ALLOW_ANY") == "1")
    except ValueError as error:
        pytest.exit(str(error), returncode=4)

    _environment = os.environ.copy()
    provider_prefixes = (
        "AIRTABLE_",
        "STARTGG_",
        "EBAS_",
        "N8N_",
        "DISCORD_",
        "SMTP_",
        "STRIPE_",
        "PG",
    )
    for name in list(os.environ):
        if name.startswith(provider_prefixes):
            os.environ.pop(name)
    os.environ.update(
        DATABASE_URL=url,
        DATA_BACKEND="postgres",
        INTEGRATION_ENGINE="none",
        VERIFICATION_SENDER="console",
        N8N_INTERNAL_URL="http://n8n.test.invalid",
        OAUTH_ADMIN_KEY="synthetic-test-key",
        ADMIN_AUTH_COOKIE_TOKEN="synthetic-admin-token",
        SSE_TOKEN="synthetic-sse-token",
    )
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "backend"))


def pytest_unconfigure(config):
    if _environment is not None:
        os.environ.clear()
        os.environ.update(_environment)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Block actual HTTP transports while preserving TestClient and MockTransport."""

    def blocked(*args, **kwargs):
        raise AssertionError("Real outbound network is blocked; install an explicit test stub")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
    monkeypatch.setattr(requests.sessions.Session, "send", blocked)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", async_blocked)
    monkeypatch.setattr(smtplib.SMTP, "connect", blocked)
    monkeypatch.setattr(smtplib.SMTP_SSL, "connect", blocked)


@pytest.fixture(scope="session")
def _pg_session():
    url = validate_test_database_url(
        os.environ["DATABASE_URL"], os.environ.get("TEST_DATABASE_ALLOW_ANY") == "1"
    )
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("test Postgres unreachable")

    schema = "fgt_test_" + uuid.uuid4().hex
    pool = None
    with admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            pool = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=4,
                open=False,
                timeout=3,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 2,
                    "options": f"-csearch_path={schema},public",
                },
            )
            pool.open(wait=True, timeout=3)
            with pool.connection() as connection:
                with connection.transaction():
                    connection.execute((ROOT / "db" / "init.sql").read_text(encoding="utf-8"))

            postgres = importlib.import_module("shared.postgres_api")
            with patch.object(postgres.logger, "warning") as warnings:
                postgres._run_migrations(pool)
            if warnings.called:
                pytest.fail("Application schema migrations emitted a warning")

            from shared.migrate import apply

            apply(pool, migrations_dir=ROOT / "shared" / "migrations")

            with pytest.MonkeyPatch.context() as isolation:
                isolation.setattr(postgres, "DATABASE_URL", url)
                isolation.setattr(postgres, "_pool", pool)
                yield TestDatabase(schema, pool)
        finally:
            if pool is not None:
                pool.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def pg_db(_pg_session):
    """One private schema per session, empty tables before each requesting test."""
    with _pg_session.pool.connection() as connection:
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (_pg_session.schema,)
        ).fetchall()
        if tables:
            connection.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(_pg_session.schema, row[0]) for row in tables)
                )
            )
    return _pg_session


@pytest.fixture
def backend_app(monkeypatch, block_network, n8n_stub):
    # main.py resolves its static/template directories relative to the working directory.
    monkeypatch.chdir(ROOT / "backend")
    backend = importlib.import_module("backend.main")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(n8n_stub.handle_async))
    modules = (
        backend,
        backend.app_state,
        backend.admin,
        backend.checkin,
        backend.ebas_legacy,
        backend.integrations,
        backend.pages,
        backend.sse,
        backend.system,
    )
    for module in modules:
        if hasattr(module, "INTEGRATION_ENGINE"):
            monkeypatch.setattr(module, "INTEGRATION_ENGINE", "none")
        if hasattr(module, "httpx_client"):
            monkeypatch.setattr(module, "httpx_client", upstream)
    try:
        yield backend
    finally:
        asyncio.run(upstream.aclose())


@pytest.fixture
def n8n_stub():
    return N8NStub()


@pytest.fixture
def ebas_stub():
    return EbasStub()


@pytest.fixture
def startgg_stub():
    return StartGGStub()


@pytest.fixture
def smtp_stub(monkeypatch):
    stub = SMTPStub()
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: stub)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: stub)
    return stub


@pytest.fixture
def handler_client(backend_app):
    """Real handlers for routes that do not require a database (for example /health)."""
    with TestClient(backend_app.app) as client:
        yield client


@pytest.fixture
def app_client(pg_db, handler_client):
    pg_db.seed_settings()
    # Orchestrate currently calls n8n even with INTEGRATION_ENGINE=none; backend_app stubs it.
    yield handler_client

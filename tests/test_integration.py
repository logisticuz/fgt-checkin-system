"""Exercise the test infrastructure's safety boundaries and explicit provider stubs."""

import asyncio
import smtplib
from email.message import EmailMessage

import aiohttp
import httpx
import pytest
import requests

from tests.harness import DEFAULT_TEST_DATABASE_URL, validate_test_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://x@127.0.0.1:5432/fgt_checkin",
        "postgresql://x@database.example.invalid:15432/fgt_test",
        "postgresql://x@localhost/fgt_test",
        "postgresql://x@127.0.0.1:15432/fgt_test?host=database.example.invalid",
        "postgresql://x@localhost:15432/fgt_test?hostaddr=192.0.2.1",
        "postgresql://x@localhost:15432/fgt_test?service=shared-database",
    ],
)
def test_database_guard_rejects_shared_or_indirect_destinations(url):
    with pytest.raises(ValueError, match="Unsafe TEST_DATABASE_URL"):
        validate_test_database_url(url)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_database_guard_allows_only_the_throwaway_port(host):
    url = f"postgresql://fgt_test:fgt_test@{host}:15432/fgt_test"
    assert validate_test_database_url(url) == url


def test_database_guard_explicit_override():
    url = "postgresql://x@disposable.example.invalid:18232/isolated"
    assert validate_test_database_url(url, allow_any=True) == url
    assert validate_test_database_url(DEFAULT_TEST_DATABASE_URL) == DEFAULT_TEST_DATABASE_URL


@pytest.mark.parametrize("send", [httpx.get, requests.get])
def test_real_synchronous_http_is_blocked(send):
    with pytest.raises(AssertionError, match="Real outbound network is blocked"):
        send("https://example.invalid")


def test_real_async_http_is_blocked():
    async def send():
        async with httpx.AsyncClient() as client:
            with pytest.raises(AssertionError, match="Real outbound network is blocked"):
                await client.get("https://example.invalid")
        async with aiohttp.ClientSession() as client:
            with pytest.raises(AssertionError, match="Real outbound network is blocked"):
                await client.get("https://example.invalid")

    asyncio.run(send())


@pytest.mark.parametrize("smtp", [smtplib.SMTP, smtplib.SMTP_SSL])
def test_real_smtp_is_blocked(smtp):
    with pytest.raises(AssertionError, match="Real outbound network is blocked"):
        smtp("smtp.example.invalid")


@pytest.mark.parametrize("fixture_name", ["ebas_stub", "startgg_stub"])
def test_http_provider_stubs_return_configured_responses(request, fixture_name):
    stub = request.getfixturevalue(fixture_name)
    stub.response = {"synthetic": True}
    with httpx.Client(transport=httpx.MockTransport(stub.handle)) as client:
        response = client.post("https://provider.example.invalid", json={"test": "payload"})
    assert response.json() == {"synthetic": True}
    assert stub.calls == [
        {
            "method": "POST",
            "url": "https://provider.example.invalid",
            "json": {"test": "payload"},
        }
    ]


def test_smtp_stub_records_messages(smtp_stub):
    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "player@example.invalid"
    message.set_content("Synthetic verification code: 123456")
    with smtplib.SMTP("smtp.example.invalid") as sender:
        sender.send_message(message)
    assert len(smtp_stub.calls) == 1
    assert smtp_stub.calls[0]["to"] == "player@example.invalid"


@pytest.mark.db
def test_database_uses_private_schema_with_real_migrations(pg_db):
    with pg_db.pool.connection() as connection:
        assert connection.execute("SELECT current_schema()").fetchone()[0] == pg_db.schema
        assert connection.execute("SELECT count(*) FROM active_event_data").fetchone()[0] == 0
        connection.execute("SELECT acquisition_source FROM active_event_data LIMIT 0")
        connection.execute("SELECT checkin_opened_at FROM settings LIMIT 0")
        connection.execute("SELECT no_show_rate FROM event_stats LIMIT 0")

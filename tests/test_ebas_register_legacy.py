"""Real-handler coverage for slow legacy registration; every provider call is in memory."""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_PERSONNUMMER = "000000000000"


@pytest.fixture
def legacy_context(pg_db, backend_app, n8n_stub, monkeypatch):
    from status_api import capability_token

    monkeypatch.setattr(backend_app.ebas_legacy, "INTEGRATION_ENGINE", "n8n")
    pg_db.seed_settings()
    row = pg_db.seed_checkin()
    monkeypatch.setattr(backend_app.ebas_legacy, "EBAS_REGISTER_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(backend_app.ebas_legacy, "_ebas_registrations", {})
    monkeypatch.setattr(backend_app.ebas_legacy, "_ebas_registration_lock", asyncio.Lock())
    monkeypatch.setattr(backend_app.ebas_legacy, "_ebas_registration_tasks", set())
    n8n_stub.response = {"success": True}
    payload = {
        "checkin_id": row["record_id"],
        "status_token": capability_token(row["record_id"]),
        "personnummer": SYNTHETIC_PERSONNUMMER,
        "tag": row["tag"],
        "slug": row["event_slug"],
        "email": row["email"],
        "telephone": row["telephone"],
    }
    return backend_app, row, payload


def asgi_client(backend):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend.app), base_url="http://testserver"
    )


@pytest.mark.db
def test_existing_member_returns_success_without_resubmitting(legacy_context, pg_db, n8n_stub):
    backend, row, payload = legacy_context
    backend.update_checkin(row["record_id"], {"member": True})
    payload.pop("personnummer")

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
        assert response.status_code == 200
        assert response.json() == {"success": True, "already_member": True}
        assert n8n_stub.calls == []

    asyncio.run(scenario())


@pytest.mark.db
def test_concurrent_registration_submits_once(legacy_context, n8n_stub, monkeypatch):
    backend, row, payload = legacy_context
    monkeypatch.setattr(backend.ebas_legacy, "EBAS_REGISTER_WAIT_SECONDS", 5)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_provider(request):
            entered.set()
            await release.wait()

        n8n_stub.on_call = slow_provider
        async with asgi_client(backend) as client:
            first = asyncio.create_task(client.post("/api/ebas/register", json=payload))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                second = await client.post("/api/ebas/register", json=payload)
                assert second.status_code == 202
                assert second.json() == {"success": True, "state": "processing"}
                assert len(n8n_stub.calls) == 1
            finally:
                release.set()
            assert (await first).json() == {"success": True}
        assert backend.ebas_legacy._ebas_registrations == {}
        assert not backend.ebas_legacy._ebas_registration_tasks

    asyncio.run(scenario())


@pytest.mark.db
def test_slow_registration_keeps_running_and_receives_callback(legacy_context, n8n_stub, pg_db):
    backend, row, payload = legacy_context

    async def scenario():
        release, completed = asyncio.Event(), asyncio.Event()
        async with asgi_client(backend) as client:

            async def slow_provider(request):
                await release.wait()
                callback = await client.post(
                    f"/api/checkin/{row['record_id']}/member-status", json={"member": True}
                )
                assert callback.status_code == 200
                completed.set()

            n8n_stub.on_call = slow_provider
            response = await client.post("/api/ebas/register", json=payload)
            assert response.status_code == 202
            assert response.json() == {"success": True, "state": "processing"}
            assert not completed.is_set()
            assert row["record_id"] in backend.ebas_legacy._ebas_registrations
            assert len(backend.ebas_legacy._ebas_registration_tasks) == 1
            release.set()
            await asyncio.gather(*backend.ebas_legacy._ebas_registration_tasks)
        assert completed.is_set()
        assert pg_db.checkins()[0]["member"] is True
        assert backend.ebas_legacy._ebas_registrations == {}
        assert not backend.ebas_legacy._ebas_registration_tasks

    asyncio.run(scenario())


@pytest.mark.db
@pytest.mark.parametrize("failure", ["http", "network", "timeout"])
def test_registry_is_released_and_personal_id_is_never_logged(
    legacy_context, n8n_stub, caplog, failure
):
    backend, row, payload = legacy_context
    caplog.set_level(logging.INFO)
    if failure == "http":
        n8n_stub.status_code = 500
        n8n_stub.response = {"message": f"echo {SYNTHETIC_PERSONNUMMER} or 00000000-0000"}
    else:

        def fail(request):
            exception = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise exception(f"request body contained {SYNTHETIC_PERSONNUMMER}")

        n8n_stub.on_call = fail

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
        assert response.status_code == (202 if failure == "timeout" else 502)
        if failure == "timeout":
            assert response.json() == {"success": True, "state": "processing"}
        assert backend.ebas_legacy._ebas_registrations == {}
        assert not backend.ebas_legacy._ebas_registration_tasks

    asyncio.run(scenario())
    assert SYNTHETIC_PERSONNUMMER not in caplog.text
    assert "00000000-0000" not in caplog.text


@pytest.mark.db
def test_expired_registry_entry_allows_a_new_attempt(legacy_context, n8n_stub):
    backend, row, payload = legacy_context
    backend.ebas_legacy._ebas_registrations[row["record_id"]] = (
        backend.time.monotonic() - backend.ebas_legacy.EBAS_REGISTER_TIMEOUT_SECONDS - 121
    )

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
        assert response.status_code == 200
        assert len(n8n_stub.calls) == 1
        assert backend.ebas_legacy._ebas_registrations == {}

    asyncio.run(scenario())


@pytest.mark.db
def test_upstream_uses_configured_timeout(legacy_context, n8n_stub, monkeypatch):
    backend, row, payload = legacy_context
    monkeypatch.setattr(backend.ebas_legacy, "EBAS_REGISTER_TIMEOUT_SECONDS", 81)

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
        assert response.status_code == 200

    # httpx exposes the timeout passed to the real client on the transport request.
    original = backend.httpx_client._transport.handler
    timeouts = []

    async def capture(request):
        timeouts.append(request.extensions["timeout"])
        return await original(request)

    monkeypatch.setattr(backend.httpx_client._transport, "handler", capture)
    asyncio.run(scenario())
    assert timeouts[0]["read"] == 81


def test_backend_runs_one_worker():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "command: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1" in compose
    assert "--port 8000 --workers 2" not in compose


@pytest.mark.parametrize("scenario", ["success", "still_processing", "error_text"])
def test_registration_page_waits_without_resubmitting(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable for registration-page behavior test")
    template = (ROOT / "backend" / "templates" / "register.html").read_text(encoding="utf-8")
    registration = template.split("// eBas Registration\n", 1)[1].split(
        "// Payment section is informational only", 1
    )[0]
    harness = r"""
const assert = require('node:assert/strict');
const scenario = process.argv[1];
let now = 0, submit, posts = 0, polls = 0;
const messages = [], delays = [], elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const item = { value: '', disabled: false, dataset: {}, textContent: 'Register', hidden: false };
    item.classList = { add: () => { item.hidden = true; }, remove: () => { item.hidden = false; } };
    item.addEventListener = (event, callback) => { submit = callback; };
    item.replaceChildren = message => { messages.push(message.textContent); };
    elements.set(id, item);
  }
  return elements.get(id);
}
const btn = element('btn-register-ebas');
element('pnr-ebas').value = '000000000000';
element('email-ebas').value = 'player@example.invalid';
element('phone-ebas').value = '0000000000';
const controls = [btn, element('pnr-ebas'), element('email-ebas'), element('phone-ebas')];
const document = {
  getElementById: element, querySelectorAll: () => controls,
  createElement: () => ({ textContent: '', className: '' })
};
const state = { name: 'Test Player 1', tag: 'test-player', eventSlug: 'test-event', needsEbas: true };
const localStorage = { getItem: () => '' };
const Validation = { sanitizePersonnummer: value => value, validatePersonnummer: () => ({ valid: true }), sanitizePhone: value => value };
function setButtonLoading(button, loading) { button.disabled = loading; }
function clearMessages() {}
function showMessage(text) { messages.push(text); }
function setStatus() {}
function updateFinalButton() {}
const Date = { now: () => now };
const AbortSignal = { timeout: () => undefined };
function setTimeout(callback, delay) { delays.push(delay); now += delay; queueMicrotask(callback); }
async function fetch(url, options) {
  assert.equal(btn.disabled, true);
  assert(controls.every(control => control.disabled));
  if (options && options.method === 'POST') {
    posts++;
    if (scenario === 'error_text') return { status: 400, json: async () => ({ message: '<img src=x onerror=alert(1)>' }) };
    return { status: 202, json: async () => ({ success: true, state: 'processing' }) };
  }
  assert.equal(state.needsEbas, true);
  assert.equal(url, '/api/participant/Test%20Player%201/status');
  polls++;
  return { ok: true, json: async () => ({ member: scenario === 'success' && polls >= 2 }) };
}
"""
    assertions = r"""
(async () => {
  await submit.call(btn);
  assert.equal(posts, 1);
  if (scenario === 'success') {
    assert.equal(polls, 2);
    assert.equal(state.needsEbas, false);
    assert.equal(element('section-ebas').hidden, true);
    assert(messages[0].includes("Don't close this page"));
    assert(messages.at(-1).includes('Membership registered!'));
  } else if (scenario === 'still_processing') {
    assert.equal(now, 240000);
    assert.equal(btn.disabled, true);
    assert.equal(state.needsEbas, true);
    assert(messages.at(-1).includes('ask a TO'));
    await submit.call(btn);
    assert.equal(posts, 1);
  } else {
    assert.equal(messages.at(-1), '<img src=x onerror=alert(1)>');
    assert.equal(state.needsEbas, true);
    assert.equal(polls, 0);
  }
  assert(delays.every(delay => delay === 5000));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness + registration + assertions, scenario],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

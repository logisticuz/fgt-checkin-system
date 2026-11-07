"""Legacy registration contracts against isolated PG and in-memory provider responses."""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from shared import postgres_api as postgres

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_PERSONNUMMER = "000000000000"
ACCEPTED = {"success": True, "state": "registered", "registered": True, "member": True}
PENDING = {"success": False, "state": "local_sync_pending", "provider_accepted": True}
REJECTED = {
    "success": False,
    "state": "rejected",
    "verified_rejection": True,
    "request_result": "fail",
    "field_errors": {"email": ["Check the email address"]},
}


@pytest.fixture
def legacy_context(pg_db, backend_app, n8n_stub, monkeypatch):
    from status_api import capability_token

    monkeypatch.setattr(backend_app.ebas_legacy, "INTEGRATION_ENGINE", "n8n")
    monkeypatch.setattr(backend_app.ebas_legacy, "EBAS_REGISTER_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(backend_app.ebas_legacy, "_ebas_registration_tasks", set())
    pg_db.seed_settings()
    row = pg_db.seed_checkin()
    n8n_stub.response = ACCEPTED
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


def attempt(database, checkin_id):
    with database.pool.connection() as connection:
        row = connection.execute(
            "SELECT state, started_at, resolved_by FROM legacy_register_attempts WHERE checkin_id=%s",
            (checkin_id,),
        ).fetchone()
    return dict(zip(("state", "started_at", "resolved_by"), row)) if row else None


@pytest.mark.db
def test_existing_member_returns_canonical_success_without_submitting(legacy_context, n8n_stub):
    backend, row, payload = legacy_context
    backend.update_checkin(row["record_id"], {"member": True})
    payload.pop("personnummer")

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
        assert response.status_code == 200
        assert response.json()["already_member"] is True
        assert response.json()["member"] is True
        assert response.json()["state"] == "registered"
        assert n8n_stub.calls == []

    asyncio.run(scenario())


@pytest.mark.db
def test_concurrent_requests_commit_one_guard_before_one_provider_submit(
    legacy_context, pg_db, n8n_stub
):
    backend, row, payload = legacy_context

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_provider(request):
            assert attempt(pg_db, row["record_id"])["state"] == "in_flight"
            entered.set()
            await release.wait()

        n8n_stub.on_call = slow_provider
        async with asgi_client(backend) as client:
            first = asyncio.create_task(client.post("/api/ebas/register", json=payload))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                second = await client.post("/api/ebas/register", json=payload)
                assert second.status_code == 202
                assert second.json()["state"] == "processing"
                assert second.json()["retry_allowed"] is False
                assert len(n8n_stub.calls) == 1
            finally:
                release.set()
            assert (await first).json()["state"] == "local_sync_pending"
        assert attempt(pg_db, row["record_id"])["state"] == "accepted"

    asyncio.run(scenario())


@pytest.mark.db
def test_slow_callback_can_confirm_canonical_membership_after_browser_returns(
    legacy_context, pg_db, n8n_stub
):
    backend, row, payload = legacy_context

    async def scenario():
        release = asyncio.Event()
        async with asgi_client(backend) as client:

            async def slow_provider(request):
                await release.wait()
                callback = await client.post(
                    f"/api/checkin/{row['record_id']}/member-status",
                    json={"member": True, "ok": True, "state": "verified_true"},
                )
                assert callback.status_code == 200 and callback.json()["member"] is True

            n8n_stub.on_call = slow_provider
            response = await client.post("/api/ebas/register", json=payload)
            assert response.status_code == 202 and response.json()["state"] == "processing"
            assert attempt(pg_db, row["record_id"])["state"] == "in_flight"
            release.set()
            await asyncio.gather(*backend.ebas_legacy._ebas_registration_tasks)
            confirmed = await client.post("/api/ebas/register", json=payload)
        assert confirmed.json()["member"] is True
        assert len(n8n_stub.calls) == 1
        assert pg_db.checkins()[0]["member"] is True

    asyncio.run(scenario())


@pytest.mark.db
@pytest.mark.parametrize(
    "failure", ["http", "redirect", "network", "timeout", "invalid_json", "empty", "bare_success"]
)
def test_unknown_outcomes_remain_durable_and_never_echo_private_inputs(
    legacy_context, pg_db, n8n_stub, monkeypatch, caplog, failure
):
    backend, row, payload = legacy_context
    caplog.set_level(logging.INFO)
    if failure in ("http", "redirect"):
        n8n_stub.status_code = 500 if failure == "http" else 302
        n8n_stub.response = {
            "message": f"echo {SYNTHETIC_PERSONNUMMER}",
            "member_errors": {"email": ["not authoritative"]},
        }
    elif failure in ("timeout", "network"):

        def fail(request):
            kind = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise kind(f"request included {SYNTHETIC_PERSONNUMMER}")

        n8n_stub.on_call = fail
    elif failure == "invalid_json":
        original = backend.httpx_client._transport.handler

        async def invalid(request):
            await original(request)
            return httpx.Response(200, content=b'not-json', request=request)

        monkeypatch.setattr(backend.httpx_client._transport, "handler", invalid)
    else:
        n8n_stub.response = {} if failure == "empty" else {"success": True}

    async def scenario():
        async with asgi_client(backend) as client:
            first = await client.post("/api/ebas/register", json=payload)
            assert first.status_code == 202 and first.json()["state"] == "outcome_unknown"
            assert first.json()["retry_allowed"] is False
            assert attempt(pg_db, row["record_id"])["state"] == "unknown"
            # No elapsed TTL or process-local task registry can release this guard.
            with pg_db.pool.connection() as connection:
                connection.execute(
                    "UPDATE legacy_register_attempts SET started_at=clock_timestamp()-interval '100 days' WHERE checkin_id=%s",
                    (row["record_id"],),
                )
            monkeypatch.setattr(backend.ebas_legacy, "_ebas_registration_tasks", set())
            second = await client.post("/api/ebas/register", json=payload)
            assert second.json()["state"] == "processing"
            assert len(n8n_stub.calls) == 1
            assert SYNTHETIC_PERSONNUMMER not in first.text

    asyncio.run(scenario())
    assert SYNTHETIC_PERSONNUMMER not in caplog.text


@pytest.mark.db
def test_verified_rejection_whitelists_field_errors_and_allows_a_new_explicit_submit(
    legacy_context, pg_db, n8n_stub
):
    backend, row, payload = legacy_context
    n8n_stub.response = {
        **REJECTED,
        "raw_response": {"secret": "must-not-leak"},
        "member_errors": {"secret": "must-not-leak"},
        "field_errors": {
            "socialsecuritynumber": [f"Invalid {SYNTHETIC_PERSONNUMMER}"],
            "email": ["Check email"],
            "private_debug": ["must-not-leak"],
        },
    }

    async def scenario():
        async with asgi_client(backend) as client:
            first = await client.post("/api/ebas/register", json=payload)
            assert first.status_code == 422
            assert first.json()["retry_allowed"] is True
            assert first.json()["field_errors"] == {
                "personnummer": ["Invalid [personal ID]"],
                "email": ["Check email"],
            }
            assert set(first.json()) == {
                "success",
                "state",
                "retry_allowed",
                "field_errors",
                "user_message",
            }
            assert "must-not-leak" not in first.text
            before = attempt(pg_db, row["record_id"])["started_at"]
            second = await client.post("/api/ebas/register", json=payload)
            assert second.status_code == 422 and len(n8n_stub.calls) == 2
            assert attempt(pg_db, row["record_id"])["started_at"] != before

    asyncio.run(scenario())


@pytest.mark.db
@pytest.mark.parametrize("body", [ACCEPTED, PENDING])
def test_provider_acceptance_without_canonical_local_member_stays_pending(
    legacy_context, pg_db, n8n_stub, body
):
    backend, row, payload = legacy_context
    n8n_stub.response = body

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
            assert response.status_code == 202 and response.json()["state"] == "local_sync_pending"
            assert response.json()["success"] is False
            assert pg_db.checkins()[0]["member"] is False
            second = await client.post("/api/ebas/register", json=payload)
            assert second.json()["state"] == "local_sync_pending"
            assert len(n8n_stub.calls) == 1

    asyncio.run(scenario())


@pytest.mark.db
def test_late_canonical_callback_closes_unknown_guard_without_another_provider_submit(
    legacy_context, pg_db, n8n_stub
):
    backend, row, payload = legacy_context

    def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout")

    n8n_stub.on_call = timeout

    async def scenario():
        async with asgi_client(backend) as client:
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "state"
            ] == "outcome_unknown"
            callback = await client.post(
                f"/api/checkin/{row['record_id']}/member-status",
                json={"member": True, "ok": True, "state": "verified_true"},
            )
            assert callback.json()["member"] is True
            assert attempt(pg_db, row["record_id"])["state"] == "accepted"
            assert attempt(pg_db, row["record_id"])["resolved_by"] == "reconcile"
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "already_member"
            ] is True
            assert len(n8n_stub.calls) == 1

    asyncio.run(scenario())


@pytest.mark.db
def test_invalid_callback_cannot_resolve_guard_even_when_member_is_already_true(
    legacy_context, pg_db
):
    _, row, _ = legacy_context
    claim = postgres.claim_legacy_registration(row["record_id"], row["event_slug"], can_submit=True)
    postgres.finish_legacy_registration(row["record_id"], claim["started_at"], "unknown")
    with pg_db.pool.connection() as connection:
        connection.execute(
            "UPDATE active_event_data SET member=true, member_source='v2' WHERE record_id=%s",
            (row["record_id"],),
        )
    result = postgres.apply_legacy_member_status(
        row["record_id"],
        True,
        ok=False,
        state="unavailable",
        envelope={"member": True, "ok": False, "state": "unavailable"},
    )
    assert result["ok"] is False
    assert attempt(pg_db, row["record_id"])["state"] == "unknown"
    postgres.apply_legacy_member_status(
        row["record_id"],
        True,
        ok=True,
        state="verified_true",
        envelope={"member": True, "ok": True, "state": "verified_true"},
    )
    assert attempt(pg_db, row["record_id"])["state"] == "accepted"


@pytest.mark.db
def test_unknown_tombstone_survives_archive_and_reopen(legacy_context, pg_db, n8n_stub):
    backend, row, payload = legacy_context

    def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout")

    n8n_stub.on_call = timeout

    async def scenario():
        async with asgi_client(backend) as client:
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "state"
            ] == "outcome_unknown"
            postgres.archive_event(row["event_slug"], clear_active=True)
            assert not pg_db.checkins()
            assert attempt(pg_db, row["record_id"])["state"] == "unknown"
            postgres.reopen_event(row["event_slug"])
            assert pg_db.checkins()[0]["record_id"] == row["record_id"]
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "state"
            ] == "processing"
            assert len(n8n_stub.calls) == 1

    asyncio.run(scenario())


@pytest.mark.db
def test_to_negative_resolution_allows_exactly_one_reviewed_retry_and_old_completion_is_stale(
    legacy_context, pg_db, n8n_stub
):
    backend, row, payload = legacy_context
    n8n_stub.response = {}

    async def scenario():
        async with asgi_client(backend) as client:
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "state"
            ] == "outcome_unknown"
            previous = attempt(pg_db, row["record_id"])["started_at"]
            backend.update_checkin(row["record_id"], {"member": False})
            assert attempt(pg_db, row["record_id"])["resolved_by"] == "to"
            entered, release = asyncio.Event(), asyncio.Event()

            async def slow(request):
                entered.set()
                await release.wait()

            n8n_stub.on_call = slow
            n8n_stub.response = PENDING
            first = asyncio.create_task(client.post("/api/ebas/register", json=payload))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = await client.post("/api/ebas/register", json=payload)
                assert second.json()["state"] == "processing"
                current = attempt(pg_db, row["record_id"])["started_at"]
                postgres.finish_legacy_registration(row["record_id"], previous, "rejected")
                assert attempt(pg_db, row["record_id"])["started_at"] == current
                assert attempt(pg_db, row["record_id"])["state"] == "in_flight"
            finally:
                release.set()
            await first
            await asyncio.gather(*backend.ebas_legacy._ebas_registration_tasks)
            assert len(n8n_stub.calls) == 2

    asyncio.run(scenario())


@pytest.mark.db
def test_migration_is_idempotent_has_no_sensitive_columns_or_active_only_fk_and_is_required(pg_db):
    from scripts import migration_preflight
    from shared import migrate

    path = ROOT / "shared/migrations/006_legacy_register_attempts.sql"
    with pg_db.pool.connection() as connection:
        connection.execute(path.read_text())
        connection.execute(path.read_text())
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='legacy_register_attempts'"
        ).fetchall()
        assert {row[0] for row in columns} == {
            "checkin_id",
            "state",
            "started_at",
            "updated_at",
            "resolved_by",
        }
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='legacy_register_attempts'::regclass AND contype='f'"
            ).fetchone()[0]
            == 0
        )
    migrate.apply(pg_db.pool)
    with pg_db.pool.connection() as connection:
        connection.execute("DELETE FROM fgt_schema_migrations WHERE version='006'")
        report = migration_preflight.inspect_database(connection)
        assert report["ready_to_deploy"] is False and report["required_unapplied"] == ["006"]


@pytest.mark.db
def test_upstream_uses_configured_timeout(legacy_context, n8n_stub, monkeypatch):
    backend, row, payload = legacy_context
    monkeypatch.setattr(backend.ebas_legacy, "EBAS_REGISTER_TIMEOUT_SECONDS", 81)
    original = backend.httpx_client._transport.handler
    timeouts = []

    async def capture(request):
        timeouts.append(request.extensions["timeout"])
        return await original(request)

    monkeypatch.setattr(backend.httpx_client._transport, "handler", capture)

    async def scenario():
        async with asgi_client(backend) as client:
            assert (await client.post("/api/ebas/register", json=payload)).status_code == 202

    asyncio.run(scenario())
    assert timeouts[0]["read"] == 81


@pytest.mark.db
def test_to_resolution_and_member_edit_roll_back_together_on_audit_failure(
    legacy_context, pg_db, n8n_stub, monkeypatch
):
    backend, row, payload = legacy_context
    n8n_stub.response = {}

    async def submit():
        async with asgi_client(backend) as client:
            await client.post("/api/ebas/register", json=payload)

    asyncio.run(submit())
    original = postgres._integration_audit

    def fail_resolution(connection, action, *args):
        if action == "legacy_registration_resolved":
            raise RuntimeError("synthetic audit failure")
        return original(connection, action, *args)

    monkeypatch.setattr(postgres, "_integration_audit", fail_resolution)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        backend.update_checkin(row["record_id"], {"member": True})
    assert pg_db.checkins()[0]["member"] is False
    assert attempt(pg_db, row["record_id"])["state"] == "unknown"


@pytest.mark.db
def test_failed_guard_write_sends_nothing_and_failed_completion_write_keeps_guard(
    legacy_context, pg_db, n8n_stub, monkeypatch
):
    backend, row, payload = legacy_context
    original = postgres.claim_legacy_registration

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(postgres, "claim_legacy_registration", failed)

    async def scenario():
        async with asgi_client(backend) as client:
            response = await client.post("/api/ebas/register", json=payload)
            assert response.json()["state"] == "outcome_unknown"
            assert n8n_stub.calls == []
            monkeypatch.setattr(postgres, "claim_legacy_registration", original)
            monkeypatch.setattr(postgres, "finish_legacy_registration", failed)
            response = await client.post("/api/ebas/register", json=payload)
            assert response.json()["state"] == "local_sync_pending"
            assert attempt(pg_db, row["record_id"])["state"] == "in_flight"
            assert (await client.post("/api/ebas/register", json=payload)).json()[
                "state"
            ] == "processing"
            assert len(n8n_stub.calls) == 1

    asyncio.run(scenario())


def test_backend_runs_one_worker():
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "command: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1" in compose
    assert "--port 8000 --workers 2" not in compose


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "still_processing",
        "error_text",
        "unknown",
        "network",
        "invalid_json",
        "local_sync_pending",
        "unconfirmed_success",
    ],
)
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
const checkinId = '00000000-0000-4000-8000-000000000042';
const statusToken = 'synthetic-legacy-status-capability';
const window = { location: { search: `?checkin_id=${checkinId}&t=${statusToken}` } };
const params = new URLSearchParams(window.location.search);
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
    const body = JSON.parse(options.body);
    assert.equal(body.checkin_id, checkinId);
    assert.equal(body.status_token, statusToken);
    if (scenario === 'error_text') return { status: 422, json: async () => ({ state: 'rejected', retry_allowed: true, field_errors: { email: ['<img src=x onerror=alert(1)>'] } }) };
    if (scenario === 'network') throw new Error('synthetic transport failure');
    if (scenario === 'invalid_json') return { status: 200, json: async () => { throw new Error('invalid JSON'); } };
    if (scenario === 'unknown') return { status: 202, json: async () => ({ state: 'outcome_unknown', retry_allowed: false }) };
    if (scenario === 'unconfirmed_success') return { status: 200, json: async () => ({ state: 'registered', success: true, member: false }) };
    if (scenario === 'local_sync_pending') return { status: 202, json: async () => ({ state: 'local_sync_pending', provider_accepted: true }) };
    return { status: 202, json: async () => ({ success: true, state: 'processing' }) };
  }
  assert.equal(state.needsEbas, true);
  assert.equal(url, `/api/checkin/${checkinId}/status?t=${statusToken}`);
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
    assert(messages.at(-1).includes('a TO will check'));
    await submit.call(btn);
    assert.equal(posts, 1);
  } else if (scenario === 'error_text') {
    assert.equal(messages.at(-1), 'Email: <img src=x onerror=alert(1)>');
    assert.equal(state.needsEbas, true);
    assert.equal(polls, 0);
    assert.equal(btn.disabled, false);
    await submit.call(btn);
    assert.equal(posts, 2);
  } else {
    assert.equal(state.needsEbas, true);
    assert.equal(btn.disabled, true);
    assert(messages.at(-1).includes(scenario === 'local_sync_pending' ? 'a TO will finish' : 'a TO will check'));
    assert(!messages.at(-1).includes('Membership registered!'));
    await submit.call(btn);
    assert.equal(posts, 1);
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

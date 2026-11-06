"""Smoke coverage uses the real FastAPI routes and disposable Postgres storage."""

import httpx
import pytest

from tests.fixtures import make_checkin


def test_health_uses_no_external_provider(handler_client, n8n_stub):
    response = handler_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["components"]["integration_engine"] == "none"
    assert n8n_stub.calls == []


def test_orchestrate_rejects_invalid_payload_before_storage(handler_client):
    response = handler_client.post("/api/checkin/orchestrate", json={"name": "Test Player 1"})
    assert response.status_code == 400
    assert "Tag/gamertag is required" in response.json()["detail"]["errors"]


@pytest.mark.db
def test_orchestrate_creates_exactly_one_row(app_client, pg_db, n8n_stub):
    payload = make_checkin()
    response = app_client.post("/api/checkin/orchestrate", json=payload)
    assert response.status_code == 200
    body = response.json()
    rows = pg_db.checkins()
    assert len(rows) == 1
    assert rows[0]["record_id"] == body["checkin_id"]
    assert rows[0]["name"] == payload["name"]
    assert body["ready"] is False
    assert rows[0]["status"] == "Pending"
    assert len(n8n_stub.calls) == 1
    assert n8n_stub.calls[0]["json"]["checkin_id"] == body["checkin_id"]


@pytest.mark.db
def test_integration_result_updates_the_existing_row(app_client, pg_db):
    checkin = app_client.post("/api/checkin/orchestrate", json=make_checkin()).json()
    response = app_client.post(
        "/api/integration/result",
        json={
            "checkin_id": checkin["checkin_id"],
            "source": "ebas",
            "ok": True,
            "data": {"member": True},
        },
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    rows = pg_db.checkins()
    assert len(rows) == 1
    assert rows[0]["member"] is True


@pytest.mark.db
def test_integration_callback_recomputes_stored_status(app_client, pg_db, request):
    row = pg_db.seed_checkin(status="Pending", startgg=True)
    response = app_client.post(
        "/api/integration/result",
        json={
            "checkin_id": row["record_id"],
            "source": "ebas",
            "ok": True,
            "data": {"member": True},
        },
    )
    assert response.status_code == 200
    stored = pg_db.checkins()[0]
    assert stored["member"] is True
    # Mark only the final assertion; transport/flag-update regressions must fail normally.
    request.node.add_marker(
        pytest.mark.xfail(
            strict=True,
            reason="integration/result does not recompute status; fixed in T-004",
        )
    )
    assert stored["status"] == "Ready"


@pytest.mark.db
def test_orchestrate_recomputes_status_after_real_integration_callbacks(
    app_client, pg_db, backend_app, n8n_stub
):
    async def integration_callbacks(request):
        checkin_id = n8n_stub.calls[-1]["json"]["checkin_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app.app), base_url="http://testserver"
        ) as callback_client:
            for source, data in (
                ("ebas", {"member": True}),
                ("startgg", {"registered": True, "events": ["Test Game"]}),
            ):
                callback = await callback_client.post(
                    "/api/integration/result",
                    json={"checkin_id": checkin_id, "source": source, "ok": True, "data": data},
                )
                assert callback.status_code == 200

    n8n_stub.on_call = integration_callbacks
    response = app_client.post("/api/checkin/orchestrate", json=make_checkin())
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["missing"] == []
    rows = pg_db.checkins()
    assert len(rows) == 1
    assert rows[0]["status"] == "Ready"
    assert rows[0]["tournament_games_registered"] == ["Test Game"]


@pytest.mark.db
def test_posting_same_checkin_twice_keeps_one_row(app_client, pg_db):
    payload = make_checkin()
    first = app_client.post("/api/checkin/orchestrate", json=payload)
    second = app_client.post("/api/checkin/orchestrate", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["checkin_id"] == second.json()["checkin_id"]
    assert len(pg_db.checkins()) == 1


@pytest.mark.db
def test_member_status_recomputes_stored_status(app_client, pg_db):
    row = pg_db.seed_checkin(startgg=True)
    response = app_client.post(
        f"/api/checkin/{row['record_id']}/member-status", json={"member": True}
    )
    response.raise_for_status()
    assert pg_db.checkins()[0]["status"] == "Ready"

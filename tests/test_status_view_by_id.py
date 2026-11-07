"""Status pages select the capability's active-event row and share API readiness rules."""

import json
import uuid
from urllib.parse import quote

import pytest
import status_api as status


@pytest.fixture(autouse=True)
def synthetic_capability_key(monkeypatch):
    monkeypatch.setattr(status, "_TOKEN_KEY", b"synthetic-status-view-signing-key")
    monkeypatch.setattr(status, "_now_seconds", lambda: 1_800_000_000)


def page_url(name):
    return "/status/" + quote(name, safe="")


def capability_params(checkin_id):
    return {"checkin_id": checkin_id, "t": status.capability_token(checkin_id)}


def forbid_name_lookups(backend_app, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Verified-ID status pages must never fall back to a name lookup")

    monkeypatch.setattr(backend_app.pages, "check_participant_status", forbidden)
    monkeypatch.setattr(backend_app.pages, "get_participant_details", forbidden)


@pytest.mark.db
def test_same_name_players_render_their_own_canonical_status_and_template(
    app_client, pg_db, backend_app, monkeypatch
):
    pg_db.seed_settings(
        require_membership=True, require_startgg=True, require_payment=True
    )
    first = pg_db.seed_checkin(
        name="Same synthetic name",
        tag="Ready synthetic tag",
        member=True,
        startgg=True,
        payment_valid=True,
        status="Pending",
        tournament_games_registered=["Ready synthetic game"],
    )
    second = pg_db.seed_checkin(
        name="Same synthetic name",
        tag="Pending synthetic tag",
        member=True,
        startgg=False,
        payment_valid=False,
        status="Ready",
        tournament_games_registered=["Pending synthetic game"],
    )
    forbid_name_lookups(backend_app, monkeypatch)
    for row, expected in ((first, True), (second, False)):
        params = capability_params(row["record_id"])
        api = app_client.get(
            f"/api/checkin/{row['record_id']}/status", params={"t": params["t"]}
        ).json()
        for _ in range(
            2
        ):  # Repeated renders stay aligned with ID polling, including stale stored status.
            response = app_client.get(page_url(row["name"]), params=params)
            assert response.status_code == 200
            assert response.template.name == (
                "status_ready.html" if expected else "status_pending.html"
            )
            context = response.context
            assert context["tag"] == row["tag"]
            assert (
                context["games"] == api["games"] == row["tournament_games_registered"]
            )
            assert context["found"] is True and context["ready"] is expected
            assert context["missing"] == api["missing"]
            for key in (
                "found",
                "ready",
                "missing",
                "member",
                "startgg",
                "payment_ok",
                "status",
            ):
                assert context["status"][key] == api[key]
            assert context["status"]["payment"] is api["payment_ok"]
            assert (
                context["checkin_id"] == row["record_id"]
                and context["status_token"] == params["t"]
            )


@pytest.mark.db
@pytest.mark.parametrize("kind", ["unknown", "inactive-event", "no-active-event"])
def test_verified_missing_or_inactive_id_is_pending_without_name_fallback(
    app_client, pg_db, backend_app, monkeypatch, kind
):
    # Even with every requirement off, a missing ID must not render Ready.
    pg_db.seed_settings(
        require_membership=False, require_startgg=False, require_payment=False
    )
    active = pg_db.seed_checkin(
        name="Shared synthetic name",
        tag="Active synthetic tag",
        member=True,
        startgg=True,
        status="Ready",
    )
    if kind == "unknown":
        checkin_id = str(uuid.uuid4())
    elif kind == "inactive-event":
        old = pg_db.seed_checkin(
            name=active["name"],
            tag="Old synthetic tag",
            event_slug="old-event",
            member=True,
            startgg=True,
            status="Ready",
            tournament_games_registered=["Old synthetic game"],
        )
        checkin_id = old["record_id"]
    else:
        checkin_id = active["record_id"]
        with pg_db.pool.connection() as connection:
            connection.execute("UPDATE settings SET is_active = false")
    forbid_name_lookups(backend_app, monkeypatch)
    response = app_client.get(
        page_url(active["name"]), params=capability_params(checkin_id)
    )
    assert (
        response.status_code == 200 and response.template.name == "status_pending.html"
    )
    assert response.context["found"] is False and response.context["ready"] is False
    assert response.context["status"]["found"] is False
    assert response.context["status"]["summary"] == "Not found"
    assert response.context["tag"] == "" and response.context["games"] == []
    assert response.context["checkin_id"] == checkin_id


@pytest.mark.db
@pytest.mark.parametrize(
    "kind", ["no-params", "no-token", "no-id", "invalid", "expired", "mismatched"]
)
def test_unverified_capabilities_preserve_legacy_name_rendering(
    app_client, pg_db, backend_app, monkeypatch, kind
):
    named = pg_db.seed_checkin(
        name="Legacy synthetic name",
        tag="Legacy synthetic tag",
        member=True,
        startgg=True,
        status="Ready",
        tournament_games_registered=["Legacy synthetic game"],
    )
    other = pg_db.seed_checkin(
        name="Different synthetic name", member=False, startgg=False
    )
    params = capability_params(other["record_id"])
    if kind == "no-params":
        params = {}
    elif kind == "no-token":
        params.pop("t")
    elif kind == "no-id":
        params.pop("checkin_id")
    elif kind == "invalid":
        params["t"] = "invalid-token"
    elif kind == "expired":
        params["t"] = status.capability_token(other["record_id"], ttl=-1)
    else:
        params["t"] = status.capability_token(named["record_id"])

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified status pages must retain their legacy name path")

    monkeypatch.setattr(backend_app.pages, "lookup_checkin_status", forbidden)
    response = app_client.get(page_url(named["name"]), params=params)
    assert response.status_code == 200 and response.template.name == "status_ready.html"
    assert response.context["tag"] == named["tag"]
    assert response.context["games"] == named["tournament_games_registered"]
    assert response.context["status"]["member"] is True
    assert response.context["status"]["startgg"] is True
    assert (
        "checkin_id" not in response.context and "status_token" not in response.context
    )


@pytest.mark.db
def test_verified_id_context_contains_no_contact_or_personal_identifier_fields(
    app_client, pg_db, backend_app, monkeypatch
):
    private_identifier = "0" * 12
    row = pg_db.seed_checkin(
        name=private_identifier,
        tag="Public synthetic tag",
        email="private@example.invalid",
        telephone="+12025550123",
        checkin_uuid="private-checkin-id",
        external_id="private-external-id",
        member=True,
        startgg=True,
    )
    forbid_name_lookups(backend_app, monkeypatch)
    response = app_client.get(
        page_url("Public synthetic label"), params=capability_params(row["record_id"])
    )
    assert response.status_code == 200 and response.template.name == "status_ready.html"
    assert response.context["tag"] == row["tag"]
    encoded_context = json.dumps(
        {key: value for key, value in response.context.items() if key != "request"}
    )
    for private in (
        private_identifier,
        row["email"],
        row["telephone"],
        row["checkin_uuid"],
        row["external_id"],
    ):
        assert private not in encoded_context and private not in response.text
    for field in (
        "email",
        "telephone",
        "personnummer",
        "_pnr_normalized",
        "checkin_uuid",
        "external_id",
    ):
        assert field not in response.context and field not in response.context["status"]


@pytest.mark.db
def test_id_lookup_failure_never_falls_back_to_another_player(
    app_client, pg_db, backend_app, monkeypatch, caplog
):
    row = pg_db.seed_checkin(
        name="Unavailable synthetic player", member=True, startgg=True
    )
    forbid_name_lookups(backend_app, monkeypatch)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic private error detail")

    monkeypatch.setattr(status.postgres_api, "get_checkin_by_record_id", unavailable)
    response = app_client.get(
        page_url(row["name"]), params=capability_params(row["record_id"])
    )
    assert (
        response.status_code == 200 and response.template.name == "status_pending.html"
    )
    assert response.context["found"] is False and response.context["games"] == []
    assert "Synthetic private error detail" not in response.text + caplog.text

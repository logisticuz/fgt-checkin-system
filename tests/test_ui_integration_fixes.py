"""T-034: public templates never render secrets, and the UI handles degraded/unknown membership."""

import itertools

import pytest

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

SENTINELS = {
    "n8n_token": "SENTINEL-N8N-6f1c",
    "sse_token": "SENTINEL-SSE-9a2e",
    "webhook_token": "SENTINEL-WEBHOOK-3b7d",
    "admin_auth_cookie_token": "SENTINEL-ADMIN-8c4f",
}
BASE = {
    "name": "Synthetic Player",
    "tag": "synthetic",
    "games": [],
    "event_slug": "synthetic-event",
    "event_name": "Synthetic Event",
    "status": {},
    "swish_number": "000 000 00 00",
    "swish_expected_per_game": 25,
    "require_membership": True,
    "require_payment": True,
    "require_startgg": True,
    "collect_acquisition_source": False,
    "offer_membership": True,
    "is_member": False,
    "membership_year": 2026,
    **SENTINELS,
}
VARIANTS = [
    ("checkin.html", {"kiosk": False, "membership_flow": "v2"}),
    ("checkin.html", {"kiosk": True, "membership_flow": "legacy"}),
    *[("register.html", {"membership_flow": flow}) for flow in ("v2", "legacy", "off")],
    *[
        (template, extra)
        for template, extra in itertools.product(
            ("status_pending.html", "status_ready.html"),
            ({}, {"checkin_id": "chk-1", "status_token": "tok-1"}),
        )
    ],
]


def render(app_templates, name, **extra):  # noqa: F811
    return app_templates.get_template(name).render({**BASE, **extra})


@pytest.mark.parametrize(
    "template, extra", VARIANTS, ids=[f"{t}-{i}" for i, (t, _) in enumerate(VARIANTS)]
)
def test_public_templates_never_render_secrets(app_templates, template, extra):  # noqa: F811
    html = render(app_templates, template, **extra)
    for key, sentinel in SENTINELS.items():
        assert sentinel not in html, f"{key} leaked into {template}"
    # Public status pages poll only: no SSE stream (which needed a token).
    assert "EventSource" not in html
    assert "/api/events/stream" not in html
    assert "WEBHOOK_TOKEN" not in html and "SSE_TOKEN" not in html


def test_checkin_turns_an_unavailable_ebas_into_unknown_membership(app_templates):  # noqa: F811
    html = render(app_templates, "checkin.html", kiosk=False, membership_flow="v2")
    assert 'ebasState === "unavailable" || ebasState === "skipped"' in html
    assert '(data.member === null && data.degraded === true)' in html
    assert 'membershipUnknown ? "" : "ebas=true"' in html
    assert 'membershipUnknown ? ["membership=unknown"] : []' in html


def test_register_keeps_membership_unknown_and_shows_the_to_message(app_templates):  # noqa: F811
    html = render(app_templates, "register.html", membership_flow="v2")
    assert 'params.get("membership") === "unknown"' in html
    assert "Verification temporarily unavailable – a TO will help you" in html
    assert "!membershipUnknown && params.get(\"ebas\") === \"true\"" in html
    assert 'setStatusUnknown("status-member")' in html


def test_register_saves_games_by_checkin_id_and_renders_messages_as_text(
    app_templates,
):  # noqa: F811
    html = render(app_templates, "register.html", membership_flow="v2")
    script = html[html.index('document.getElementById("btn-confirm-games")') :][:3000]
    assert "checkin_id: checkinId, games: selectedNames" in script
    assert "serverErrorText(err" in script
    helper = html[html.index("function showMessage") :][:400]
    assert "message.textContent = text" in helper and "innerHTML" not in helper
    assert 'localStorage.setItem("selectedGames"' not in html


def test_unknown_membership_reaches_the_status_page(app_templates):  # noqa: F811
    register = render(app_templates, "register.html", membership_flow="v2")
    helper = register[register.index("function statusQuery()") :][:900]
    assert 'membershipUnknown ? "membership=unknown" : ""' in helper
    pending = render(app_templates, "status_pending.html")
    assert 'get("membership") === "unknown"' in pending
    assert "? Membership: unknown – a TO will help" in pending


def test_t040_ownership_unverified_and_legacy_capability(app_templates):  # noqa: F811
    checkin = render(app_templates, "checkin.html", kiosk=False, membership_flow="v2")
    assert 'data.ownership === "unverified" || (data.checkin_id && !data.status_token)' in checkin
    register = render(app_templates, "register.html", membership_flow="legacy")
    assert "const ownershipUnverified" in register
    assert "checkin_id: ownedCheckinId, status_token: ownedToken" in register
    assert register.count('"Ask a TO to help you continue"') >= 3  # page, games, legacy POST


def test_t040_manual_review_is_terminal_in_the_v2_script():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "backend" / "static" / "js" / "membership_v2.js"
    ).read_text(encoding="utf-8")
    assert (
        script.count('if (isManualReview(data)) { showHelp("help_manual_title", "help_manual")')
        == 3
    )
    assert "Your registration was received – a TO will finish it with you" in script

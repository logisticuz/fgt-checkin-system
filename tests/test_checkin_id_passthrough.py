"""checkin_id passthrough in the check-in/status templates (T-020). Behaviour is covered by e2e."""

import re
from pathlib import Path

import pytest

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
OLD_MEMBER_LINK = (
    '<a href="/register?name=Synthetic%20Player&tag=synthetic&event=synthetic-event'
    '&membership_only=true" class="support-link">'
)
STATUS_CONTEXT = {
    "name": "Synthetic Player",
    "tag": "synthetic",
    "games": [],
    "event_slug": "synthetic-event",
    "event_name": "Synthetic Event",
    "kiosk": False,
    "status": {},
    "n8n_token": "",
    "sse_token": "",
    "swish_number": "000 000 00 00",
    "swish_expected_per_game": 25,
    "require_membership": False,
    "require_payment": False,
    "require_startgg": False,
    "is_member": False,
}


def render(app_templates, name, **overrides):  # noqa: F811
    return app_templates.get_template(name).render({**STATUS_CONTEXT, **overrides})


def test_ready_page_member_link_is_unchanged_and_gets_the_id_appended(app_templates):  # noqa: F811
    html = render(app_templates, "status_ready.html", offer_membership=True)
    # The server-rendered link is byte-for-byte the old one ...
    assert OLD_MEMBER_LINK in html
    # ... and a small script appends the opaque id from the page URL, only when present.
    script = html[html.index(OLD_MEMBER_LINK) :]
    assert 'new URLSearchParams(window.location.search).get("checkin_id")' in script
    assert "if (!checkinId || !link) return;" in script
    assert '"&checkin_id=" + encodeURIComponent(checkinId)' in script


@pytest.mark.parametrize("overrides", [{"offer_membership": False}, {"is_member": True}])
def test_ready_page_without_member_offer_has_no_link_or_script(
    app_templates, overrides
):  # noqa: F811
    html = render(app_templates, "status_ready.html", **{"offer_membership": True, **overrides})
    assert 'class="support-link"' not in html
    assert "checkin_id" not in html


def test_pending_page_keeps_kiosk_and_id_on_ready_redirect(app_templates):  # noqa: F811
    html = render(app_templates, "status_pending.html")
    block = html[html.index("// Keep the opaque checkin_id") :][:600]
    assert 'new URLSearchParams(window.location.search).get("checkin_id")' in block
    assert 'KIOSK_MODE ? "kiosk=true" : ""' in block
    assert "encodeURIComponent(checkinId)" in block
    assert '${statusQs ? "?" + statusQs : ""}' in block


def test_checkin_page_adds_the_orchestrate_id_to_both_navigations(app_templates):  # noqa: F811
    html = render(
        app_templates, "checkin.html", membership_flow="v2", collect_acquisition_source=False
    )
    assert re.search(
        r"data\.checkin_id\s*\?\s*`checkin_id=\$\{encodeURIComponent\(String\(data\.checkin_id\)\)\}`",
        html,
    )  # whitespace-insensitive: indentation may change with refactors
    status_match = re.search(r"const statusQs = \[(.*?)\]", html)
    assert status_match
    status_nav = status_match.group(1)
    assert status_nav == 'KIOSK_MODE ? "kiosk=true" : "", checkinIdQ'
    register_match = re.search(r"const qs = `(.*?)`;", html)
    assert register_match
    register_qs = register_match.group(1)
    assert register_qs.endswith('${checkinIdQ ? "&" + checkinIdQ : ""}')
    # Only opaque/flow data goes into these URLs: never personnummer or contact details.
    for forbidden in ("personnummer", "telefon", "email", "phone"):
        assert forbidden not in register_qs
        assert forbidden not in status_nav


@pytest.mark.parametrize("flow", ["v2", "legacy"])
def test_register_status_redirects_carry_the_id(app_templates, flow):  # noqa: F811
    html = render(app_templates, "register.html", membership_flow=flow, games=[])
    helper = html[html.index("function statusQuery()") :][:400]
    assert 'params.get("checkin_id")' in helper
    assert "encodeURIComponent(checkinId)" in helper
    # Both register.html redirects (legacy membership_only + "Continue") use the helper.
    assert html.count("${statusQuery()}`") == 2
    assert 'const kioskParam = KIOSK_MODE ? "?kiosk=true" : "";' not in html


def test_v2_membership_only_redirect_carries_the_id():
    script = (BACKEND_DIR / "static" / "js" / "membership_v2.js").read_text(encoding="utf-8")
    redirect = script[script.index("if (membershipOnly) {") :][:600]
    assert 'CHECKIN_ID ? "checkin_id=" + encodeURIComponent(CHECKIN_ID) : ""' in redirect

"""T-032: status pages use the checkin_id + capability token endpoint when the server verified it."""

import json
import re

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

ID_ENDPOINT = "/api/checkin/${encodeURIComponent(CHECKIN_ID)}/status?t="
NAME_ENDPOINT = "/api/participant/"
HOSTILE = "x</script><img src=x onerror=1>&"
BASE = {
    "name": "Synthetic Player",
    "tag": "synthetic",
    "games": [],
    "event_slug": "synthetic-event",
    "event_name": "Synthetic Event",
    "kiosk": False,
    "status": {},
    "sse_token": "",
    "swish_number": "000 000 00 00",
    "swish_expected_per_game": 25,
    "require_membership": False,
    "require_payment": True,
    "require_startgg": True,
    "offer_membership": True,
    "is_member": False,
}


def render(app_templates, name, **overrides):  # noqa: F811
    return app_templates.get_template(name).render({**BASE, **overrides})


def json_const(html, name):
    match = re.search(r"const %s = (.*?);\n" % name, html)
    assert match, name
    return json.loads(match.group(1))


def test_pending_with_capability_polls_only_the_id_endpoint(app_templates):  # noqa: F811
    html = render(app_templates, "status_pending.html", checkin_id="chk-1", status_token="tok.1")
    assert ID_ENDPOINT in html
    assert NAME_ENDPOINT not in html  # no name-based status URL at all
    assert json_const(html, "CHECKIN_ID") == "chk-1"
    assert json_const(html, "STATUS_TOKEN") == "tok.1"
    assert "Your status link expired – ask a TO or check in again" in html
    assert '`t=${encodeURIComponent(STATUS_TOKEN)}`' in html  # carried to the Ready page


def test_pending_without_capability_keeps_the_legacy_name_lookup(app_templates):  # noqa: F811
    html = render(app_templates, "status_pending.html")
    assert NAME_ENDPOINT + "${encodeURIComponent(name)}/status" in html
    assert "/api/checkin/" not in html
    assert json_const(html, "CHECKIN_ID") == ""
    assert json_const(html, "STATUS_TOKEN") == ""


def test_capability_values_are_safe_literals(app_templates):  # noqa: F811
    html = render(app_templates, "status_pending.html", checkin_id=HOSTILE, status_token=HOSTILE)
    assert HOSTILE not in html
    assert json_const(html, "CHECKIN_ID") == HOSTILE
    assert json_const(html, "STATUS_TOKEN") == HOSTILE


def test_ready_page_with_capability_links_and_confirms_by_id(app_templates):  # noqa: F811
    html = render(app_templates, "status_ready.html", checkin_id="chk 1", status_token="t/1")
    # Jinja's urlencode keeps "/" (it's safe in a query value).
    assert '&membership_only=true&checkin_id=chk%201&t=t/1" class="support-link"' in html
    assert "confirmById" in html and "/api/checkin/" in html
    assert "Legacy links (no verified capability" not in html
    assert 'id="status-check-notice"' in html


def test_ready_page_without_capability_is_unchanged(app_templates):  # noqa: F811
    html = render(app_templates, "status_ready.html")
    assert (
        '<a href="/register?name=Synthetic%20Player&tag=synthetic&event=synthetic-event'
        '&membership_only=true" class="support-link">' in html
    )
    assert "confirmById" not in html and "/api/checkin/" not in html
    assert "Legacy links (no verified capability" in html


def test_checkin_and_register_carry_the_token(app_templates):  # noqa: F811
    checkin = render(
        app_templates, "checkin.html", membership_flow="v2", collect_acquisition_source=False
    )
    assert "&t=${encodeURIComponent(String(data.status_token))}" in checkin
    assert "sessionStorage.setItem" not in checkin.replace('sessionStorage.setItem("kioskMode"', "")
    register = render(app_templates, "register.html", membership_flow="v2")
    helper = register[register.index("function statusQuery()") :][:600]
    assert 'params.get("t")' in helper
    assert "checkinId && statusToken ? `t=${encodeURIComponent(statusToken)}`" in helper


SUCCESS_TAGS = re.compile(r"<[a-z0-9]+ [^>]*\bdata-success\b[^>]*>")


def test_ready_page_hides_all_success_ui_until_the_id_confirms(app_templates):  # noqa: F811
    html = render(
        app_templates, "status_ready.html", checkin_id="chk-1", status_token="tok", games=["G"]
    )
    tags = SUCCESS_TAGS.findall(html)
    assert len(tags) == 7  # ✓, Ready, heading, tag, thank-you, games, member offer
    assert all(tag.endswith(" hidden>") for tag in tags), tags
    assert "<audio id=\"readySound\">" in html  # no autoplay before confirmation
    assert "Confirming your check-in…" in html


def test_ready_page_without_capability_shows_success_and_sound_as_before(
    app_templates,
):  # noqa: F811
    html = render(app_templates, "status_ready.html", games=["G"])
    tags = SUCCESS_TAGS.findall(html)
    assert tags and not any(" hidden" in tag for tag in tags)
    assert "<audio id=\"readySound\" autoplay>" in html
    assert "status-check-notice\"" not in html.split("</style>")[1]


def test_v2_membership_only_redirect_reuses_status_query():
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "backend" / "static" / "js" / "membership_v2.js"
    ).read_text(encoding="utf-8")
    redirect = script[script.index("if (membershipOnly) {") :][:1400]
    assert 'typeof statusQuery === "function"' in redirect
    assert '"t=" + encodeURIComponent(token)' in redirect

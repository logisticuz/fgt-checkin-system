"""Static checks for the membership v2 kiosk step (T-013): template wiring and hygiene rules."""

import re
from pathlib import Path

import pytest

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
PARTIAL = BACKEND_DIR / "templates" / "partials" / "membership_v2.html"
SCRIPT = BACKEND_DIR / "static" / "js" / "membership_v2.js"
LEGAL_DIR = BACKEND_DIR / "templates" / "legal"
BYLAWS_VERSION = "2026-11-06"
PRIVACY_VERSION = "2026-11-06-ebas+app-v1"


def render_register(app_templates, flow, **overrides):  # noqa: F811
    context = {
        "membership_flow": flow,
        "n8n_token": "",
        "swish_number": "000 000 00 00",
        "swish_expected_per_game": 25,
        "games": [],
        "require_payment": True,
        "require_membership": True,
        "require_startgg": True,
    }
    context.update(overrides)
    return app_templates.get_template("register.html").render(context)


def tag(html, element_id):
    match = re.search(r"<(?:input|button|section)\b[^>]*\bid=\"%s\"[^>]*>" % element_id, html)
    assert match, f"#{element_id} not rendered"
    return match.group(0)


def test_v2_renders_partial_and_parks_legacy_form(app_templates):  # noqa: F811
    html = render_register(app_templates, "v2", membership_year=2026)

    assert html.count('id="section-ebas"') == 1
    assert 'data-membership-flow="v2"' in tag(html, "section-ebas")
    legacy = tag(html, "section-ebas-legacy")
    assert "inert" in legacy and "display: none" in legacy
    assert '<script src="/static/js/membership_v2.js"></script>' in html
    # The partial (and its script) must come before the inline script that reads localStorage.
    assert html.index("membership_v2.js") < html.index("/static/js/validation.js")


@pytest.mark.parametrize("flow", ["legacy", "off"])
def test_other_flows_do_not_include_v2(app_templates, flow):  # noqa: F811
    html = render_register(app_templates, flow)

    assert "membership_v2.js" not in html
    assert "mv2-" not in html
    assert "section-ebas-legacy" not in html
    section = tag(html, "section-ebas")
    assert ("inert" in section) is (flow == "off")


def test_personnummer_and_code_inputs_follow_kiosk_rules(app_templates):  # noqa: F811
    html = render_register(app_templates, "v2")

    pnr = tag(html, "mv2-pnr")
    assert 'autocomplete="off"' in pnr
    assert 'inputmode="numeric"' in pnr
    code = tag(html, "mv2-code")
    for attribute in ('inputmode="numeric"', 'autocomplete="one-time-code"', 'maxlength="6"'):
        assert attribute in code
    # "Become a member" starts disabled until both consent boxes are ticked.
    assert "disabled" in tag(html, "mv2-start")
    for element_id in ("mv2-email", "mv2-phone"):
        assert "required" in tag(html, element_id)
    assert 'value="sms" checked' in html


def test_no_dynamic_html_injection_or_pii_storage():
    script = SCRIPT.read_text(encoding="utf-8")
    partial = PARTIAL.read_text(encoding="utf-8")

    for source in (script, partial):
        assert "innerHTML" not in source
        assert "insertAdjacentHTML" not in source
        assert "outerHTML" not in source
        assert "document.write" not in source
    assert "localStorage.setItem" not in script
    # sessionStorage only ever holds the attempt id.
    assert re.findall(r"sessionStorage\.setItem\(([^,]+),", script) == ["ATTEMPT_KEY"]
    assert 'const ATTEMPT_KEY = "mv2_attempt_id";' in script
    # Stale PII from older flows is removed on load.
    assert '["personnummer", "telefon"].forEach' in script
    # The step never shows a name (the API never returns one).
    assert "{{ name" not in partial and ".name" not in script.replace("state.name", "")


def test_consent_payload_uses_strict_booleans_and_rendered_versions(app_templates):  # noqa: F811
    script = SCRIPT.read_text(encoding="utf-8")
    assert "accepted_bylaws: el.bylaws.checked === true" in script
    assert "accepted_privacy: el.privacy.checked === true" in script

    html = render_register(app_templates, "v2", membership_year=2026)
    versions = dict(re.findall(r'data-legal-doc="([\w-]+)" data-legal-version="([^"]+)"', html))
    assert versions == {
        "bylaws": BYLAWS_VERSION,
        "privacy": PRIVACY_VERSION,
        "app-data": PRIVACY_VERSION,
    }
    # The JS reads the first match per doc, so EN and SV must carry the same version.
    for doc, version in (("bylaws", BYLAWS_VERSION), ("privacy", PRIVACY_VERSION)):
        found = re.findall(r'data-legal-doc="%s" data-legal-version="([^"]+)"' % doc, html)
        assert found == [version, version]


def test_legal_texts_render_year_todos_and_agenda(app_templates):  # noqa: F811
    html = render_register(app_templates, "v2", membership_year=2026)
    assert "registered for 2026." in html
    assert "medlemsskap i föreningen under 2026." in html
    assert "[membership year]" not in html
    # Unresolved values from the app-data draft stay visible as TODO markers.
    for todo in ("[or email]", "[SMS provider]"):
        assert f'<mark class="legal-todo">{todo}</mark>' in html
    assert "delete your data: info@fgctrollhattan.se." in html  # user decision 2026-11-06
    assert "[board contact email]" not in html
    sv_bylaws = (LEGAL_DIR / "bylaws.sv.html").read_text(encoding="utf-8")
    agenda_match = re.search(r"<ol>(.*?)</ol>", sv_bylaws, re.S)
    assert agenda_match
    agenda = agenda_match.group(1)
    assert agenda.count("<li>") == 16
    assert "<li>mötets öppnande</li>" in agenda and "<li>mötets avslutande</li>" in agenda
    assert "Show Swedish original" in html
    assert "The Swedish version adopted by the annual meeting is the legally binding one." in html

    missing_year = render_register(app_templates, "v2")
    assert '<mark class="legal-todo">[membership year]</mark>' in missing_year

    hostile = render_register(app_templates, "v2", membership_year="<script>x</script>")
    assert "<script>x</script>" not in hostile

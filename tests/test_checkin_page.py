"""Static checks for the check-in page's kiosk hygiene and accessibility (T-025)."""

import re
from pathlib import Path

import pytest

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
HYGIENE_JS = BACKEND_DIR / "static" / "js" / "checkin_hygiene.js"
PERSONAL_FIELDS = ("namn", "telefon", "tag", "ci-pnr-last4", "ci-pnr-manual-input")


@pytest.fixture
def checkin_html(app_templates):  # noqa: F811
    context = {
        "membership_flow": "v2",
        "n8n_token": "synthetic-token",
        "kiosk": True,
        "collect_acquisition_source": False,
        "require_membership": True,
        "require_payment": False,
        "require_startgg": False,
    }
    return app_templates.get_template("checkin.html").render(context)


def element(html, element_id):
    match = re.search(r'<(?:input|div|select|form)\b[^>]*\bid="%s"[^>]*>' % element_id, html)
    assert match, f"#{element_id} not rendered"
    return match.group(0)


def test_personal_fields_have_autocomplete_off(checkin_html):
    assert 'autocomplete="off"' in element(checkin_html, "checkin-form")
    for field in PERSONAL_FIELDS:
        assert 'autocomplete="off"' in element(checkin_html, field), field


def test_no_personal_data_is_written_to_local_storage(checkin_html):
    assert "localStorage.setItem" not in checkin_html
    hygiene = HYGIENE_JS.read_text(encoding="utf-8")
    assert "localStorage.setItem" not in hygiene and "sessionStorage.setItem" not in hygiene
    # Old keys are removed on load (the script loads before the page's inline script).
    for key in ("namn", "telefon", "tag", "eventSlug", "personnummer", "selectedGames"):
        assert f'"{key}"' in hygiene
    assert checkin_html.index("checkin_hygiene.js") < checkin_html.index("CheckinHygiene.install")


def test_unused_webhook_token_is_gone(checkin_html):
    assert "WEBHOOK_TOKEN" not in checkin_html
    assert "tokenParam" not in checkin_html
    assert "synthetic-token" not in checkin_html


def test_live_regions_and_personal_id_labels(checkin_html):
    assert 'aria-live="polite"' in element(checkin_html, "error-container")
    assert 'aria-live="polite"' in element(checkin_html, "ci-pnr-error")
    group = element(checkin_html, "personnummer-group")
    assert 'role="group"' in group and 'aria-labelledby="pnr-label"' in group
    for field in (
        "ci-pnr-year",
        "ci-pnr-month",
        "ci-pnr-day",
        "ci-pnr-last4",
        "ci-pnr-manual-input",
    ):
        assert "aria-label=" in element(checkin_html, field), field


def test_hidden_personal_id_field_is_not_marked_required(checkin_html):
    # `required` on a hidden input never validates; the submit handler checks the visible fields.
    assert 'getElementById("personnummer").setAttribute("required"' not in checkin_html
    assert "personalIdProblem()" in checkin_html


def test_kiosk_minimum_font_size_rules_exist(checkin_html):
    assert 'document.body.classList.add("kiosk")' in checkin_html
    assert re.search(r"body\.kiosk label,[^{]*\{ font-size: 1rem; \}", checkin_html)


def test_error_ux_uses_text_and_the_maybe_registered_message(checkin_html):
    script = checkin_html[checkin_html.index("function showErrors") :]
    assert "Something went wrong" not in checkin_html
    assert "Your check-in may have been registered – check with a TO." in script
    assert "p.textContent = e" in script and "link.textContent" in script
    # Only static markup ever goes through innerHTML on this page.
    for match in re.finditer(r"\.innerHTML = (.+);", checkin_html):
        assert match.group(1).startswith("'") and "${" not in match.group(1), match.group(0)

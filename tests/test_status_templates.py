"""Exercise status templates with the app's real Jinja environment, fully offline."""

import importlib
import json
import re
from pathlib import Path

import httpx
import pytest
import requests
from markupsafe import escape

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
TEMPLATE_NAMES = ("status_pending.html", "status_ready.html")
HOSTILE_NAME = "O'Brien \\ </script><img src=x onerror=1>"
HOSTILE_GAME = "<img src=x onerror=alert(1)>"


@pytest.fixture
def app_templates(monkeypatch):
    monkeypatch.chdir(BACKEND_DIR)
    monkeypatch.syspath_prepend(str(BACKEND_DIR))
    monkeypatch.setenv("DATA_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic:synthetic@127.0.0.1:1/template_test")
    monkeypatch.setenv("INTEGRATION_ENGINE", "none")
    for name in (
        "STARTGG_CLIENT_ID",
        "STARTGG_CLIENT_SECRET",
        "STARTGG_API_KEY",
        "STARTGG_TOKEN",
        "N8N_WEBHOOK_TOKEN",
        "N8N_BASIC_AUTH_USER",
        "N8N_BASIC_AUTH_PASSWORD",
        "SSE_TOKEN",
        "ADMIN_AUTH_COOKIE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbid_external_call(*args, **kwargs):
        raise AssertionError("Template tests must not access a database or network.")

    monkeypatch.setattr(requests.Session, "request", forbid_external_call)
    monkeypatch.setattr(httpx.Client, "request", forbid_external_call)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbid_external_call)
    postgres = importlib.import_module("shared.postgres_api")
    monkeypatch.setattr(postgres, "_get_pool", forbid_external_call)
    return importlib.import_module("backend.main").templates


def render_status(app_templates, template_name, **overrides):
    context = {
        "name": HOSTILE_NAME,
        "tag": HOSTILE_NAME,
        "games": [HOSTILE_GAME],
        "event_slug": "synthetic-event",
        "event_name": "Synthetic event",
        "kiosk": False,
    }
    context.update(overrides)
    return app_templates.get_template(template_name).render(context)


def script_block(rendered):
    # Greedily include an injected closing tag so the safety assertion detects it.
    match = re.search(r"<script\b[^>]*>(.*)</script\s*>", rendered, re.IGNORECASE | re.DOTALL)
    assert match is not None
    return match.group(1)


def json_constant(script, name):
    match = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*(.+?);", script)
    assert match is not None
    return json.loads(match.group(1))


def test_pending_name_is_a_valid_json_string(app_templates):
    rendered = render_status(app_templates, "status_pending.html")
    assert json_constant(script_block(rendered), "name") == HOSTILE_NAME


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_hostile_inputs_cannot_end_script_block(app_templates, template_name):
    rendered = render_status(app_templates, template_name)
    assert "</script" not in script_block(rendered).lower()
    assert HOSTILE_GAME not in rendered


def test_ready_name_and_games_remain_escaped_html(app_templates):
    rendered = render_status(app_templates, "status_ready.html")
    assert str(escape(HOSTILE_NAME)) in rendered
    assert str(escape(HOSTILE_GAME)) in rendered


def test_swish_settings_use_json_and_preserve_number_cleanup(app_templates):
    number = '123-456 78 90"\\</script><img src=x onerror=1>'
    price = '25"\\</script><img src=x onerror=1>'
    rendered = render_status(
        app_templates,
        "status_pending.html",
        swish_number=number,
        swish_expected_per_game=price,
    )
    script = script_block(rendered)
    assert json_constant(script, "SWISH_NUMBER") == number.replace("-", "").replace(" ", "")
    assert json_constant(script, "SWISH_PRICE_PER_GAME") == price
    assert "</script" not in script.lower()


def test_default_swish_settings_keep_their_types(app_templates):
    script = script_block(render_status(app_templates, "status_pending.html"))
    assert json_constant(script, "SWISH_NUMBER") == "1234567890"
    assert json_constant(script, "SWISH_PRICE_PER_GAME") == 25


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_all_script_template_values_use_tojson(template_name):
    source = (BACKEND_DIR / "templates" / template_name).read_text(encoding="utf-8")
    expressions = re.findall(r"{{(.*?)}}", script_block(source), re.DOTALL)
    assert expressions
    assert all(re.search(r"\|\s*tojson\s*$", expression) for expression in expressions)


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_templates_do_not_insert_dynamic_html(template_name):
    source = (BACKEND_DIR / "templates" / template_name).read_text(encoding="utf-8")
    assignments = re.findall(r"\.\s*innerHTML\s*=\s*(.*?);", source, re.DOTALL)
    assert all(value.strip() in ('""', "''") for value in assignments)
    assert not re.search(r"\.\s*outerHTML\s*=|\.\s*insertAdjacentHTML\s*\(", source)

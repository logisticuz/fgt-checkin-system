"""Playwright harness for kiosk UI tests: real templates, mocked API, no server, no network.

Run with:  RUN_E2E=1 pytest -p no:cacheprovider tests/e2e
Screens:   also set E2E_SCREENSHOT_DIR=<dir> to write state screenshots.
"""

import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.test_status_templates import app_templates  # noqa: F401  (pytest fixture)

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
BASE_URL = "http://kiosk.test"
REGISTER_URL = BASE_URL + "/register?name=Synthetic%20Player&ebas=true&event=synthetic-event"
DESKTOP = {"width": 1280, "height": 800}
TABLET = {"width": 820, "height": 1180}


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: browser tests (Playwright); run with RUN_E2E=1")


def synthetic_personnummer(base="19000101001"):
    """A Luhn-valid but obviously synthetic personnummer (born 1900-01-01)."""
    ten = base[2:]
    total = 0
    for index, char in enumerate(ten):
        value = int(char) * (2 if index % 2 == 0 else 1)
        total += value - 9 if value >= 10 else value
    return base + str((10 - total % 10) % 10)


class MockApi:
    """Scripted responses per endpoint; the last one repeats until something new is queued."""

    ENDPOINTS = ("start", "verify", "resend", "status")

    def __init__(self):
        self.responses = {name: [] for name in self.ENDPOINTS}
        self.calls = {name: [] for name in self.ENDPOINTS}
        self.served_last = set()
        self.hold = set()
        self.pending = []

    def queue(self, endpoint, body, status=200, headers=None):
        if endpoint in self.served_last:  # the repeating answer is used up: replace it
            self.responses[endpoint].clear()
            self.served_last.discard(endpoint)
        self.responses[endpoint].append((status, body, headers or {}))
        return self

    def handle(self, route, endpoint):
        request = route.request
        self.calls[endpoint].append(
            request.post_data_json if request.method == "POST" else request.url
        )
        if endpoint in self.hold:
            self.pending.append((route, endpoint))
            return
        self._fulfill(route, endpoint)

    def release(self):
        pending, self.pending = self.pending, []
        for route, endpoint in pending:
            self._fulfill(route, endpoint)

    def _fulfill(self, route, endpoint):
        queue = self.responses[endpoint]
        assert queue, f"no mocked response for {endpoint}"
        if len(queue) > 1:
            status, body, headers = queue.pop(0)
        else:
            status, body, headers = queue[0]
            self.served_last.add(endpoint)
        route.fulfill(
            status=status,
            headers={"Content-Type": "application/json", **headers},
            body=json.dumps(body),
        )


# Module scope: sync Playwright owns an asyncio loop; it must close before other test
# modules call asyncio.run() (e.g. TestClient-based tests).
@pytest.fixture(scope="module")
def browser():
    if os.environ.get("RUN_E2E") != "1":
        pytest.skip("browser tests run only with RUN_E2E=1")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def register_html(app_templates):  # noqa: F811
    def render(**overrides):
        context = {
            "membership_flow": "v2",
            "membership_year": 2026,
            "n8n_token": "",
            "games": [],
            "require_payment": False,
            "require_membership": True,
            "require_startgg": False,
        }
        context.update(overrides)
        return app_templates.get_template("register.html").render(context)

    return render


@pytest.fixture
def kiosk(browser, register_html):
    """Factory: open the register page with a mocked API. Returns (page, api)."""
    contexts = []
    errors = []

    def open_page(viewport=DESKTOP, clock=False, url=REGISTER_URL, **template_overrides):
        api = MockApi()
        html = register_html(**template_overrides)
        context = browser.new_context(viewport=viewport, base_url=BASE_URL)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def router(route):
            path = urlparse(route.request.url).path
            if path == "/register":
                return route.fulfill(status=200, content_type="text/html", body=html)
            if path.startswith("/static/"):
                file = BACKEND_DIR / path.lstrip("/")
                if file.is_file():
                    kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return route.fulfill(status=200, content_type=kind, body=file.read_bytes())
                return route.fulfill(status=404, body="")
            match = re.fullmatch(r"/api/membership/(start|verify|resend)", path)
            if match:
                return api.handle(route, match.group(1))
            if re.fullmatch(r"/api/membership/[^/]+/status", path):
                return api.handle(route, "status")
            if path.startswith("/api/participant/"):
                return route.fulfill(status=404, content_type="application/json", body="{}")
            return route.abort()

        page.route("**/*", router)
        if clock:
            page.clock.install()
        page.goto(url)
        return page, api

    yield open_page
    for context in contexts:
        context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"

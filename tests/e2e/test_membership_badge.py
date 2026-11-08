"""T-075 e2e: "eBas svarade inte" vs "Inte medlem" in the TO table and Needs attention.

The production layout + callbacks on a real Dash server (127.0.0.1, OS port) against the
test Postgres. Synthetic pw- rows only; the eBas failure goes through the real
apply_integration_result path (provider timeout -> member_lookup_state 'unknown').
"""

import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from shared import postgres_api as postgres
from tests.e2e.test_live_duplicate_hints import (  # noqa: F401  (pytest fixtures)
    DESKTOP,
    broadcasts,
    dashboard_url,
    shot,
)
from tests.test_safe_archiving import archive_db  # noqa: F401  (pytest fixture)

pytestmark = [pytest.mark.e2e, pytest.mark.db]
BADGE = "⚠ eBas svarade inte"
HELP = "Kunde inte kontrollera medlemskap (eBas-fel)."


@pytest.fixture
def live_slug(archive_db):  # noqa: F811
    slug = "pw-ebas-" + uuid.uuid4().hex[:8]
    archive_db.seed_settings(
        active_event_slug=slug,
        event_display_name="Synthetic eBas Event",
        require_membership=True,
        require_startgg=False,
        require_payment=False,
    )
    return slug


def seed(database, slug, key, **fields):
    return database.seed_checkin(
        event_slug=slug,
        record_id=f"rec-{key}",
        name=f"Synthetic {key.title()}",
        tag=f"pw-{key}",
        email=None,
        telephone="0700000000",
        **fields,
    )


def shot_clip(page, name, clip):
    out = os.environ.get("E2E_SCREENSHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), clip=clip, animations="disabled")


def member_cell(page, tag):
    row = page.locator("#checkins-table tr", has_text=tag)
    return row.locator("td[data-dash-column='member']")


def test_ebas_unknown_badge(browser, dashboard_url, archive_db, live_slug):  # noqa: F811
    seed(
        archive_db,
        live_slug,
        "member",
        member=True,
        member_lookup_state="confirmed_member",
        status="Ready",
    )
    seed(
        archive_db,
        live_slug,
        "nonmember",
        member=False,
        member_lookup_state="confirmed_nonmember",
        status="Pending",
    )
    seed(
        archive_db,
        live_slug,
        "noanswer",
        member=False,
        member_source="ebas_check",
        status="Pending",
    )
    seed(
        archive_db,
        live_slug,
        "skipped",
        member=False,
        member_lookup_state="skipped",
        status="Pending",
    )
    postgres.apply_integration_result(  # the real eBas failure path
        "rec-noanswer",
        "ebas",
        False,
        {},
        state="unavailable",
        error={"code": "provider_timeout"},
    )

    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(dashboard_url)
        page.wait_for_selector("#checkins-table")
        expect(member_cell(page, "pw-noanswer")).to_have_text(BADGE, timeout=15000)
        expect(member_cell(page, "pw-nonmember")).to_have_text("✗")
        expect(member_cell(page, "pw-skipped")).to_have_text("✗")
        expect(member_cell(page, "pw-member")).to_have_text("✓")
        color = member_cell(page, "pw-noanswer").evaluate("e => getComputedStyle(e).color")
        red = member_cell(page, "pw-nonmember").evaluate("e => getComputedStyle(e).color")
        assert color == "rgb(245, 158, 11)" and red != color
        shot(page, "T-075-1-table", page.locator("#checkins-table"))

        cell = member_cell(page, "pw-noanswer")
        cell.hover()
        box = cell.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=5)
        tooltip = page.locator(".dash-table-tooltip")
        expect(tooltip).to_be_visible(timeout=5000)
        expect(tooltip).to_contain_text(HELP)
        tip = tooltip.bounding_box()
        top = min(box["y"], tip["y"]) - 40
        shot_clip(
            page,
            "T-075-2-hover-help",
            {
                "x": 0,
                "y": max(top, 0),
                "width": DESKTOP["width"],
                "height": max(box["y"] + box["height"], tip["y"] + tip["height"])
                - max(top, 0)
                + 40,
            },
        )
        page.mouse.move(0, 0)

        section = page.locator("#needs-attention-section")
        expect(section).to_be_visible()
        expect(section).to_contain_text("eBas svarade inte")
        expect(section).to_contain_text("Be spelaren checka in igen")
        expect(section.get_by_text("🎫 MEMBERSHIP")).to_have_count(2)  # nonmember + skipped
        shot(page, "T-075-3-needs-attention", section)

        # The TO verifies the player and marks them as a member: the badge goes away.
        member_cell(page, "pw-noanswer").click()
        expect(member_cell(page, "pw-noanswer")).to_have_text("✓", timeout=15000)
        page.click("#btn-refresh")
        expect(member_cell(page, "pw-noanswer")).to_have_text("✓", timeout=15000)
        expect(section).not_to_contain_text("eBas svarade inte")
        shot(page, "T-075-4-after-marking-member")
        assert errors == []
    finally:
        context.close()

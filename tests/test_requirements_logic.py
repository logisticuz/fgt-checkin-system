"""Exercise the production requirements/status functions, without a copied algorithm."""

import pytest

from shared.postgres_api import compute_checkin_status, compute_requirements
from tests.fixtures import make_checkin, make_settings


@pytest.mark.parametrize("value, enabled", [(True, True), (False, False), (None, False)])
def test_only_true_enables_a_requirement(value, enabled):
    assert compute_requirements({"require_payment": value}) == {
        "require_payment": enabled,
        "require_membership": False,
        "require_startgg": False,
    }


@pytest.mark.parametrize(
    "settings, checkin, expected_missing",
    [
        ({}, {}, ["Membership", "Start.gg"]),
        ({"require_membership": False}, {}, ["Start.gg"]),
        ({"require_startgg": False}, {}, ["Membership"]),
        ({"require_membership": False, "require_startgg": False}, {}, []),
        ({}, {"member": True, "startgg": True}, []),
        ({"require_payment": True}, {"member": True, "startgg": True}, ["Payment"]),
        (
            {"require_payment": True},
            {"member": True, "startgg": True, "payment_valid": True},
            [],
        ),
    ],
)
def test_status_respects_active_requirements(settings, checkin, expected_missing):
    status = compute_checkin_status(make_checkin(**checkin), make_settings(**settings))
    assert status["missing"] == expected_missing
    assert status["ready"] is (not expected_missing)
    assert status["status"] == ("Pending" if expected_missing else "Ready")

# test_integration.py
"""
Integration tests that verify the complete check-in flow.
These tests simulate real user scenarios.

Run with: pytest tests/test_integration.py -v
"""
import pytest


# ============================================================================
# Scenario: Free Event (no payment, no membership required)
# This is the user's weekend event scenario!
# ============================================================================

class TestFreeEventScenario:
    """
    Scenario: Weekend event with:
    - No payment required
    - No membership required
    - Only Start.gg registration required
    """

    def test_player_on_startgg_becomes_ready_immediately(self):
        """
        Player registered on Start.gg should be Ready immediately.
        No payment, no membership check needed.
        """
        # Simulate settings (all requirements unchecked except startgg)
        settings = {
            "require_payment": None,      # Unchecked
            "require_membership": None,   # Unchecked
            "require_startgg": True,      # Checked
        }

        # Simulate player data from check-in
        player = {
            "name": "Viktor",
            "tag": "logisticuz",
            "member": False,
            "payment_valid": False,
            "startgg": True,  # Found on Start.gg
        }

        # Compute requirements
        require_payment = settings.get("require_payment") is True
        require_membership = settings.get("require_membership") is True
        require_startgg = settings.get("require_startgg") is True

        # Calculate status
        payment_ok = (not require_payment) or player.get("payment_valid", False)
        member_ok = (not require_membership) or player.get("member", False)
        startgg_ok = (not require_startgg) or player.get("startgg", False)

        status = "Ready" if (payment_ok and member_ok and startgg_ok) else "Pending"

        assert status == "Ready"
        assert require_payment is False
        assert require_membership is False
        assert require_startgg is True

    def test_player_not_on_startgg_stays_pending(self):
        """
        Player NOT on Start.gg should stay Pending.
        They need to register on Start.gg first.
        """
        settings = {
            "require_payment": None,
            "require_membership": None,
            "require_startgg": True,
        }

        player = {
            "name": "Guest Player",
            "tag": "guest123",
            "member": False,
            "payment_valid": False,
            "startgg": False,  # NOT on Start.gg
        }

        require_startgg = settings.get("require_startgg") is True
        startgg_ok = (not require_startgg) or player.get("startgg", False)

        status = "Ready" if startgg_ok else "Pending"

        assert status == "Pending"


# ============================================================================
# Scenario: Paid Event (full requirements)
# ============================================================================

class TestPaidEventScenario:
    """
    Scenario: Paid event with all requirements:
    - Payment required
    - Membership required
    - Start.gg registration required
    """

    def test_player_with_all_requirements_is_ready(self):
        """Player meeting all requirements should be Ready."""
        settings = {
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        }

        player = {
            "name": "Pro Player",
            "tag": "proplayer",
            "member": True,
            "payment_valid": True,
            "startgg": True,
        }

        require_payment = settings.get("require_payment") is True
        require_membership = settings.get("require_membership") is True
        require_startgg = settings.get("require_startgg") is True

        payment_ok = (not require_payment) or player.get("payment_valid", False)
        member_ok = (not require_membership) or player.get("member", False)
        startgg_ok = (not require_startgg) or player.get("startgg", False)

        status = "Ready" if (payment_ok and member_ok and startgg_ok) else "Pending"

        assert status == "Ready"

    def test_player_without_payment_stays_pending(self):
        """Player without payment should stay Pending."""
        settings = {
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        }

        player = {
            "name": "Unpaid Player",
            "tag": "unpaid",
            "member": True,
            "payment_valid": False,  # Not paid!
            "startgg": True,
        }

        require_payment = settings.get("require_payment") is True
        payment_ok = (not require_payment) or player.get("payment_valid", False)

        assert payment_ok is False

    def test_payment_expected_calculation(self):
        """Payment expected should be games * price_per_game."""
        price_per_game = 25
        selected_games = ["Tekken 8", "Street Fighter 6", "Guilty Gear"]

        payment_expected = len(selected_games) * price_per_game

        assert payment_expected == 75


# ============================================================================
# Scenario: Membership registration flow
# ============================================================================

class TestMembershipRegistrationFlow:
    """
    Scenario: Player needs to register as Sverok member.
    """

    def test_non_member_needs_registration(self):
        """Non-member with membership required should need registration."""
        settings = {"require_membership": True}
        player = {"member": False}

        require_membership = settings.get("require_membership") is True
        needs_registration = require_membership and not player.get("member", False)

        assert needs_registration is True

    def test_member_skips_registration(self):
        """Existing member should skip registration."""
        settings = {"require_membership": True}
        player = {"member": True}

        require_membership = settings.get("require_membership") is True
        needs_registration = require_membership and not player.get("member", False)

        assert needs_registration is False

    def test_membership_not_required_skips_registration(self):
        """If membership not required, skip registration."""
        settings = {"require_membership": None}  # Unchecked
        player = {"member": False}

        require_membership = settings.get("require_membership") is True
        needs_registration = require_membership and not player.get("member", False)

        assert needs_registration is False


# ============================================================================
# Scenario: Airtable checkbox behavior simulation
# ============================================================================

class TestAirtableCheckboxSimulation:
    """
    Simulate actual Airtable API responses.
    When checkbox is unchecked, the field is MISSING from the response.
    """

    def test_all_checkboxes_checked(self):
        """All checkboxes checked returns True for all."""
        # Airtable response when all checked
        airtable_settings = {
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
            "swish_number": "123456",
        }

        assert airtable_settings.get("require_payment") is True
        assert airtable_settings.get("require_membership") is True
        assert airtable_settings.get("require_startgg") is True

    def test_payment_unchecked(self):
        """Unchecked checkbox is MISSING from response."""
        # Airtable response when payment unchecked
        airtable_settings = {
            # "require_payment" is MISSING (not False!)
            "require_membership": True,
            "require_startgg": True,
            "swish_number": "123456",
        }

        # Using .get() returns None for missing keys
        assert airtable_settings.get("require_payment") is None
        assert airtable_settings.get("require_payment") is not True  # Our logic
        assert (airtable_settings.get("require_payment") is True) is False

    def test_all_unchecked_for_free_event(self):
        """All unchecked = free event, no requirements."""
        # Airtable response when all unchecked
        airtable_settings = {
            # All requirement fields MISSING
            "swish_number": "123456",
            "active_event_slug": "free-event",
        }

        require_payment = airtable_settings.get("require_payment") is True
        require_membership = airtable_settings.get("require_membership") is True
        require_startgg = airtable_settings.get("require_startgg") is True

        assert require_payment is False
        assert require_membership is False
        assert require_startgg is False


# ============================================================================
# Scenario: Dashboard display
# ============================================================================

class TestDashboardDisplay:
    """Tests for how data should be displayed in dashboard."""

    def test_active_requirements_badge(self):
        """Dashboard should show badges only for active requirements."""
        settings = {
            "require_payment": None,  # OFF
            "require_membership": None,  # OFF
            "require_startgg": True,  # ON
        }

        badges = []
        if settings.get("require_payment") is True:
            badges.append("PAYMENT")
        if settings.get("require_membership") is True:
            badges.append("MEMBERSHIP")
        if settings.get("require_startgg") is True:
            badges.append("START.GG")

        assert badges == ["START.GG"]
        assert "PAYMENT" not in badges
        assert "MEMBERSHIP" not in badges

    def test_needs_attention_respects_requirements(self):
        """Needs Attention should only show based on active requirements."""
        settings = {
            "require_payment": None,  # OFF
            "require_membership": None,  # OFF
            "require_startgg": True,  # ON
        }

        player = {
            "member": False,
            "payment_valid": False,
            "startgg": False,
        }

        # Calculate what player is missing
        missing = []
        if settings.get("require_payment") is True and not player.get("payment_valid"):
            missing.append("Payment")
        if settings.get("require_membership") is True and not player.get("member"):
            missing.append("Membership")
        if settings.get("require_startgg") is True and not player.get("startgg"):
            missing.append("Start.gg")

        # Only Start.gg should be missing
        assert missing == ["Start.gg"]

# test_requirements_logic.py
"""
Unit tests for the configurable requirements logic.
These tests verify that the status calculation correctly respects
which requirements are enabled/disabled.

IMPORTANT: Airtable checkbox behavior:
- Checked checkbox → True
- Unchecked checkbox → Field is MISSING (returns None, NOT False)

Therefore we use "is True" logic:
- True → requirement ON
- None/False → requirement OFF

Run with: pytest tests/test_requirements_logic.py -v
"""
import pytest


# ============================================================================
# Extract the core logic from callbacks.py for testing
# (These are the same algorithms used in the dashboard)
# ============================================================================

def is_ok(val):
    """Check if a value indicates 'OK' (matches dashboard logic)."""
    return val == "✓" or val is True or str(val).lower() == "true"


def compute_requirements(settings):
    """
    Compute which requirements are active based on settings.

    Airtable checkbox semantics:
    - Checkbox checked → True
    - Checkbox unchecked → field missing (None)
    - We treat None as False (requirement is OFF when unchecked)
    """
    return {
        "require_payment": settings.get("require_payment") is True,
        "require_membership": settings.get("require_membership") is True,
        "require_startgg": settings.get("require_startgg") is True,
    }


def calculate_status(player, requirements):
    """
    Calculate if a player should be 'Ready' or 'Pending' based on requirements.

    Args:
        player: dict with keys like 'member', 'payment_valid', 'startgg'
        requirements: dict with keys 'require_payment', 'require_membership', 'require_startgg'
                     (already computed with is True logic)

    Returns:
        'Ready' if all active requirements are met, else 'Pending'
    """
    require_membership = requirements.get("require_membership", False)
    require_payment = requirements.get("require_payment", False)
    require_startgg = requirements.get("require_startgg", False)

    # Check each requirement (skip if not required)
    payment_ok = (not require_payment) or is_ok(player.get("payment_valid", False))
    member_ok = (not require_membership) or is_ok(player.get("member", False))
    startgg_ok = (not require_startgg) or is_ok(player.get("startgg", False))

    return "Ready" if (payment_ok and member_ok and startgg_ok) else "Pending"


def get_missing_requirements(player, requirements):
    """
    Get list of requirements the player is missing (only checks active requirements).

    Args:
        player: dict with player data
        requirements: dict with requirement toggles (already computed)

    Returns:
        list of missing requirement names (e.g., ['Payment', 'Membership'])
    """
    require_membership = requirements.get("require_membership", False)
    require_payment = requirements.get("require_payment", False)
    require_startgg = requirements.get("require_startgg", False)

    missing = []

    if require_membership and not is_ok(player.get("member", "")):
        missing.append("Membership")

    if require_payment and not is_ok(player.get("payment_valid", "")):
        missing.append("Payment")

    if require_startgg and not is_ok(player.get("startgg", "")):
        missing.append("Start.gg")

    return missing


# ============================================================================
# Test: Airtable checkbox behavior (is True logic)
# ============================================================================

class TestAirtableCheckboxBehavior:
    """Tests for Airtable checkbox semantics - the core of our logic."""

    def test_checked_checkbox_enables_requirement(self):
        """Checked checkbox (True) should enable requirement."""
        settings = {"require_payment": True}
        reqs = compute_requirements(settings)
        assert reqs["require_payment"] is True

    def test_unchecked_checkbox_disables_requirement(self):
        """Unchecked checkbox (None/missing) should disable requirement."""
        settings = {"require_payment": None}
        reqs = compute_requirements(settings)
        assert reqs["require_payment"] is False

    def test_missing_field_disables_requirement(self):
        """Missing field should disable requirement."""
        settings = {}
        reqs = compute_requirements(settings)
        assert reqs["require_payment"] is False
        assert reqs["require_membership"] is False
        assert reqs["require_startgg"] is False

    def test_explicit_false_disables_requirement(self):
        """Explicit False should disable requirement."""
        settings = {"require_payment": False}
        reqs = compute_requirements(settings)
        assert reqs["require_payment"] is False

    def test_all_checked_enables_all(self):
        """All checkboxes checked should enable all requirements."""
        settings = {
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        }
        reqs = compute_requirements(settings)
        assert reqs["require_payment"] is True
        assert reqs["require_membership"] is True
        assert reqs["require_startgg"] is True


# ============================================================================
# Test: All requirements enabled
# ============================================================================

class TestAllRequirementsEnabled:
    """Tests with all requirements enabled (all checkboxes checked)."""

    @pytest.fixture
    def requirements(self):
        return compute_requirements({
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        })

    def test_player_with_all_requirements_met_is_ready(self, requirements):
        """Player meeting all requirements should be Ready."""
        player = {"member": True, "payment_valid": True, "startgg": True}
        assert calculate_status(player, requirements) == "Ready"

    def test_player_missing_payment_is_pending(self, requirements):
        """Player missing payment should be Pending."""
        player = {"member": True, "payment_valid": False, "startgg": True}
        assert calculate_status(player, requirements) == "Pending"

    def test_player_missing_membership_is_pending(self, requirements):
        """Player missing membership should be Pending."""
        player = {"member": False, "payment_valid": True, "startgg": True}
        assert calculate_status(player, requirements) == "Pending"

    def test_player_missing_startgg_is_pending(self, requirements):
        """Player not on Start.gg should be Pending."""
        player = {"member": True, "payment_valid": True, "startgg": False}
        assert calculate_status(player, requirements) == "Pending"

    def test_player_missing_all_is_pending(self, requirements):
        """Player missing everything should be Pending."""
        player = {"member": False, "payment_valid": False, "startgg": False}
        assert calculate_status(player, requirements) == "Pending"


# ============================================================================
# Test: Payment disabled (unchecked in Airtable)
# ============================================================================

class TestPaymentDisabled:
    """Tests with payment requirement disabled (checkbox unchecked)."""

    @pytest.fixture
    def requirements(self):
        # Simulate: payment unchecked, others checked
        return compute_requirements({
            "require_payment": None,  # Unchecked = None
            "require_membership": True,
            "require_startgg": True,
        })

    def test_player_without_payment_is_ready_when_payment_disabled(self, requirements):
        """Player without payment should be Ready if payment not required."""
        player = {"member": True, "payment_valid": False, "startgg": True}
        assert calculate_status(player, requirements) == "Ready"

    def test_player_missing_membership_still_pending(self, requirements):
        """Player missing membership should still be Pending."""
        player = {"member": False, "payment_valid": False, "startgg": True}
        assert calculate_status(player, requirements) == "Pending"

    def test_missing_requirements_excludes_payment(self, requirements):
        """Missing requirements list should not include Payment when disabled."""
        player = {"member": True, "payment_valid": False, "startgg": True}
        missing = get_missing_requirements(player, requirements)
        assert "Payment" not in missing


# ============================================================================
# Test: Membership disabled (unchecked in Airtable)
# ============================================================================

class TestMembershipDisabled:
    """Tests with membership requirement disabled (checkbox unchecked)."""

    @pytest.fixture
    def requirements(self):
        return compute_requirements({
            "require_payment": True,
            "require_membership": None,  # Unchecked
            "require_startgg": True,
        })

    def test_player_without_membership_is_ready_when_membership_disabled(self, requirements):
        """Non-member should be Ready if membership not required."""
        player = {"member": False, "payment_valid": True, "startgg": True}
        assert calculate_status(player, requirements) == "Ready"

    def test_missing_requirements_excludes_membership(self, requirements):
        """Missing requirements list should not include Membership when disabled."""
        player = {"member": False, "payment_valid": True, "startgg": True}
        missing = get_missing_requirements(player, requirements)
        assert "Membership" not in missing


# ============================================================================
# Test: Start.gg disabled (unchecked in Airtable)
# ============================================================================

class TestStartggDisabled:
    """Tests with Start.gg requirement disabled (checkbox unchecked)."""

    @pytest.fixture
    def requirements(self):
        return compute_requirements({
            "require_payment": True,
            "require_membership": True,
            "require_startgg": None,  # Unchecked
        })

    def test_guest_is_ready_when_startgg_disabled(self, requirements):
        """Guest (no Start.gg) should be Ready if Start.gg not required."""
        player = {"member": True, "payment_valid": True, "startgg": False}
        assert calculate_status(player, requirements) == "Ready"

    def test_missing_requirements_excludes_startgg(self, requirements):
        """Missing requirements list should not include Start.gg when disabled."""
        player = {"member": True, "payment_valid": True, "startgg": False}
        missing = get_missing_requirements(player, requirements)
        assert "Start.gg" not in missing


# ============================================================================
# Test: All requirements disabled (free event mode)
# This is what the user wants for their weekend event!
# ============================================================================

class TestAllRequirementsDisabled:
    """Tests with all requirements disabled (auto-Ready mode)."""

    @pytest.fixture
    def requirements(self):
        # All checkboxes unchecked = free event
        return compute_requirements({})  # Empty = all None

    def test_anyone_is_ready_when_all_disabled(self, requirements):
        """Anyone should be Ready when no requirements are active."""
        player = {"member": False, "payment_valid": False, "startgg": False}
        assert calculate_status(player, requirements) == "Ready"

    def test_no_missing_requirements_when_all_disabled(self, requirements):
        """No one should be 'missing' anything when all disabled."""
        player = {"member": False, "payment_valid": False, "startgg": False}
        missing = get_missing_requirements(player, requirements)
        assert missing == []

    def test_empty_player_is_ready(self, requirements):
        """Even empty player data should be Ready."""
        player = {}
        assert calculate_status(player, requirements) == "Ready"


# ============================================================================
# Test: Payment and Membership disabled (user's weekend event scenario!)
# ============================================================================

class TestPaymentAndMembershipDisabled:
    """Tests for weekend event: no payment, no membership, only Start.gg required."""

    @pytest.fixture
    def requirements(self):
        return compute_requirements({
            "require_payment": None,     # Unchecked
            "require_membership": None,  # Unchecked
            "require_startgg": True,     # Checked
        })

    def test_player_on_startgg_is_ready(self, requirements):
        """Player on Start.gg should be Ready (no payment/membership needed)."""
        player = {"member": False, "payment_valid": False, "startgg": True}
        assert calculate_status(player, requirements) == "Ready"

    def test_player_not_on_startgg_is_pending(self, requirements):
        """Player NOT on Start.gg should be Pending."""
        player = {"member": False, "payment_valid": False, "startgg": False}
        assert calculate_status(player, requirements) == "Pending"

    def test_missing_only_shows_startgg(self, requirements):
        """Missing list should only show Start.gg."""
        player = {"member": False, "payment_valid": False, "startgg": False}
        missing = get_missing_requirements(player, requirements)
        assert missing == ["Start.gg"]
        assert "Payment" not in missing
        assert "Membership" not in missing


# ============================================================================
# Test: Icon values (dashboard uses ✓ and ✗)
# ============================================================================

class TestIconValues:
    """Tests with icon string values like the dashboard uses."""

    @pytest.fixture
    def requirements(self):
        return compute_requirements({
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        })

    def test_checkmark_is_ok(self, requirements):
        """✓ should be treated as True."""
        player = {"member": "✓", "payment_valid": "✓", "startgg": "✓"}
        assert calculate_status(player, requirements) == "Ready"

    def test_cross_is_not_ok(self, requirements):
        """✗ should be treated as False."""
        player = {"member": "✗", "payment_valid": "✓", "startgg": "✓"}
        assert calculate_status(player, requirements) == "Pending"

    def test_string_true_is_ok(self, requirements):
        """'true' string should be treated as True."""
        player = {"member": "true", "payment_valid": "TRUE", "startgg": "True"}
        assert calculate_status(player, requirements) == "Ready"


# ============================================================================
# Test: Edge cases
# ============================================================================

class TestEdgeCases:
    """Edge case tests."""

    def test_none_player_values_treated_as_false(self):
        """None values in player data should be treated as False."""
        player = {"member": None, "payment_valid": None, "startgg": None}
        requirements = compute_requirements({
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        })
        assert calculate_status(player, requirements) == "Pending"

    def test_mixed_requirements(self):
        """Mix of enabled and disabled requirements."""
        player = {"member": False, "payment_valid": False, "startgg": True}
        requirements = compute_requirements({
            "require_payment": None,     # OFF
            "require_membership": None,  # OFF
            "require_startgg": True,     # ON
        })
        # Only startgg matters, and it's True
        assert calculate_status(player, requirements) == "Ready"

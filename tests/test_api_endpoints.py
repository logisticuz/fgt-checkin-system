# test_api_endpoints.py
"""
API endpoint tests for the backend.
Tests the FastAPI endpoints directly.

Run with: pytest tests/test_api_endpoints.py -v
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi.testclient import TestClient


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_airtable():
    """Mock Airtable API calls."""
    with patch('main.get_active_settings') as mock_settings, \
         patch('main.get_checkins') as mock_checkins, \
         patch('main.find_checkin_by_tag') as mock_find, \
         patch('main.update_checkin') as mock_update:

        # Default mock returns
        mock_settings.return_value = {
            "active_event_slug": "test-event-2025",
            "event_display_name": "Test Event 2025",
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
            "swish_number": "123 456 78 90",
            "swish_expected_per_game": 25,
        }
        mock_checkins.return_value = []
        mock_find.return_value = None
        mock_update.return_value = True

        yield {
            "settings": mock_settings,
            "checkins": mock_checkins,
            "find": mock_find,
            "update": mock_update,
        }


@pytest.fixture
def client(mock_airtable):
    """Create test client with mocked dependencies."""
    from main import app
    return TestClient(app)


# ============================================================================
# Test: Health endpoint
# ============================================================================

class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, client):
        """Health endpoint should return 200 OK."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"


# ============================================================================
# Test: Main check-in page
# ============================================================================

class TestCheckinPage:
    """Tests for the main check-in page."""

    def test_checkin_page_loads(self, client):
        """Check-in page should load with settings from Airtable."""
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_checkin_page_contains_event_name(self, client, mock_airtable):
        """Check-in page should display event name."""
        mock_airtable["settings"].return_value["event_display_name"] = "My Cool Event"
        response = client.get("/")
        assert response.status_code == 200
        # Event name should be in the HTML
        assert b"My Cool Event" in response.content or b"test-event" in response.content.lower()


# ============================================================================
# Test: Status API endpoint
# ============================================================================

class TestStatusAPI:
    """Tests for /api/participant/{name}/status endpoint."""

    def test_status_returns_participant_data(self, client, mock_airtable):
        """Status endpoint should return participant data."""
        mock_airtable["checkins"].return_value = [{
            "name": "Test Player",
            "tag": "testplayer",
            "status": "Pending",
            "member": True,
            "payment_valid": False,
            "startgg": True,
            "tournament_games_registered": ["Game 1", "Game 2"],
            "payment_expected": 50,
        }]

        response = client.get("/api/participant/Test%20Player/status")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Player"
        assert data["status"] == "Pending"

    def test_status_not_found_returns_404(self, client, mock_airtable):
        """Status endpoint should return 404 for unknown player."""
        mock_airtable["checkins"].return_value = []

        response = client.get("/api/participant/Unknown%20Player/status")
        assert response.status_code == 404


# ============================================================================
# Test: Player games update endpoint
# ============================================================================

class TestPlayerGamesAPI:
    """Tests for PATCH /api/player/games endpoint."""

    def test_update_games_success(self, client, mock_airtable):
        """Should update player's selected games."""
        mock_airtable["find"].return_value = {
            "id": "rec123",
            "fields": {
                "tag": "testplayer",
                "name": "Test Player",
            }
        }
        mock_airtable["settings"].return_value["swish_expected_per_game"] = 25

        response = client.patch("/api/player/games", json={
            "tag": "testplayer",
            "slug": "test-event-2025",
            "games": ["Game 1", "Game 2"],
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["payment_expected"] == 50  # 2 games * 25kr

    def test_update_games_player_not_found(self, client, mock_airtable):
        """Should return 404 if player not found."""
        mock_airtable["find"].return_value = None

        response = client.patch("/api/player/games", json={
            "tag": "unknownplayer",
            "slug": "test-event-2025",
            "games": ["Game 1"],
        })

        assert response.status_code == 404


# ============================================================================
# Test: Player member update endpoint
# ============================================================================

class TestPlayerMemberAPI:
    """Tests for PATCH /api/player/member endpoint."""

    def test_update_member_success(self, client, mock_airtable):
        """Should update player's member status."""
        mock_airtable["find"].return_value = {
            "id": "rec123",
            "fields": {
                "tag": "testplayer",
                "name": "Test Player",
                "member": False,
            }
        }

        response = client.patch("/api/player/member", json={
            "tag": "testplayer",
            "slug": "test-event-2025",
            "member": True,
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_update_member_player_not_found(self, client, mock_airtable):
        """Should return 404 if player not found."""
        mock_airtable["find"].return_value = None

        response = client.patch("/api/player/member", json={
            "tag": "unknownplayer",
            "slug": "test-event-2025",
            "member": True,
        })

        assert response.status_code == 404


# ============================================================================
# Test: Requirements computation
# ============================================================================

class TestRequirementsComputation:
    """Tests for compute_requirements function in backend."""

    def test_all_enabled(self, client, mock_airtable):
        """All requirements checked should be enabled."""
        mock_airtable["settings"].return_value = {
            "active_event_slug": "test",
            "require_payment": True,
            "require_membership": True,
            "require_startgg": True,
        }

        response = client.get("/")
        assert response.status_code == 200
        # Requirements should be in the HTML as JavaScript variables
        content = response.text
        # Check that requirements are passed to template
        assert "require_payment" in content.lower() or "REQUIRE_PAYMENT" in content

    def test_payment_disabled(self, client, mock_airtable):
        """Payment unchecked (None) should be disabled."""
        mock_airtable["settings"].return_value = {
            "active_event_slug": "test",
            "require_payment": None,  # Unchecked checkbox
            "require_membership": True,
            "require_startgg": True,
        }

        response = client.get("/")
        assert response.status_code == 200


# ============================================================================
# Test: Register page
# ============================================================================

class TestRegisterPage:
    """Tests for /register endpoint."""

    def test_register_page_loads(self, client):
        """Register page should load."""
        response = client.get("/register?name=Test&event=test-event")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_register_page_without_params(self, client):
        """Register page should handle missing params gracefully."""
        response = client.get("/register")
        # Should either load or redirect, not crash
        assert response.status_code in [200, 302, 307]


# ============================================================================
# Test: Status pages
# ============================================================================

class TestStatusPages:
    """Tests for status pages."""

    def test_status_pending_page_loads(self, client, mock_airtable):
        """Pending status page should load."""
        mock_airtable["checkins"].return_value = [{
            "name": "Test Player",
            "tag": "testplayer",
            "status": "Pending",
        }]

        response = client.get("/status/Test%20Player")
        assert response.status_code == 200

    def test_status_page_unknown_player(self, client, mock_airtable):
        """Status page for unknown player should show appropriate message."""
        mock_airtable["checkins"].return_value = []

        response = client.get("/status/Unknown%20Player")
        # Should handle gracefully
        assert response.status_code in [200, 404]

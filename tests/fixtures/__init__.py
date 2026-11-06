"""Factories containing invented data only; every call returns a fresh dictionary."""

from itertools import count

_players = count(1)


def make_settings(**overrides):
    settings = {
        "is_active": True,
        "active_event_slug": "test-event",
        "event_display_name": "Test Event",
        "swish_expected_per_game": 25,
        "require_payment": False,
        "require_membership": True,
        "require_startgg": True,
        "offer_membership": True,
        "collect_acquisition_source": False,
        "events_json": [],
    }
    settings.update(overrides)
    return settings


def make_checkin(**overrides):
    number = next(_players)
    checkin = {
        "event_slug": "test-event",
        "name": f"Test Player {number}",
        "tag": f"test-player-{number}",
        "email": f"player-{number}@example.invalid",
        "telephone": "0000000000",
        "status": "Pending",
        "member": False,
        "startgg": False,
        "payment_valid": False,
        "payment_amount": 0,
        "payment_expected": 0,
        "tournament_games_registered": [],
    }
    checkin.update(overrides)
    return checkin

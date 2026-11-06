"""Membership mode is validated once at backend startup; restart to change it."""

import os
from typing import Literal, cast

MembershipFlow = Literal["v2", "legacy", "off"]
_configured_flow = os.environ.get("MEMBERSHIP_FLOW", "legacy")
if _configured_flow not in ("v2", "legacy", "off"):
    raise ValueError("Invalid MEMBERSHIP_FLOW: expected v2, legacy, or off")

_membership_flow = cast(MembershipFlow, _configured_flow)


def get_membership_flow() -> MembershipFlow:
    """Return the startup configuration without silently changing modes at runtime."""
    return _membership_flow

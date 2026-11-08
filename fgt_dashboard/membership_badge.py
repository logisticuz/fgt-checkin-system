"""How the TO dashboard shows a check-in's membership (T-075).

An eBas outage (timeout, 5xx, non-JSON) leaves ``member`` false with
``member_lookup_state = 'unknown'``. That must never look like a real non-member,
or the TO tells a paying member to register. This module is dependency free so the
table (callbacks), its styling (layout) and the tests share one mapping and one text.
"""

MEMBER = "member"
EBAS_UNKNOWN = "ebas_unknown"
NONMEMBER = "nonmember"  # eBas answered: not a member
MISSING = "missing"  # not checked (no personnummer, eBas skipped, legacy rows)

MEMBER_CELL = "✓"
MISSING_CELL = "✗"
EBAS_UNKNOWN_CELL = "⚠ eBas svarade inte"
EBAS_UNKNOWN_LABEL = "eBas svarade inte"
EBAS_UNKNOWN_HELP = (
    "Kunde inte kontrollera medlemskap (eBas-fel). Be spelaren checka in igen om en "
    "stund, eller verifiera och markera som medlem."
)


def _truthy(value) -> bool:
    return value is True or value == MEMBER_CELL or str(value).lower() == "true"


def membership_state(member, lookup_state, lookup_reported) -> str:
    """Classify one check-in row.

    ``lookup_reported`` says an eBas result was actually stored for the row. Storage
    reads a missing state as 'unknown', so without it a row where eBas was never asked
    (no personnummer) would wrongly claim that eBas failed. The badge also needs
    ``member`` to be explicitly False: an unknown member value never shows amber.
    """
    if _truthy(member):
        return MEMBER
    if member is False and lookup_reported is True and lookup_state == "unknown":
        return EBAS_UNKNOWN
    if lookup_state == "confirmed_nonmember":
        return NONMEMBER
    return MISSING


def member_cell(state: str) -> str:
    """The text in the TO table's Member column for a membership_state()."""
    if state == MEMBER:
        return MEMBER_CELL
    if state == EBAS_UNKNOWN:
        return EBAS_UNKNOWN_CELL
    return MISSING_CELL

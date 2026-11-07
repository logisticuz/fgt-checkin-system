"""Recompute archive-derived statistics. Default: a SELECT-only dry run.

Explicit prices are assertions, never guesses: --event-price slug=25 is per person,
slug=25/game uses recorded game counts. --compare-price slug=25 previews both bases
without choosing one. Free-event assertions affect expectations, not recorded amounts.
"""

import argparse
import json
import os
import sys
from collections import Counter
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_prices(values, *, compare=False):
    result = {}
    for value in values:
        slug, separator, text = value.partition("=")
        per_game = text.endswith("/game")
        if per_game:
            text = text[:-5]
        try:
            amount = Decimal(text)
            valid = amount.is_finite() and amount > 0 and amount < Decimal("100000000")
            valid = valid and amount == amount.quantize(Decimal("0.01"))
        except InvalidOperation:
            valid = False
        if not separator or not slug.strip() or not valid or (compare and per_game):
            raise ValueError("Prices must be slug=positive-amount[/game]; comparisons omit /game")
        if slug in result:
            raise ValueError("Each event price must be supplied once")
        result[slug] = (amount, per_game)
    return result


def _guard_apply(connection):
    """Freeze activation/check-in writes, then fail immediately on an active or busy archive."""
    try:
        connection.execute("LOCK TABLE settings, active_event_data IN SHARE MODE NOWAIT")
    except psycopg.errors.LockNotAvailable:
        raise ValueError(
            "Apply refused: settings/check-ins are busy; retry after the event is closed"
        ) from None
    locked = connection.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext('fgt:player-merges'))"
    ).fetchone()[0]
    if not locked:
        raise ValueError("Apply refused: archive/player maintenance is in progress")
    active = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM settings WHERE is_active = true AND nullif(active_event_slug, '') IS NOT NULL) "
        "OR EXISTS(SELECT 1 FROM active_event_data)"
    ).fetchone()[0]
    if active:
        raise ValueError(
            "Apply refused: an event or check-in rows are active; archive/close it first"
        )


def _reconciliation(connection, context, events, players):
    """Reconcile every profile and every archived payment; public results contain counts only."""
    profiles = {
        row[0]: (row[1] or 0, row[2] or Decimal(0))
        for row in connection.execute("SELECT uuid, total_events, total_paid FROM players")
    }
    planned = {item["player_uuid"]: item["after"] for item in players}
    rows = [row for attendance in context["events"].values() for row in attendance]
    paid = {}
    attended = set()
    for row in rows:
        attended.add(row["player_uuid"])
        paid[row["player_uuid"]] = paid.get(row["player_uuid"], Decimal(0)) + (
            row["payment_amount"] or Decimal(0)
        )
    never_new = set(profiles) - set(context["first"])
    reasons = Counter(
        "zero_archive_rows" if player not in attended else "undated_only" for player in never_new
    )
    archive_paid = sum((row["payment_amount"] or Decimal(0) for row in rows), Decimal(0))
    after_paid = sum(
        (
            planned[player]["total_paid"] if player in planned else before[1]
            for player, before in profiles.items()
        ),
        Decimal(0),
    )
    selected = {item["event_slug"]: item["after"] for item in events}
    event_paid = sum(
        (
            (selected.get(slug) or context["metadata"].get(slug, {})).get("total_revenue")
            or Decimal(0)
            for slug in set(context["events"]) | set(context["metadata"])
        ),
        Decimal(0),
    )
    unassigned = sum(
        bool(row["payment_amount"]) and row["player_uuid"] not in profiles for row in rows
    )
    return {
        "profiles": len(profiles),
        "never_new_profiles": {"count": len(never_new), "reasons": dict(sorted(reasons.items()))},
        "players_total_paid_before": sum((value[1] for value in profiles.values()), Decimal(0)),
        "players_total_paid_after": after_paid,
        "archive_payments_after": archive_paid,
        "event_revenue_after": event_paid,
        "players_total_events_before": sum(value[0] for value in profiles.values()),
        "players_total_events_after": sum(
            planned[player]["total_events"] if player in planned else before[0]
            for player, before in profiles.items()
        ),
        "per_player_payment_mismatches": sum(
            value[1] != paid.get(player, Decimal(0)) for player, value in profiles.items()
        ),
        "unassigned_payment_rows": unassigned,
        "balanced": after_paid == archive_paid == event_paid and not unassigned,
        "archive_rows": len(rows),
        "unique_attendances": sum(
            len({row["player_uuid"] for row in attendance if row["player_uuid"]})
            for attendance in context["events"].values()
        ),
        "global_first_arrivals": len(context["first"]),
    }


def repair(
    *,
    apply=False,
    event=None,
    free_events=(),
    event_prices=None,
    compare_prices=None,
    required_membership=(),
    no_membership=(),
    member_not_recorded=(),
    _conn=None,
):
    """Preview verified corrections in memory, or commit the whole batch with its audit."""
    from shared import postgres_api as postgres

    event_prices, compare_prices = event_prices or {}, compare_prices or {}
    if set(required_membership) & set(no_membership):
        raise ValueError("An event cannot both require and not require membership")
    membership_requirements = {
        **dict.fromkeys(required_membership, True),
        **dict.fromkeys(no_membership, False),
    }
    if apply and compare_prices:
        raise ValueError("--compare-price is dry-run only; confirm the basis before --apply")
    if set(free_events) & (set(event_prices) | set(compare_prices)):
        raise ValueError("An event cannot be both explicitly free and priced")
    if set(event_prices) & set(compare_prices):
        raise ValueError("Choose either a confirmed price or a comparison for an event")
    # Do not use _get_pool(): it can run startup DDL, including in a dry run.
    resource = (
        nullcontext(_conn)
        if _conn is not None
        else psycopg.connect(
            os.environ.get("DATABASE_URL") or postgres.DATABASE_URL,
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=30000"
            + ("" if apply else " -c default_transaction_read_only=on"),
        )
    )
    with resource as connection, connection.transaction():
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" + ("" if apply else ", READ ONLY")
        )
        if apply:
            _guard_apply(connection)
        available = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT event_slug FROM event_archive ORDER BY event_slug"
            )
        ]
        if event and event not in available:
            raise ValueError("--event must identify archived attendance facts")
        selected = [event] if event else available
        assertions = (
            set(free_events)
            | set(event_prices)
            | set(compare_prices)
            | set(membership_requirements)
            | set(member_not_recorded)
        )
        if assertions - set(selected):
            raise ValueError(
                "Price/free inputs must identify archived events in the selected scope"
            )
        if apply:
            columns = {
                row[0]
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'event_stats'"
                )
            }
            if (
                not {
                    "requirements_snapshot",
                    "same_series_return_rate",
                    "same_series_base_count",
                    "same_series_returned_players",
                }
                <= columns
            ):
                raise ValueError("Apply requires migration 010; dry-run works without it")
            for slug in sorted(selected):
                if not connection.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext('fgt-event:' || %s))", (slug,)
                ).fetchone()[0]:
                    raise ValueError("Apply refused: selected event is being archived")
        context = postgres._stats_context(
            connection,
            free_events=free_events,
            event_prices=event_prices,
            membership_requirements=membership_requirements,
            member_not_recorded=member_not_recorded,
        )
        events = [
            postgres.recompute_event_stats(slug, dry_run=True, _conn=connection, _context=context)
            for slug in selected
        ]
        players = postgres.recompute_player_aggregates(
            dry_run=True, _conn=connection, _context=context
        )
        integrity = postgres.scan_event_stats_integrity(
            selected if event else None, _conn=connection, _context=context
        )
        reconciliation = _reconciliation(connection, context, events, players)
        if apply and not reconciliation["balanced"]:
            raise ValueError(
                "Apply refused: all-player/archive/event payment totals do not reconcile"
            )
        comparisons = []
        for slug, (amount, _) in sorted(compare_prices.items()):
            for per_game in (False, True):
                scenario = postgres._stats_context(
                    connection,
                    free_events=free_events,
                    event_prices={**event_prices, slug: (amount, per_game)},
                    membership_requirements=membership_requirements,
                    member_not_recorded=member_not_recorded,
                )
                preview = postgres.recompute_event_stats(
                    slug, dry_run=True, _conn=connection, _context=scenario
                )
                deviations = postgres.scan_event_stats_integrity(
                    [slug], _conn=connection, _context=scenario
                )["deviations"]
                comparisons.append(
                    {
                        "event_slug": slug,
                        "basis": "game" if per_game else "person",
                        "price": str(amount),
                        "revenue": str(preview["after"]["total_revenue"]),
                        "amounts_filled": sum(
                            "payment_amount" in item["after"]
                            for item in scenario["corrections"]
                            if item["event_slug"] == slug
                        ),
                        "below_expected": sum(
                            item["kind"] == "paid_below_expected" for item in deviations
                        ),
                        "unknown_game_count": sum(
                            item["kind"] == "price_game_count_unknown" for item in deviations
                        ),
                    }
                )
        if apply:
            row_changes = [
                {
                    "table": "event_archive",
                    "key": {"id": item["archive_id"]},
                    "before_exists": True,
                    "changes": {
                        key: {"before": item["before"][key], "after": value}
                        for key, value in item["after"].items()
                    },
                }
                for item in context["corrections"]
            ]
            row_changes += [
                {
                    "table": "event_stats",
                    "key": {"event_slug": item["event_slug"]},
                    "before_exists": item["before"] is not None,
                    "changes": item["changes"],
                }
                for item in events
                if item["changes"]
            ]
            row_changes += [
                {
                    "table": "players",
                    "key": {"uuid": item["player_uuid"]},
                    "before_exists": True,
                    "changes": item["changes"],
                }
                for item in players
            ]
            for correction in context["corrections"]:
                values = correction["after"]
                assignments = ", ".join(f"{key} = %s" for key in values)
                connection.execute(
                    f"UPDATE event_archive SET {assignments} WHERE id = %s",
                    [*values.values(), correction["archive_id"]],
                )
            for slug in selected:
                postgres.recompute_event_stats(
                    slug, dry_run=False, _conn=connection, _audit=False, _context=context
                )
            postgres.recompute_player_aggregates(
                dry_run=False, _conn=connection, _audit=False, _context=context
            )
            actual = connection.execute(
                "SELECT (SELECT coalesce(sum(total_paid),0) FROM players), "
                "(SELECT coalesce(sum(payment_amount),0) FROM event_archive), "
                "(SELECT coalesce(sum(total_revenue),0) FROM event_stats)"
            ).fetchone()
            if not actual[0] == actual[1] == actual[2]:
                raise ValueError(
                    "Apply refused: post-repair payment totals do not reconcile; batch rolled back"
                )
            postgres._stats_audit(
                connection,
                "stats_repair_applied",
                "event_stats",
                {
                    "events": selected,
                    "changed_players": len(players),
                    "verified_free_events": sorted(free_events),
                    "verified_prices": {
                        slug: [str(amount), "game" if per_game else "person"]
                        for slug, (amount, per_game) in event_prices.items()
                    },
                    "verified_membership_requirements": membership_requirements,
                    "verified_member_not_recorded": sorted(member_not_recorded),
                    "repair_snapshot_version": 1,
                    "row_changes": row_changes,
                    "reconciliation": reconciliation,
                    "corrected_archive_rows": len(context["corrections"]),
                },
                user={"user_name": "stats-repair"},
            )
    return {
        "applied": apply,
        "events": events,
        "players": players,
        "comparisons": comparisons,
        "corrections": context["corrections"],
        "free_events": sorted(free_events),
        "reconciliation": reconciliation,
        **integrity,
    }


def counts_summary(result):
    """Public export: counts and dates only, with no player identifiers or identity values."""
    fields = (
        "new_players",
        "returning_players",
        "no_show_count",
        "total_revenue",
        "retention_rate",
        "same_series_return_rate",
        "member_percentage",
    )
    players = result["players"]
    return {
        "applied": result["applied"],
        "events": [
            {
                "event_slug": item["event_slug"],
                "before": {key: (item["before"] or {}).get(key) for key in fields},
                "after": {key: item["after"][key] for key in fields},
                "participants": item["after"]["total_participants"],
                "participants_before": (item["before"] or {}).get("total_participants"),
                "archive_rows": item["archive_rows"],
                "expected_payment_corrections": sum(
                    correction["event_slug"] == item["event_slug"]
                    and "payment_expected" in correction["after"]
                    for correction in result["corrections"]
                ),
                "amount_fills": sum(
                    correction["event_slug"] == item["event_slug"]
                    and "payment_amount" in correction["after"]
                    for correction in result["corrections"]
                ),
                "flags": dict(
                    sorted(
                        Counter(
                            issue["kind"]
                            for issue in result["deviations"]
                            if issue.get("event_slug") == item["event_slug"]
                        ).items()
                    )
                ),
            }
            for item in result["events"]
        ],
        "reconciliation": result["reconciliation"],
        "players_changed": len(players),
        "player_field_changes": dict(
            sorted(Counter(key for item in players for key in item["changes"]).items())
        ),
        "player_totals": {
            key: {
                side: sum((item[side].get(key) or 0 for item in players), Decimal("0"))
                for side in ("before", "after")
            }
            for key in ("total_events", "total_paid")
        },
        "archive_corrections": len(result["corrections"]),
        "archive_field_changes": dict(
            sorted(Counter(key for item in result["corrections"] for key in item["after"]).items())
        ),
        "free_events": result["free_events"],
        "price_comparisons": result["comparisons"],
        "data_errors": dict(
            sorted(Counter(item["kind"] for item in result["data_errors"]).items())
        ),
        "deviations": dict(sorted(Counter(item["kind"] for item in result["deviations"]).items())),
    }


def print_result(result, *, player_details=False):
    def change(before, after):
        return f"{before if before is not None else '–'} -> {after if after is not None else '–'}"

    print("APPLIED" if result["applied"] else "DRY RUN (SELECT-only; no writes)")
    print(
        "Event | New | Returning | No-show | Revenue | Familiar faces % | Previous same-series return % | Member %"
    )
    for item in result["events"]:
        values = [
            change((item["before"] or {}).get(key), item["after"][key])
            for key in (
                "new_players",
                "returning_players",
                "no_show_count",
                "total_revenue",
                "retention_rate",
                "same_series_return_rate",
                "member_percentage",
            )
        ]
        print(" | ".join([item["event_slug"], *values]))
    if player_details:
        print("Player UUID | Total event visits | Total paid | First seen")
        for item in result["players"]:
            print(
                " | ".join(
                    [
                        item["player_uuid"],
                        *[
                            change(item["before"][key], item["after"][key])
                            for key in ("total_events", "total_paid", "first_seen")
                        ],
                    ]
                )
            )
    print("Counts: " + json.dumps(counts_summary(result), default=str, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Preview without writes (default)")
    modes.add_argument(
        "--apply", action="store_true", help="Commit repairs and their audit atomically"
    )
    parser.add_argument(
        "--event", help="Limit event statistics; player aggregates use the whole archive"
    )
    parser.add_argument(
        "--free-events", default="", help="Comma-separated VERIFIED free event slugs"
    )
    parser.add_argument(
        "--event-price", action="append", default=[], help="Repeatable verified slug=amount[/game]"
    )
    parser.add_argument(
        "--compare-price",
        action="append",
        default=[],
        help="Dry-only slug=amount comparison of both bases",
    )
    parser.add_argument(
        "--required-membership",
        default="",
        help="Comma-separated VERIFIED membership-required event slugs",
    )
    parser.add_argument(
        "--no-membership",
        default="",
        help="Comma-separated VERIFIED membership-not-required event slugs",
    )
    parser.add_argument(
        "--member-not-recorded",
        default="",
        help="Comma-separated VERIFIED events with missing historical membership data",
    )
    parser.add_argument(
        "--player-details",
        action="store_true",
        help="Include UUID-level derived diffs; no contact data",
    )
    parser.add_argument("--json", action="store_true", help="Counts-only JSON output")
    args = parser.parse_args(argv)
    try:
        result = repair(
            apply=args.apply,
            event=args.event,
            free_events=[slug.strip() for slug in args.free_events.split(",") if slug.strip()],
            event_prices=parse_prices(args.event_price),
            compare_prices=parse_prices(args.compare_price, compare=True),
            required_membership=[
                slug.strip() for slug in args.required_membership.split(",") if slug.strip()
            ],
            no_membership=[slug.strip() for slug in args.no_membership.split(",") if slug.strip()],
            member_not_recorded=[
                slug.strip() for slug in args.member_not_recorded.split(",") if slug.strip()
            ],
        )
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"ERROR: repair failed ({type(error).__name__}); no batch changes committed.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(counts_summary(result), default=str, sort_keys=True))
    else:
        print_result(result, player_details=args.player_details)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

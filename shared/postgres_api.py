"""
Postgres storage backend for FGC Check-in System.

Drop-in replacement for airtable_api.py - same public function signatures.
Activated when DATA_BACKEND=postgres in .env.

Connection uses DATABASE_URL from environment, with a connection pool
managed by psycopg3.
"""

import os
import logging
import json
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- Database connection ---
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.critical("❌ Missing DATABASE_URL in .env")
    raise EnvironmentError("Missing DATABASE_URL in .env")

# Connection pool - lazy-initialized on first use
_pool = None


def _get_pool():
    """Lazy-init a connection pool (import psycopg only when Postgres backend is active)."""
    global _pool
    if _pool is None:
        import psycopg_pool  # type: ignore

        _pool = psycopg_pool.ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            open=True,
            kwargs={"autocommit": True},
        )
        logger.info("✅ Postgres connection pool initialized")
    return _pool


def _coerce_jsonb(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
    return {col: row[idx] for idx, col in enumerate(columns)}


def _checkin_fields_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("name"),
        "email": row.get("email"),
        "telephone": row.get("telephone"),
        "tag": row.get("tag"),
        "payment_amount": row.get("payment_amount"),
        "payment_expected": row.get("payment_expected"),
        "payment_valid": row.get("payment_valid"),
        "member": row.get("member"),
        "startgg": row.get("startgg"),
        "is_guest": row.get("is_guest"),
        "status": row.get("status"),
        "tournament_games_registered": row.get("tournament_games_registered"),
        "UUID": row.get("checkin_uuid"),
        "event_slug": row.get("event_slug"),
        "startgg_event_id": row.get("startgg_event_id"),
        "external_id": row.get("external_id"),
    }


def _settings_value(key: str, value: Any) -> Any:
    if key == "events_json":
        from psycopg.types.json import Json  # type: ignore

        return Json(value) if value is not None else None
    return value


# Session timeouts (same as airtable_api.py)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)
SESSION_IDLE_TIMEOUT = timedelta(hours=2)


# =============================================
# Requirement helpers
# =============================================
def compute_requirements(settings: Dict[str, Any]) -> Dict[str, bool]:
    """
    Compute which requirements are active based on settings.

    Postgres stores booleans natively (no Airtable checkbox semantics),
    but we keep the same None-safe logic for consistency.
    """
    return {
        "require_payment": settings.get("require_payment") is True,
        "require_membership": settings.get("require_membership") is True,
        "require_startgg": settings.get("require_startgg") is True,
    }


# =============================================
# Settings
# =============================================
def get_active_settings() -> Optional[Dict[str, Any]]:
    """Return fields from the active settings row (is_active = true)."""
    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM settings
                WHERE is_active = true
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        logger.warning("⚠️ No active settings row found.")
        return None

    data = _row_to_dict(columns, row)
    data.pop("id", None)
    return data


def get_active_slug() -> Optional[str]:
    """Return active_event_slug from the active settings row."""
    settings = get_active_settings() or {}
    slug = settings.get("active_event_slug")
    if slug:
        logger.info(f"🎯 Active slug: {slug}")
    else:
        logger.warning("⚠️ active_event_slug missing on active settings row.")
    return slug


def get_active_settings_with_id() -> Optional[Dict[str, Any]]:
    """Return the active settings row with its record_id included."""
    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM settings
                WHERE is_active = true
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        logger.warning("⚠️ No active settings row found.")
        return None

    data = _row_to_dict(columns, row)
    record_id = data.pop("id", None)
    return {"record_id": str(record_id) if record_id is not None else None, "fields": data}


def update_settings(record_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update fields on a settings record."""
    if not record_id:
        return None

    try:
        record_id_int: int = int(record_id)
    except (TypeError, ValueError):
        return None

    columns = []
    values: List[Any] = []
    for key, value in (fields or {}).items():
        columns.append(f"{key} = %s")
        values.append(_settings_value(key, value))

    if not columns:
        return None

    values.append(record_id_int)

    columns_out = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE settings SET {', '.join(columns)} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
            if row:
                columns_out = [desc[0] for desc in cur.description]

    if not row or not columns_out:
        return None

    data = _row_to_dict(columns_out, row)
    return {"record_id": str(data.get("id")), "fields": {k: v for k, v in data.items() if k != "id"}}


# =============================================
# Checkins (active_event_data)
# =============================================
def get_checkins(
    slug: Optional[str] = None, include_all: bool = False
) -> List[Dict[str, Any]]:  # type: ignore[assignment]
    """Return check-ins for a given event_slug from active_event_data."""
    if not slug and not include_all:
        return []

    params: List[Any] = []
    where_sql = ""
    if not include_all:
        where_sql = "WHERE event_slug = %s"
        params.append(slug)

    query = f"""
        SELECT record_id, created, event_slug, status, member, startgg, is_guest,
               payment_amount, payment_expected, payment_valid,
               name, email, tag, telephone,
               tournament_games_registered, checkin_uuid, startgg_event_id, external_id
        FROM active_event_data
        {where_sql}
        ORDER BY created DESC
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        (
            record_id,
            created,
            event_slug,
            status,
            member,
            startgg,
            is_guest,
            payment_amount,
            payment_expected,
            payment_valid,
            name,
            email,
            tag,
            telephone,
            tournament_games_registered,
            checkin_uuid,
            startgg_event_id,
            external_id,
        ) = row

        result.append(
            {
                "record_id": record_id,
                "created": created.isoformat() if created else None,
                "event_slug": event_slug,
                "status": status,
                "member": member,
                "startgg": startgg,
                "is_guest": is_guest,
                "payment_amount": payment_amount,
                "payment_expected": payment_expected,
                "payment_valid": payment_valid,
                "name": name,
                "email": email,
                "tag": tag,
                "telephone": telephone,
                "tournament_games_registered": tournament_games_registered,
                "UUID": checkin_uuid,
                "startgg_event_id": startgg_event_id,
                "external_id": external_id,
            }
        )

    if include_all:
        logger.info(f"📥 Found {len(result)} checkins (ALL events)")
    else:
        logger.info(f"📥 Found {len(result)} checkins for slug '{slug}'")
    return result


def get_all_event_slugs() -> List[str]:
    """Collect unique event_slug values from active_event_data + settings."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_slug FROM active_event_data WHERE event_slug IS NOT NULL")
            rows = cur.fetchall()

    slugs = {row[0] for row in rows if row and row[0]}

    active = get_active_slug()
    if active:
        slugs.add(active)

    out = sorted(slugs)
    logger.info(f"📚 Retrieved {len(out)} unique event slugs (including active fallback).")
    return out


def get_checkin_by_name(name: str, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find a checkin record by name (case-insensitive)."""
    if not name:
        return None

    params: List[Any] = [name]
    where_sql = "LOWER(name) = LOWER(%s)"

    if slug:
        where_sql += " AND event_slug = %s"
        params.append(slug)

    query = f"""
        SELECT record_id, name, tag, email, telephone, status, member, startgg,
               payment_valid, payment_amount, payment_expected,
               tournament_games_registered, checkin_uuid, event_slug,
               startgg_event_id, external_id, is_guest, created
        FROM active_event_data
        WHERE {where_sql}
        ORDER BY created DESC
        LIMIT 1
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

    if not row:
        return None

    columns = [
        "record_id",
        "name",
        "tag",
        "email",
        "telephone",
        "status",
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "checkin_uuid",
        "event_slug",
        "startgg_event_id",
        "external_id",
        "is_guest",
        "created",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def get_checkin_by_tag(tag: str, slug: str) -> Optional[Dict[str, Any]]:
    """Find a checkin record by tag + event_slug (case-insensitive tag match)."""
    if not tag or not slug:
        return None

    query = """
        SELECT record_id, name, tag, email, telephone, status, member, startgg,
               payment_valid, payment_amount, payment_expected,
               tournament_games_registered, checkin_uuid, event_slug,
               startgg_event_id, external_id, is_guest, created
        FROM active_event_data
        WHERE LOWER(tag) = LOWER(%s) AND event_slug = %s
        ORDER BY created DESC
        LIMIT 1
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (tag, slug))
            row = cur.fetchone()

    if not row:
        return None

    columns = [
        "record_id",
        "name",
        "tag",
        "email",
        "telephone",
        "status",
        "member",
        "startgg",
        "payment_valid",
        "payment_amount",
        "payment_expected",
        "tournament_games_registered",
        "checkin_uuid",
        "event_slug",
        "startgg_event_id",
        "external_id",
        "is_guest",
        "created",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def update_checkin(
    record_id: str, fields: Dict[str, Any], typecast: bool = False
) -> Optional[Dict[str, Any]]:
    """Update fields on a checkin record."""
    if not record_id:
        return None

    update_fields = {}
    for key, value in (fields or {}).items():
        if key == "UUID":
            update_fields["checkin_uuid"] = value
        else:
            update_fields[key] = value

    if not update_fields:
        return None

    set_sql = ", ".join([f"{k} = %s" for k in update_fields.keys()])
    params = list(update_fields.values())
    params.append(record_id)

    columns = None
    row = None
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE active_event_data SET {set_sql} WHERE record_id = %s RETURNING *",
                params,
            )
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]

    if not row or not columns:
        return None

    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def delete_checkin(record_id: str) -> bool:
    """Delete a checkin record."""
    if not record_id:
        return False

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_event_data WHERE record_id = %s", (record_id,))
            return cur.rowcount > 0


# =============================================
# Players
# =============================================
def get_players() -> List[Dict[str, Any]]:
    """Return all player profiles."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uuid, name, email, tag, telephone, created_at
                FROM players
                ORDER BY created_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        uuid_val, name, email, tag, telephone, created_at = row
        result.append(
            {
                "id": uuid_val,
                "name": name,
                "email": email,
                "tag": tag,
                "telephone": telephone,
                "created": created_at.isoformat() if created_at else None,
            }
        )

    logger.info(f"👥 Retrieved {len(result)} players.")
    return result


# =============================================
# Event history / archive
# =============================================
def get_event_history() -> List[Dict[str, Any]]:
    """Return archived event rows."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_slug, event_date, event_display_name, archived_at,
                       total_participants, total_revenue, avg_payment
                FROM event_stats
                ORDER BY archived_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        (
            event_slug,
            event_date,
            event_display_name,
            archived_at,
            total_participants,
            total_revenue,
            avg_payment,
        ) = row

        result.append(
            {
                "event_slug": event_slug,
                "event_date": event_date.isoformat() if event_date else None,
                "event_display_name": event_display_name,
                "participants": total_participants,
                "total_participants": total_participants,
                "total_revenue": total_revenue,
                "avg_payment": avg_payment,
                "created": archived_at.isoformat() if archived_at else None,
                "archived_at": archived_at.isoformat() if archived_at else None,
                "status": None,
            }
        )

    logger.info(f"📦 Retrieved {len(result)} historical rows.")
    return result


def get_event_history_dashboard() -> List[Dict[str, Any]]:
    """Return event history for dashboard view."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_slug, event_date, event_display_name, archived_at,
                       total_participants, total_revenue, avg_payment,
                       member_count, guest_count, startgg_count,
                       new_players, returning_players, retention_rate,
                       games_breakdown, most_popular_game, status_breakdown
                FROM event_stats
                ORDER BY archived_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        (
            event_slug,
            event_date,
            event_display_name,
            archived_at,
            total_participants,
            total_revenue,
            avg_payment,
            member_count,
            guest_count,
            startgg_count,
            new_players,
            returning_players,
            retention_rate,
            games_breakdown,
            most_popular_game,
            status_breakdown,
        ) = row

        result.append(
            {
                "event_slug": event_slug,
                "event_date": event_date.isoformat() if event_date else None,
                "event_display_name": event_display_name,
                "archived_at": archived_at.isoformat() if archived_at else None,
                "total_participants": total_participants,
                "total_revenue": total_revenue,
                "avg_payment": avg_payment,
                "member_count": member_count,
                "guest_count": guest_count,
                "startgg_count": startgg_count,
                "new_players": new_players,
                "returning_players": returning_players,
                "retention_rate": retention_rate,
                "games_breakdown": games_breakdown,
                "most_popular_game": most_popular_game,
                "status_breakdown": status_breakdown,
            }
        )

    logger.info(f"📊 Retrieved {len(result)} dashboard-history rows.")
    return result


# =============================================
# Archive / Event Stats (write operations)
# =============================================
def compute_event_stats(checkins: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute aggregated event statistics from a list of checkin rows.

    Takes raw checkin dicts from active_event_data (DB types: Decimal for amounts,
    bool for flags, list for TEXT[] games).

    Returns a dict matching event_stats columns (minus identifiers/retention).
    Retention (new_players, returning_players, retention_rate) requires player
    table lookups and is computed separately in archive_event().
    """
    total = len(checkins)
    if total == 0:
        return {
            "total_participants": 0,
            "total_revenue": Decimal("0"),
            "avg_payment": Decimal("0"),
            "member_count": 0,
            "member_percentage": Decimal("0"),
            "guest_count": 0,
            "startgg_count": 0,
            "games_breakdown": {},
            "most_popular_game": None,
            "status_breakdown": {},
        }

    # Revenue
    total_revenue = sum(
        Decimal(str(c.get("payment_amount") or 0)) for c in checkins
    )
    avg_payment = (total_revenue / total).quantize(Decimal("0.01"))

    # Segments
    member_count = sum(1 for c in checkins if c.get("member"))
    guest_count = sum(1 for c in checkins if c.get("is_guest"))
    startgg_count = sum(1 for c in checkins if c.get("startgg"))
    member_pct = (Decimal(member_count) / Decimal(total) * 100).quantize(Decimal("0.01"))

    # Games breakdown
    games: Dict[str, int] = {}
    for c in checkins:
        for g in (c.get("tournament_games_registered") or []):
            games[g] = games.get(g, 0) + 1

    most_popular = max(games, key=games.get) if games else None

    # Status breakdown
    statuses: Dict[str, int] = {}
    for c in checkins:
        s = c.get("status") or "Unknown"
        statuses[s] = statuses.get(s, 0) + 1

    return {
        "total_participants": total,
        "total_revenue": total_revenue,
        "avg_payment": avg_payment,
        "member_count": member_count,
        "member_percentage": member_pct,
        "guest_count": guest_count,
        "startgg_count": startgg_count,
        "games_breakdown": games,
        "most_popular_game": most_popular,
        "status_breakdown": statuses,
    }


def _match_or_create_player(
    cur,
    checkin: Dict[str, Any],
    event_slug: str,
    event_date: str,
    now: datetime,
) -> tuple:
    """
    Match a checkin to an existing player or create a new one.

    Match priority: tag (case-insensitive) > email (case-insensitive).
    Updates player totals, games, timeline on match.
    Creates new player profile if no match found.

    Returns:
        (player_uuid: str, is_new: bool)
    """
    from psycopg.types.json import Json as _Json  # type: ignore

    tag = checkin.get("tag")
    email = checkin.get("email")

    # Try match by tag, then email
    player_row = None
    select_cols = """
        uuid, games_played, total_events, total_paid,
        events_list, game_counts, first_seen
    """

    if tag:
        cur.execute(
            f"SELECT {select_cols} FROM players WHERE LOWER(tag) = LOWER(%s) LIMIT 1",
            (tag,),
        )
        player_row = cur.fetchone()

    if not player_row and email:
        cur.execute(
            f"SELECT {select_cols} FROM players WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            (email,),
        )
        player_row = cur.fetchone()

    new_games = checkin.get("tournament_games_registered") or []
    payment = Decimal(str(checkin.get("payment_amount") or 0))

    if player_row:
        # ---- Existing player: update ----
        (
            p_uuid, p_games, p_total_events, p_total_paid,
            p_events_list, p_game_counts, p_first_seen,
        ) = player_row

        p_games = p_games or []
        p_total_events = p_total_events or 0
        p_events_list = p_events_list if isinstance(p_events_list, list) else []
        p_game_counts = p_game_counts if isinstance(p_game_counts, dict) else {}

        # Merge games_played (unique set)
        updated_games = list(set(p_games) | set(new_games))

        # Accumulate game_counts
        for g in new_games:
            p_game_counts[g] = p_game_counts.get(g, 0) + 1
        favorite = max(p_game_counts, key=p_game_counts.get) if p_game_counts else None

        # Append to events_list (skip if already archived for this slug)
        is_new_event = event_slug not in p_events_list
        if is_new_event:
            updated_events = p_events_list + [event_slug]
            new_total_events = p_total_events + 1
        else:
            updated_events = p_events_list
            new_total_events = p_total_events

        # Only add payment if this is a new event for the player (avoid double-counting)
        payment_to_add = payment if is_new_event else Decimal("0")

        cur.execute(
            """
            UPDATE players SET
                tag = COALESCE(%s, tag),
                email = COALESCE(%s, email),
                name = COALESCE(%s, name),
                telephone = COALESCE(%s, telephone),
                games_played = %s,
                game_counts = %s,
                favorite_game = %s,
                total_events = %s,
                total_paid = total_paid + %s,
                last_seen = %s::date,
                last_event = %s,
                events_list = %s,
                is_member = COALESCE(%s, is_member),
                updated_at = %s
            WHERE uuid = %s
            """,
            (
                tag, email, checkin.get("name"), checkin.get("telephone"),
                updated_games,
                _Json(p_game_counts),
                favorite,
                new_total_events,
                payment_to_add,
                event_date, event_slug,
                _Json(updated_events),
                checkin.get("member"),
                now,
                p_uuid,
            ),
        )
        return (p_uuid, False)

    else:
        # ---- New player: create ----
        game_counts: Dict[str, int] = {}
        for g in new_games:
            game_counts[g] = game_counts.get(g, 0) + 1
        favorite = max(game_counts, key=game_counts.get) if game_counts else None

        cur.execute(
            """
            INSERT INTO players (
                name, tag, email, telephone,
                games_played, game_counts, favorite_game,
                total_events, total_paid,
                first_seen, last_seen, first_event, last_event,
                events_list, is_member, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s::date, %s::date, %s, %s,
                %s, %s, %s, %s
            )
            RETURNING uuid
            """,
            (
                checkin.get("name"), tag, email, checkin.get("telephone"),
                new_games or [],
                _Json(game_counts) if game_counts else _Json({}),
                favorite,
                1,
                payment,
                event_date, event_date, event_slug, event_slug,
                _Json([event_slug]),
                checkin.get("member") or False,
                now, now,
            ),
        )
        new_uuid = cur.fetchone()[0]
        return (new_uuid, True)


def archive_event(
    event_slug: str,
    *,
    event_date: Optional[str] = None,
    event_display_name: str = "",
    swish_expected_per_game: int = 0,
    startgg_snapshot: Optional[Dict[str, Any]] = None,
    user: Optional[Dict[str, Any]] = None,
    clear_active: bool = False,
) -> Dict[str, Any]:
    """
    Archive an event: match/create players, copy checkins to event_archive,
    compute + store stats, audit log.

    All writes happen in a single transaction (all-or-nothing).
    Audit log is written after commit (best-effort).

    Args:
        event_slug: The event to archive.
        event_date: ISO date string (YYYY-MM-DD). Falls back to settings.event_date.
        event_display_name: Human-readable event name.
        swish_expected_per_game: Payment config snapshot at archive time.
        startgg_snapshot: Start.gg event data as JSONB (stored in event_stats).
        user: Session dict for audit log (user_id, user_name, user_email).
        clear_active: If True, delete from active_event_data after archiving.

    Returns:
        Dict with archive summary (participant count, revenue, breakdowns, etc.).

    Raises:
        ValueError: If event_date cannot be resolved from parameter or settings.
    """
    from psycopg.types.json import Json  # type: ignore

    # 0. Resolve defaults from settings where parameters are missing
    needs_settings = (
        not event_date or not event_display_name
        or not swish_expected_per_game or startgg_snapshot is None
    )
    if needs_settings:
        settings = get_active_settings()
    else:
        settings = None

    if not event_date:
        if settings:
            ed = settings.get("event_date")
            if ed:
                event_date = ed.isoformat() if hasattr(ed, "isoformat") else str(ed)
        if not event_date:
            raise ValueError(
                "event_date is required for archiving "
                "(not provided as parameter and not found in active settings)"
            )

    if not event_display_name and settings:
        event_display_name = settings.get("event_display_name") or ""

    if not swish_expected_per_game and settings:
        swish_expected_per_game = settings.get("swish_expected_per_game") or 0

    if startgg_snapshot is None and settings:
        startgg_snapshot = settings.get("events_json")

    now = datetime.now(timezone.utc)
    replaced_rows = 0

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Replace mode: keep one current archive snapshot per event_slug
                cur.execute(
                    "SELECT COUNT(*) FROM event_archive WHERE event_slug = %s",
                    (event_slug,),
                )
                existing_archive_rows = cur.fetchone()[0] or 0
                if existing_archive_rows > 0:
                    cur.execute(
                        "DELETE FROM event_archive WHERE event_slug = %s",
                        (event_slug,),
                    )
                    replaced_rows = cur.rowcount or 0

                # 1. Read active checkins for this slug
                cur.execute(
                    """
                    SELECT name, tag, email, telephone, status,
                           member, startgg, payment_valid,
                           payment_amount, payment_expected,
                           tournament_games_registered, checkin_uuid,
                           external_id, startgg_event_id, is_guest
                    FROM active_event_data
                    WHERE event_slug = %s
                    """,
                    (event_slug,),
                )
                columns = [desc[0] for desc in cur.description]
                raw_rows = cur.fetchall()

                if not raw_rows:
                    logger.warning(f"⚠️ No checkins found for slug '{event_slug}'")
                    return {"archived": 0, "event_slug": event_slug}

                checkins = [_row_to_dict(columns, row) for row in raw_rows]

                # 2. Match/create players (returns uuid + new/returning flag)
                player_results = []
                for c in checkins:
                    p_uuid, is_new = _match_or_create_player(
                        cur, c, event_slug, event_date, now
                    )
                    player_results.append((p_uuid, is_new))

                # 3. Insert into event_archive (with player_uuid)
                cur.executemany(
                    """
                    INSERT INTO event_archive (
                        event_slug, event_date, event_display_name,
                        name, tag, email, telephone, status,
                        member, startgg, payment_valid,
                        payment_amount, payment_expected,
                        swish_expected_per_game,
                        tournament_games_registered, checkin_uuid,
                        external_id, startgg_event_id, is_guest,
                        archived_at, player_uuid
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s
                    )
                    """,
                    [
                        (
                            event_slug, event_date, event_display_name,
                            c.get("name"), c.get("tag"), c.get("email"),
                            c.get("telephone"), c.get("status"),
                            c.get("member"), c.get("startgg"), c.get("payment_valid"),
                            c.get("payment_amount"), c.get("payment_expected"),
                            swish_expected_per_game,
                            c.get("tournament_games_registered"), c.get("checkin_uuid"),
                            c.get("external_id"), c.get("startgg_event_id"),
                            c.get("is_guest"),
                            now, player_results[i][0],
                        )
                        for i, c in enumerate(checkins)
                    ],
                )

                # 4. Compute stats + retention from player matching results
                stats = compute_event_stats(checkins)

                new_players = sum(1 for _, is_new in player_results if is_new)
                total = stats["total_participants"]
                returning_players = total - new_players
                retention_rate = (
                    (Decimal(returning_players) / Decimal(total) * 100).quantize(
                        Decimal("0.01")
                    )
                    if total > 0
                    else Decimal("0")
                )

                # 5. Upsert event_stats (including startgg_snapshot)
                cur.execute(
                    """
                    INSERT INTO event_stats (
                        event_slug, event_date, event_display_name, archived_at,
                        total_participants, total_revenue, avg_payment,
                        member_count, member_percentage, guest_count, startgg_count,
                        new_players, returning_players, retention_rate,
                        games_breakdown, most_popular_game, status_breakdown,
                        startgg_snapshot
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s
                    )
                    ON CONFLICT (event_slug) DO UPDATE SET
                        event_date = EXCLUDED.event_date,
                        event_display_name = EXCLUDED.event_display_name,
                        archived_at = EXCLUDED.archived_at,
                        total_participants = EXCLUDED.total_participants,
                        total_revenue = EXCLUDED.total_revenue,
                        avg_payment = EXCLUDED.avg_payment,
                        member_count = EXCLUDED.member_count,
                        member_percentage = EXCLUDED.member_percentage,
                        guest_count = EXCLUDED.guest_count,
                        startgg_count = EXCLUDED.startgg_count,
                        new_players = EXCLUDED.new_players,
                        returning_players = EXCLUDED.returning_players,
                        retention_rate = EXCLUDED.retention_rate,
                        games_breakdown = EXCLUDED.games_breakdown,
                        most_popular_game = EXCLUDED.most_popular_game,
                        status_breakdown = EXCLUDED.status_breakdown,
                        startgg_snapshot = EXCLUDED.startgg_snapshot
                    """,
                    (
                        event_slug, event_date, event_display_name, now,
                        stats["total_participants"],
                        stats["total_revenue"],
                        stats["avg_payment"],
                        stats["member_count"],
                        stats["member_percentage"],
                        stats["guest_count"],
                        stats["startgg_count"],
                        new_players,
                        returning_players,
                        retention_rate,
                        Json(stats["games_breakdown"]),
                        stats["most_popular_game"],
                        Json(stats["status_breakdown"]),
                        Json(startgg_snapshot) if startgg_snapshot else None,
                    ),
                )

                # 6. Optionally clear active data
                deleted_count = 0
                if clear_active:
                    cur.execute(
                        "DELETE FROM active_event_data WHERE event_slug = %s",
                        (event_slug,),
                    )
                    deleted_count = cur.rowcount

    # 7. Audit log (after commit, best-effort)
    archive_user = user or {"user_id": "", "user_name": "system", "user_email": ""}
    audit_action = "event_rearchived" if replaced_rows > 0 else "event_archived"
    log_action(
        archive_user,
        audit_action,
        "event_archive",
        target_event=event_slug,
        details=json.dumps({
            "participants": stats["total_participants"],
            "total_revenue": str(stats["total_revenue"]),
            "new_players": new_players,
            "returning_players": returning_players,
            "replaced_rows": replaced_rows,
            "cleared_active": clear_active,
        }),
    )

    logger.info(
        f"📦 Archived event '{event_slug}': "
        f"{stats['total_participants']} participants, "
        f"revenue {stats['total_revenue']}"
    )

    return {
        "event_slug": event_slug,
        "archived": stats["total_participants"],
        "total_revenue": float(stats["total_revenue"]),
        "avg_payment": float(stats["avg_payment"]),
        "member_count": stats["member_count"],
        "guest_count": stats["guest_count"],
        "startgg_count": stats["startgg_count"],
        "new_players": new_players,
        "returning_players": returning_players,
        "retention_rate": float(retention_rate),
        "games_breakdown": stats["games_breakdown"],
        "most_popular_game": stats["most_popular_game"],
        "status_breakdown": stats["status_breakdown"],
        "replaced_rows": replaced_rows,
        "cleared_active": deleted_count if clear_active else 0,
    }


# =============================================
# Sessions
# =============================================
def create_session(user_info: dict, access_token: str) -> Optional[str]:
    """Create a new session and return the session_id."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    user_info = user_info or {}

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    user_id,
                    user_name,
                    user_email,
                    access_token,
                    created_at,
                    expires_at,
                    last_active
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    str(user_info.get("id", "")),
                    user_info.get("name", ""),
                    user_info.get("email", ""),
                    access_token,
                    now,
                    now + SESSION_ABSOLUTE_TIMEOUT,
                    now,
                ),
            )

    logger.info(f"🔐 Created session for user '{user_info.get('name')}' ({session_id[:8]}...)")
    return session_id


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve and validate a session (checks absolute + idle timeout)."""
    if not session_id:
        return None

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, session_id, user_id, user_name, user_email, access_token,
                       created_at, expires_at, last_active
                FROM sessions
                WHERE session_id = %s
                LIMIT 1
                """,
                (session_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    (
        record_id,
        session_id,
        user_id,
        user_name,
        user_email,
        access_token,
        created_at,
        expires_at,
        last_active,
    ) = row

    now = datetime.now(timezone.utc)

    if expires_at and now > expires_at:
        logger.info(f"🔒 Session {session_id[:8]}... expired (absolute timeout)")
        delete_session(session_id)
        return None

    if last_active and now - last_active > SESSION_IDLE_TIMEOUT:
        logger.info(f"🔒 Session {session_id[:8]}... expired (idle timeout)")
        delete_session(session_id)
        return None

    return {
        "_record_id": record_id,
        "session_id": session_id,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "access_token": access_token,
        "created_at": created_at.isoformat() if created_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "last_active": last_active.isoformat() if last_active else None,
    }


def delete_session(session_id: str) -> bool:
    """Delete a session (logout)."""
    if not session_id:
        return False

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            return cur.rowcount > 0


def update_session_activity(session_id: str) -> None:
    """Touch the last_active timestamp on a session."""
    if not session_id:
        return

    now = datetime.now(timezone.utc)
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET last_active = %s WHERE session_id = %s",
                (now, session_id),
            )


def cleanup_expired_sessions() -> int:
    """Delete all sessions past their absolute expiry. Returns count deleted."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < now()")
            deleted = cur.rowcount

    if deleted:
        logger.info(f"🧹 Cleaned up {deleted} expired sessions")
    return deleted


# =============================================
# Audit Log
# =============================================
def log_action(
    user: Dict[str, Any],
    action: str,
    target_table: str,
    *,
    target_event: str = "",
    target_record: str = "",
    target_player: str = "",
    reason: str = "",
    details: str = "",
    before_state: str = "",
    after_state: str = "",
) -> Optional[str]:
    """Write an entry to the audit log. Returns record ID or None."""
    now = datetime.now(timezone.utc)
    user = user or {}

    fields = {
        "timestamp": now,
        "user_id": user.get("user_id", ""),
        "user_name": user.get("user_name", "system"),
        "user_email": user.get("user_email", ""),
        "action": action,
        "target_table": target_table,
        "target_event": target_event or None,
        "target_record": target_record or None,
        "target_player": target_player or None,
        "reason": reason or None,
        "details": details or None,
        "before_state": _coerce_jsonb(before_state),
        "after_state": _coerce_jsonb(after_state),
    }

    columns = [k for k, v in fields.items() if v is not None]
    values = [fields[k] for k in columns]

    from psycopg.types.json import Json  # type: ignore

    values = [Json(v) if isinstance(v, (dict, list)) else v for v in values]

    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(columns)

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO audit_log ({col_sql}) VALUES ({placeholders}) RETURNING id",
                values,
            )
            row = cur.fetchone()

    if row:
        record_id = str(row[0])
        logger.info(f"📋 Audit: {user.get('user_name', '?')} -> {action} on {target_table}")
        return record_id

    logger.error(f"❌ Failed to write audit log: {action}")
    return None


def get_audit_log(
    *,
    action: Optional[str] = None,
    target_event: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve audit log entries with optional filters, newest first."""
    conditions = []
    params: List[Any] = []

    if action:
        conditions.append("action = %s")
        params.append(action)
    if target_event:
        conditions.append("target_event = %s")
        params.append(target_event)
    if user_id:
        conditions.append("user_id = %s")
        params.append(user_id)

    where_sql = ""
    if conditions:
        where_sql = "WHERE " + " AND ".join(conditions)

    params.append(limit)

    query = f"""
        SELECT id, timestamp, user_id, user_name, user_email, action,
               target_table, target_event, target_record, target_player,
               reason, details, before_state, after_state
        FROM audit_log
        {where_sql}
        ORDER BY timestamp DESC
        LIMIT %s
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result = []
    for row in rows:
        (
            record_id,
            timestamp,
            row_user_id,
            row_user_name,
            row_user_email,
            row_action,
            row_target_table,
            row_target_event,
            row_target_record,
            row_target_player,
            row_reason,
            row_details,
            row_before_state,
            row_after_state,
        ) = row

        result.append(
            {
                "id": record_id,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "user_id": row_user_id,
                "user_name": row_user_name,
                "user_email": row_user_email,
                "action": row_action,
                "target_table": row_target_table,
                "target_event": row_target_event,
                "target_record": row_target_record,
                "target_player": row_target_player,
                "reason": row_reason,
                "details": row_details,
                "before_state": row_before_state,
                "after_state": row_after_state,
            }
        )

    logger.info(f"📋 Retrieved {len(result)} audit log entries")
    return result

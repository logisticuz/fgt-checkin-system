"""
Postgres storage backend for FGC Check-in System.

Primary storage backend for FGC Check-in System (default).
Shares public function signatures with airtable_api.py (legacy fallback).

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

# --- Feature flags ---
CANONICAL_PLAYER_ID_ENABLED = os.getenv("CANONICAL_PLAYER_ID_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
logger.info(f"🔑 Feature flag CANONICAL_PLAYER_ID_ENABLED={CANONICAL_PLAYER_ID_ENABLED}")

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
        _run_migrations(_pool)
    return _pool


def _run_migrations(pool):
    """Idempotent schema migrations - safe to run on every startup."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # No-show tracking columns (added 2026-02-24)
                for col, typ in [
                    ("startgg_registered_count", "INTEGER DEFAULT 0"),
                    ("startgg_registered_players", "INTEGER DEFAULT 0"),
                    ("checked_in_count", "INTEGER DEFAULT 0"),
                    ("no_show_count", "INTEGER DEFAULT 0"),
                    ("no_show_rate", "NUMERIC(5,2) DEFAULT 0"),
                ]:
                    cur.execute(
                        f"ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS {col} {typ}"
                    )
                # Canonical player ID column on active_event_data (added 2026-03-10)
                cur.execute(
                    "ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS player_uuid TEXT"
                )
                cur.execute(
                    "ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS added_via TEXT DEFAULT 'unknown'"
                )
                cur.execute(
                    "UPDATE active_event_data SET added_via = 'unknown' WHERE added_via IS NULL"
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_active_player_uuid
                    ON active_event_data(player_uuid)
                    """
                )
                cur.execute(
                    "ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS added_via TEXT DEFAULT 'unknown'"
                )
                cur.execute(
                    "UPDATE event_archive SET added_via = 'unknown' WHERE added_via IS NULL"
                )

                # Merge log table (added 2026-03-11)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS merge_log (
                        id                      SERIAL PRIMARY KEY,
                        merged_at               TIMESTAMPTZ DEFAULT now(),
                        keep_uuid               TEXT NOT NULL,
                        remove_uuid             TEXT NOT NULL,
                        user_id                 TEXT,
                        user_name               TEXT,
                        reason                  TEXT,
                        removed_player_snapshot  JSONB NOT NULL,
                        archive_rows_updated    INTEGER DEFAULT 0,
                        active_rows_updated     INTEGER DEFAULT 0,
                        undone                  BOOLEAN DEFAULT false,
                        undone_at               TIMESTAMPTZ
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_merge_log_keep ON merge_log(keep_uuid)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_merge_log_remove ON merge_log(remove_uuid)"
                )
        logger.info("✅ Schema migrations checked (no-show + player_uuid + added_via + merge_log)")
    except Exception as e:
        logger.warning(f"⚠️ Migration check failed (non-fatal): {e}")


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


def _normalize_added_via(value: Any) -> str:
    allowed = {"manual_dashboard", "startgg_flow", "api", "reopen_restore", "unknown"}
    candidate = str(value or "unknown").strip().lower()
    return candidate if candidate in allowed else "unknown"


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
        "added_via": row.get("added_via"),
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


def compute_checkin_status(
    checkin_fields: Dict[str, Any], settings: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Compute Ready/Pending status for a checkin based on active requirements.

    Args:
        checkin_fields: The checkin record fields (member, startgg, payment_valid, etc.)
        settings: Active settings row (require_payment, require_membership, require_startgg)

    Returns:
        { "status": "Ready"|"Pending", "ready": bool, "missing": [...] }
    """
    reqs = compute_requirements(settings)

    missing: List[str] = []
    if reqs["require_membership"] and not checkin_fields.get("member"):
        missing.append("Membership")
    if reqs["require_payment"] and not checkin_fields.get("payment_valid"):
        missing.append("Payment")
    if reqs["require_startgg"] and not checkin_fields.get("startgg"):
        missing.append("Start.gg")

    ready = len(missing) == 0
    return {
        "status": "Ready" if ready else "Pending",
        "ready": ready,
        "missing": missing,
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
               tournament_games_registered, checkin_uuid, startgg_event_id, external_id,
               added_via
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
            added_via,
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
                "added_via": added_via,
            }
        )

    if include_all:
        logger.info(f"📥 Found {len(result)} checkins (ALL events)")
    else:
        logger.info(f"📥 Found {len(result)} checkins for slug '{slug}'")
    return result


def get_all_event_slugs() -> List[str]:
    """Collect unique event_slug values from active_event_data only.

    Only returns slugs that have actual participant data.  Cleared/archived
    events (no rows left in active_event_data) are intentionally excluded
    so the select-event dropdown stays clean.  Reopened events re-appear
    automatically because their data is restored to active_event_data.
    """
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_slug FROM active_event_data WHERE event_slug IS NOT NULL")
            rows = cur.fetchall()

    slugs = {row[0] for row in rows if row and row[0]}

    out = sorted(slugs)
    logger.info(f"📚 Retrieved {len(out)} unique event slugs from active data.")
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
               startgg_event_id, external_id, is_guest, added_via, created
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
        "added_via",
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
               startgg_event_id, external_id, is_guest, added_via, created
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
        "added_via",
        "created",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def get_checkin_by_record_id(record_id: str) -> Optional[Dict[str, Any]]:
    """Find a checkin record by its record_id (primary key)."""
    if not record_id:
        return None

    query = """
        SELECT record_id, name, tag, email, telephone, status, member, startgg,
               payment_valid, payment_amount, payment_expected,
               tournament_games_registered, checkin_uuid, event_slug,
               startgg_event_id, external_id, is_guest, added_via, created
        FROM active_event_data
        WHERE record_id = %s
        LIMIT 1
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (record_id,))
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
        "added_via",
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


def begin_checkin(event_slug: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create or update a check-in attempt and return checkin_id.

    Dedupe strategy (backend-owned):
    - event_slug + tag (case-insensitive) if tag exists
    - else event_slug + name (case-insensitive) if name exists
    """
    if not event_slug:
        raise ValueError("event_slug is required")

    payload = payload or {}
    tag = (payload.get("tag") or "").strip() or None
    name = (payload.get("name") or "").strip() or None

    existing = None
    if tag:
        existing = get_checkin_by_tag(tag, event_slug)
    elif name:
        existing = get_checkin_by_name(name, event_slug)

    games = payload.get("tournament_games_registered")
    if isinstance(games, str):
        games = [g.strip() for g in games.split(",") if g.strip()]
    if not isinstance(games, list):
        games = []

    # Resolve player_uuid early (match-only, no creation)
    matched_player_uuid = None
    if CANONICAL_PLAYER_ID_ENABLED:
        try:
            matched_player_uuid = _find_player_uuid(tag, payload.get("email"))
        except Exception as exc:
            logger.warning(f"⚠️ Player UUID lookup failed (non-blocking): {exc}")

    fields: Dict[str, Any] = {
        "event_slug": event_slug,
        "name": payload.get("name"),
        "tag": payload.get("tag"),
        "email": payload.get("email"),
        "telephone": payload.get("telephone"),
        "status": payload.get("status") or "Pending",
        "member": bool(payload.get("member", False)),
        "startgg": bool(payload.get("startgg", False)),
        "is_guest": bool(payload.get("is_guest", False)),
        "payment_valid": bool(payload.get("payment_valid", False)),
        "payment_amount": payload.get("payment_amount") or 0,
        "payment_expected": payload.get("payment_expected") or 0,
        "tournament_games_registered": games,
        "UUID": payload.get("UUID") or payload.get("checkin_uuid"),
        "startgg_event_id": payload.get("startgg_event_id"),
        "external_id": payload.get("external_id"),
        "added_via": _normalize_added_via(payload.get("added_via")),
        "player_uuid": matched_player_uuid,
    }

    if existing and existing.get("record_id"):
        checkin_id = existing["record_id"]
        updated = update_checkin(checkin_id, fields)
        if not updated:
            raise RuntimeError("Failed to update existing checkin")
        return {
            "checkin_id": checkin_id,
            "record_id": checkin_id,
            "event_slug": event_slug,
            "created": False,
            "player_uuid": matched_player_uuid,
        }

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO active_event_data (
                    event_slug, external_id,
                    name, tag, email, telephone,
                    status, member, startgg, payment_valid,
                    payment_amount, payment_expected,
                    tournament_games_registered, checkin_uuid,
                    startgg_event_id, is_guest, added_via, player_uuid
                ) VALUES (
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING record_id
                """,
                (
                    event_slug,
                    fields["external_id"],
                    fields["name"],
                    fields["tag"],
                    fields["email"],
                    fields["telephone"],
                    fields["status"],
                    fields["member"],
                    fields["startgg"],
                    fields["payment_valid"],
                    fields["payment_amount"],
                    fields["payment_expected"],
                    fields["tournament_games_registered"],
                    fields.get("checkin_uuid") or fields.get("UUID"),
                    fields["startgg_event_id"],
                    fields["is_guest"],
                    fields["added_via"],
                    matched_player_uuid,
                ),
            )
            checkin_id = cur.fetchone()[0]

    return {
        "checkin_id": checkin_id,
        "record_id": checkin_id,
        "event_slug": event_slug,
        "created": True,
        "player_uuid": matched_player_uuid,
    }


def apply_integration_result(
    checkin_id: str,
    source: str,
    ok: bool,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    fetched_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply integration result to a checkin record and write audit log."""
    if not checkin_id:
        raise ValueError("checkin_id is required")
    if not source:
        raise ValueError("source is required")

    data = data or {}
    update_fields: Dict[str, Any] = {}

    src = source.lower().strip()
    if src == "startgg":
        registered = bool(ok and data.get("registered", True))
        update_fields["startgg"] = registered
        # Keep guest flag aligned with Start.gg match result:
        # - matched on Start.gg => not guest
        # - not matched (manual add/guest flow) => guest
        update_fields["is_guest"] = not registered
        if isinstance(data.get("events"), list):
            update_fields["tournament_games_registered"] = data.get("events")
        if data.get("startgg_event_id"):
            update_fields["startgg_event_id"] = str(data.get("startgg_event_id"))
        if data.get("email"):
            update_fields["email"] = data["email"]
    elif src == "ebas":
        update_fields["member"] = bool(ok and data.get("member", True))
    elif src in ("swish", "stripe"):
        if data.get("payment_amount") is not None:
            update_fields["payment_amount"] = data.get("payment_amount")
        if data.get("payment_expected") is not None:
            update_fields["payment_expected"] = data.get("payment_expected")
        if data.get("payment_valid") is not None:
            update_fields["payment_valid"] = bool(data.get("payment_valid"))
        else:
            update_fields["payment_valid"] = bool(ok)

    updated = update_checkin(checkin_id, update_fields) if update_fields else None
    if update_fields and not updated:
        raise RuntimeError("Failed to update checkin from integration result")

    fields = (updated or {}).get("fields", {})
    event_slug = fields.get("event_slug") if isinstance(fields, dict) else None

    log_action(
        {"user_id": "integration", "user_name": f"n8n:{src}", "user_email": ""},
        "integration_result",
        "active_event_data",
        target_event=event_slug,
        target_record=checkin_id,
        details=json.dumps(
            {
                "source": src,
                "ok": ok,
                "data": data,
                "error": error,
                "fetched_at": fetched_at,
            }
        ),
    )

    return {
        "checkin_id": checkin_id,
        "source": src,
        "ok": ok,
        "updated": bool(update_fields),
    }


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
                       games_breakdown, most_popular_game, status_breakdown,
                       startgg_registered_count, checked_in_count,
                       no_show_count, no_show_rate
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
            startgg_registered_count,
            checked_in_count,
            no_show_count,
            no_show_rate,
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
                "startgg_registered_count": startgg_registered_count or 0,
                "checked_in_count": checked_in_count or 0,
                "no_show_count": no_show_count or 0,
                "no_show_rate": float(no_show_rate or 0),
            }
        )

    logger.info(f"📊 Retrieved {len(result)} dashboard-history rows.")
    return result


def get_event_manual_add_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return per-event manual check-in stats from event_archive.added_via."""
    conditions: List[str] = []
    params: List[Any] = []

    if event_slugs:
        conditions.append("event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT
            event_slug,
            COUNT(*) AS total_count,
            COUNT(*) FILTER (WHERE added_via = 'manual_dashboard') AS manual_count
        FROM event_archive
        {where_sql}
        GROUP BY event_slug
    """

    out: Dict[str, Dict[str, Any]] = {}
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    for slug, total_count, manual_count in rows:
        total_i = int(total_count or 0)
        manual_i = int(manual_count or 0)
        manual_pct = (manual_i / total_i * 100.0) if total_i > 0 else 0.0
        out[str(slug)] = {
            "total_count": total_i,
            "manual_count": manual_i,
            "manual_pct": manual_pct,
        }

    return out


def get_added_via_breakdown(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return added_via source distribution for archived rows in scope."""
    conditions: List[str] = []
    params: List[Any] = []

    if event_slugs:
        conditions.append("event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT
            COALESCE(NULLIF(added_via, ''), 'unknown') AS source,
            COUNT(*) AS cnt
        FROM event_archive
        {where_sql}
        GROUP BY source
        ORDER BY cnt DESC, source ASC
    """

    result: List[Dict[str, Any]] = []
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    total = sum(int(r[1] or 0) for r in rows)
    for source, cnt in rows:
        count_i = int(cnt or 0)
        share = (count_i / total * 100.0) if total > 0 else 0.0
        result.append({"source": source, "count": count_i, "share": share})

    return result


def get_top_players_history(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Return top participants by archived event attendance count."""
    conditions: List[str] = []
    params: List[Any] = []

    if event_slugs:
        conditions.append("event_slug = ANY(%s)")
        params.append(event_slugs)

    if start_date:
        conditions.append("event_date >= %s")
        params.append(start_date)

    if end_date:
        conditions.append("event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT
            MAX(COALESCE(NULLIF(tag, ''), NULLIF(name, ''), 'Unknown')) AS display_tag,
            MAX(COALESCE(NULLIF(name, ''), NULLIF(tag, ''), 'Unknown')) AS display_name,
            COUNT(DISTINCT event_slug) AS events_attended
        FROM event_archive
        {where_sql}
        GROUP BY player_uuid
        HAVING player_uuid IS NOT NULL
        ORDER BY events_attended DESC, display_name ASC
        LIMIT %s
    """
    params.append(limit)

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    result = []
    for idx, row in enumerate(rows, start=1):
        display_tag, display_name, events_attended = row
        result.append(
            {
                "rank": idx,
                "name": display_name,
                "tag": display_tag,
                "events_attended": int(events_attended or 0),
            }
        )

    return result


def get_unique_attendee_count(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Return the number of distinct players (by player_uuid) in the given scope."""
    conditions: List[str] = ["player_uuid IS NOT NULL"]
    params: List[Any] = []

    if event_slugs:
        conditions.append("event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}"

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(DISTINCT player_uuid) FROM event_archive {where_sql}",
                params,
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


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


def _find_player_uuid(tag: Optional[str], email: Optional[str]) -> Optional[str]:
    """
    Lightweight player lookup — match only, no creation or stat updates.

    Used during active check-in (begin_checkin) to link a check-in to an
    existing player profile.  Returns the player UUID if found, else None.

    Match priority: tag (case-insensitive) > email (case-insensitive).
    """
    if not tag and not email:
        return None

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            if tag:
                cur.execute(
                    "SELECT uuid FROM players WHERE LOWER(tag) = LOWER(%s) LIMIT 1",
                    (tag,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]

            if email:
                cur.execute(
                    "SELECT uuid FROM players WHERE LOWER(email) = LOWER(%s) LIMIT 1",
                    (email,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]

    return None


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

    def _coerce_to_iso_date(value: Any) -> Optional[str]:
        if value is None:
            return None

        # Unix timestamp support (seconds or milliseconds)
        if isinstance(value, (int, float)):
            ts = int(value)
            if ts > 10_000_000_000:
                ts = ts // 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            except Exception:
                return None

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit() and len(text) in (10, 13):
            ts = int(text)
            if len(text) == 13:
                ts = ts // 1000
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            except Exception:
                return None

        # ISO datetime/date strings (including trailing Z)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            pass

        # Plain date fallback
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except Exception:
                continue

        return None

    def _extract_date_from_snapshot(snapshot: Any, slug: str) -> Optional[str]:
        if not isinstance(snapshot, dict):
            return None

        date_keys = ["event_date", "date", "start_at", "startAt", "event_start_at", "eventStartAt"]

        for key in date_keys:
            parsed = _coerce_to_iso_date(snapshot.get(key))
            if parsed:
                return parsed

        events = snapshot.get("events")
        if isinstance(events, list):
            selected_event = None
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_slug = str(ev.get("event_slug") or ev.get("slug") or "").strip()
                if ev_slug and ev_slug == slug:
                    selected_event = ev
                    break
            if selected_event is None:
                selected_event = next((ev for ev in events if isinstance(ev, dict)), None)

            if isinstance(selected_event, dict):
                for key in date_keys:
                    parsed = _coerce_to_iso_date(selected_event.get(key))
                    if parsed:
                        return parsed

        return None

    if not event_date:
        if settings:
            ed = settings.get("event_date")
            if ed:
                event_date = ed.isoformat() if hasattr(ed, "isoformat") else str(ed)

        if not event_date:
            snapshot_for_date = startgg_snapshot
            if snapshot_for_date is None and settings:
                snapshot_for_date = settings.get("events_json")
            event_date = _extract_date_from_snapshot(snapshot_for_date, event_slug)

        if not event_date:
            event_date = datetime.now(timezone.utc).date().isoformat()
            logger.warning(
                "archive_event: missing event_date in payload/settings/snapshot for '%s'; "
                "falling back to today's date (%s)",
                event_slug,
                event_date,
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
                           external_id, startgg_event_id, is_guest, added_via
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
                        external_id, startgg_event_id, is_guest, added_via,
                        archived_at, player_uuid
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s, %s, %s,
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
                            c.get("is_guest"), c.get("added_via"),
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

                # 4b. No-show computation (person-based when available)
                checked_in_count = total
                startgg_registered_count = 0  # slot total (backward compat)
                startgg_registered_players = 0  # unique players
                if startgg_snapshot:
                    if isinstance(startgg_snapshot, dict):
                        startgg_registered_count = int(
                            startgg_snapshot.get("tournament_entrants") or 0
                        )
                        startgg_registered_players = int(
                            startgg_snapshot.get("tournament_entrants_players") or 0
                        )
                    elif isinstance(startgg_snapshot, list):
                        # Legacy format: sum per-event numEntrants
                        startgg_registered_count = sum(
                            int(e.get("numEntrants") or 0)
                            for e in startgg_snapshot
                            if isinstance(e, dict)
                        )
                # Use player count for no-show; fall back to slot count for old data
                no_show_base = startgg_registered_players or startgg_registered_count
                no_show_count = max(no_show_base - checked_in_count, 0)
                no_show_rate = (
                    (Decimal(no_show_count) / Decimal(no_show_base) * 100).quantize(
                        Decimal("0.01")
                    )
                    if no_show_base > 0
                    else Decimal("0")
                )
                if no_show_base > 0:
                    logger.info(
                        f"No-show: {no_show_count}/{no_show_base} "
                        f"({'players' if startgg_registered_players else 'slots (legacy)'})"
                    )

                # 5. Upsert event_stats (including startgg_snapshot + no-show)
                cur.execute(
                    """
                    INSERT INTO event_stats (
                        event_slug, event_date, event_display_name, archived_at,
                        total_participants, total_revenue, avg_payment,
                        member_count, member_percentage, guest_count, startgg_count,
                        new_players, returning_players, retention_rate,
                        games_breakdown, most_popular_game, status_breakdown,
                        startgg_snapshot,
                        startgg_registered_count, startgg_registered_players,
                        checked_in_count,
                        no_show_count, no_show_rate
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s,
                        %s, %s,
                        %s,
                        %s, %s
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
                        startgg_snapshot = EXCLUDED.startgg_snapshot,
                        startgg_registered_count = EXCLUDED.startgg_registered_count,
                        startgg_registered_players = EXCLUDED.startgg_registered_players,
                        checked_in_count = EXCLUDED.checked_in_count,
                        no_show_count = EXCLUDED.no_show_count,
                        no_show_rate = EXCLUDED.no_show_rate
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
                        startgg_registered_count,
                        startgg_registered_players,
                        checked_in_count,
                        no_show_count,
                        no_show_rate,
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

                    # Also reset active_event_slug in settings so the cleared
                    # event no longer appears as "active" in the select-event
                    # dropdown or forces stale dashboard state.
                    cur.execute(
                        """
                        UPDATE settings
                        SET active_event_slug = NULL
                        WHERE is_active = true
                          AND active_event_slug = %s
                        """,
                        (event_slug,),
                    )

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
        "startgg_registered_count": startgg_registered_count,
        "checked_in_count": checked_in_count,
        "no_show_count": no_show_count,
        "no_show_rate": float(no_show_rate),
        "replaced_rows": replaced_rows,
        "cleared_active": deleted_count if clear_active else 0,
    }


def reopen_event(
    event_slug: str,
    *,
    restore_active: bool = True,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Reopen an archived event for continued check-ins.

    - Sets active_event_slug in settings
    - Optionally restores active_event_data rows from event_archive snapshot
      when there are currently no active rows for the slug.
    """
    if not event_slug:
        raise ValueError("event_slug is required")

    now = datetime.now(timezone.utc)
    restored_rows = 0

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_date, event_display_name
                    FROM event_stats
                    WHERE event_slug = %s
                    LIMIT 1
                    """,
                    (event_slug,),
                )
                stats_row = cur.fetchone()

                if not stats_row:
                    cur.execute(
                        "SELECT COUNT(*) FROM event_archive WHERE event_slug = %s",
                        (event_slug,),
                    )
                    if (cur.fetchone()[0] or 0) == 0:
                        raise ValueError(f"No archived data found for event_slug '{event_slug}'")
                    stats_row = (None, "")

                event_date, event_display_name = stats_row

                cur.execute(
                    """
                    SELECT id
                    FROM settings
                    WHERE is_active = true
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if row:
                    settings_id = row[0]
                else:
                    cur.execute("INSERT INTO settings (is_active, created_at, updated_at) VALUES (true, now(), now()) RETURNING id")
                    settings_id = cur.fetchone()[0]

                event_date_value = event_date.isoformat() if event_date is not None and hasattr(event_date, "isoformat") else event_date

                cur.execute(
                    """
                    UPDATE settings
                    SET is_active = true,
                        active_event_slug = %s,
                        event_display_name = COALESCE(NULLIF(%s, ''), event_display_name),
                        event_date = COALESCE(%s::date, event_date),
                        startgg_event_url = NULL,
                        events_json = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        event_slug,
                        event_display_name or "",
                        event_date_value,
                        settings_id,
                    ),
                )

                if restore_active:
                    cur.execute(
                        "SELECT COUNT(*) FROM active_event_data WHERE event_slug = %s",
                        (event_slug,),
                    )
                    active_count = cur.fetchone()[0] or 0

                    if active_count == 0:
                        cur.execute(
                            """
                            INSERT INTO active_event_data (
                                record_id, event_slug, external_id,
                                name, tag, email, telephone,
                                status, member, startgg, payment_valid,
                                payment_amount, payment_expected,
                                tournament_games_registered, checkin_uuid,
                                startgg_event_id, is_guest, added_via, player_uuid,
                                created
                            )
                            SELECT
                                gen_random_uuid()::text,
                                event_slug,
                                external_id,
                                name,
                                tag,
                                email,
                                telephone,
                                status,
                                member,
                                startgg,
                                payment_valid,
                                payment_amount,
                                payment_expected,
                                tournament_games_registered,
                                checkin_uuid,
                                startgg_event_id,
                                is_guest,
                                COALESCE(NULLIF(added_via, ''), 'reopen_restore'),
                                player_uuid,
                                %s
                            FROM event_archive
                            WHERE event_slug = %s
                            """,
                            (now, event_slug),
                        )
                        restored_rows = cur.rowcount or 0

    reopen_user = user or {"user_id": "", "user_name": "system", "user_email": ""}
    log_action(
        reopen_user,
        "event_reopened",
        "settings",
        target_event=event_slug,
        details=json.dumps({"restore_active": restore_active, "restored_rows": restored_rows}),
    )

    logger.info(f"🔓 Reopened event '{event_slug}' (restored_rows={restored_rows})")
    return {
        "event_slug": event_slug,
        "reopened": True,
        "restore_active": restore_active,
        "restored_rows": restored_rows,
    }


def delete_archived_event(
    event_slug: str,
    *,
    reason: str = "",
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Permanently delete historical data for an archived event (event_archive + event_stats)."""
    if not event_slug:
        raise ValueError("event_slug is required")

    deleted_archive_rows = 0
    deleted_stats_rows = 0

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM event_archive WHERE event_slug = %s",
                    (event_slug,),
                )
                archive_count = cur.fetchone()[0] or 0

                cur.execute(
                    "SELECT COUNT(*) FROM event_stats WHERE event_slug = %s",
                    (event_slug,),
                )
                stats_count = cur.fetchone()[0] or 0

                if archive_count == 0 and stats_count == 0:
                    raise ValueError(f"No archived data found for event_slug '{event_slug}'")

                cur.execute("DELETE FROM event_archive WHERE event_slug = %s", (event_slug,))
                deleted_archive_rows = cur.rowcount or 0

                cur.execute("DELETE FROM event_stats WHERE event_slug = %s", (event_slug,))
                deleted_stats_rows = cur.rowcount or 0

    delete_user = user or {"user_id": "", "user_name": "system", "user_email": ""}
    log_action(
        delete_user,
        "event_deleted_from_history",
        "event_archive",
        target_event=event_slug,
        reason=reason or "",
        details=json.dumps(
            {
                "deleted_archive_rows": deleted_archive_rows,
                "deleted_stats_rows": deleted_stats_rows,
            }
        ),
    )

    logger.info(
        "🗑️ Deleted archived event '%s' (archive_rows=%s, stats_rows=%s)",
        event_slug,
        deleted_archive_rows,
        deleted_stats_rows,
    )
    return {
        "event_slug": event_slug,
        "deleted_archive_rows": deleted_archive_rows,
        "deleted_stats_rows": deleted_stats_rows,
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


# =============================================
# Player Merge Engine
# =============================================


def find_duplicate_candidates(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Detect potential duplicate player pairs.

    Matching signals (ordered by confidence):
    1. Same telephone number (highest confidence — V1 auto-merge threshold)
    2. Similar tag  (Levenshtein distance <= 2, case-insensitive)
    3. Similar name (Levenshtein distance <= 2, case-insensitive)

    Returns list of candidate pairs with match reason and confidence level.
    """
    candidates: List[Dict[str, Any]] = []

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Load all players
            cur.execute("""
                SELECT uuid, name, tag, email, telephone, total_events
                FROM players
                ORDER BY total_events DESC, name
            """)
            players = cur.fetchall()

    # Build lookup structures
    player_list = []
    for row in players:
        p_uuid, name, tag, email, telephone, total_events = row
        player_list.append({
            "uuid": p_uuid,
            "name": (name or "").strip(),
            "tag": (tag or "").strip(),
            "email": (email or "").strip(),
            "telephone": (telephone or "").strip(),
            "total_events": total_events or 0,
        })

    seen_pairs = set()

    for i, a in enumerate(player_list):
        for b in player_list[i + 1:]:
            pair_key = tuple(sorted([a["uuid"], b["uuid"]]))
            if pair_key in seen_pairs:
                continue

            reasons = []
            confidence = "low"

            # 1. Phone match (strongest signal)
            if a["telephone"] and b["telephone"] and a["telephone"] == b["telephone"]:
                reasons.append("same_phone")
                confidence = "high"

            # 2. Tag similarity (case-insensitive)
            if a["tag"] and b["tag"]:
                tag_a = a["tag"].lower()
                tag_b = b["tag"].lower()
                if tag_a == tag_b:
                    # Exact case-insensitive match — should have been caught by _match_or_create
                    reasons.append("exact_tag")
                    confidence = "high"
                elif _levenshtein(tag_a, tag_b) <= 2:
                    reasons.append("similar_tag")
                    if confidence != "high":
                        confidence = "medium"

            # 3. Name similarity (case-insensitive)
            if a["name"] and b["name"]:
                name_a = a["name"].lower()
                name_b = b["name"].lower()
                if name_a == name_b:
                    reasons.append("exact_name")
                    if confidence != "high":
                        confidence = "medium"
                elif _levenshtein(name_a, name_b) <= 2:
                    reasons.append("similar_name")

            if reasons:
                seen_pairs.add(pair_key)
                candidates.append({
                    "player_a": a,
                    "player_b": b,
                    "reasons": reasons,
                    "confidence": confidence,
                })

            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    # Sort by confidence (high first), then by number of reasons
    conf_order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (conf_order.get(c["confidence"], 3), -len(c["reasons"])))

    logger.info(f"🔍 Found {len(candidates)} duplicate candidates")
    return candidates


def _levenshtein(s1: str, s2: str) -> int:
    """Simple Levenshtein distance for short strings (player names/tags)."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]


def merge_players(
    keep_uuid: str,
    remove_uuid: str,
    *,
    reason: str = "",
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Merge two player profiles: keep one, absorb the other.

    What happens:
    1. Snapshot the removed player (for undo)
    2. Re-point all event_archive rows from remove_uuid → keep_uuid
    3. Re-point all active_event_data rows from remove_uuid → keep_uuid
    4. Merge stats into keep player (events_list, games, totals)
    5. Delete the removed player profile
    6. Log to merge_log (undo-capable) + audit_log

    Returns merge summary dict.
    """
    from psycopg.types.json import Json as _Json  # type: ignore

    if not keep_uuid or not remove_uuid:
        raise ValueError("Both keep_uuid and remove_uuid are required")
    if keep_uuid == remove_uuid:
        raise ValueError("Cannot merge a player with itself")

    merge_user = user or {"user_id": "", "user_name": "system", "user_email": ""}

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # 1. Load both players
                cur.execute("SELECT * FROM players WHERE uuid = %s", (keep_uuid,))
                keep_row = cur.fetchone()
                if not keep_row:
                    raise ValueError(f"Keep player not found: {keep_uuid}")
                keep_cols = [desc[0] for desc in cur.description]
                keep_player = _row_to_dict(keep_cols, keep_row)

                cur.execute("SELECT * FROM players WHERE uuid = %s", (remove_uuid,))
                remove_row = cur.fetchone()
                if not remove_row:
                    raise ValueError(f"Remove player not found: {remove_uuid}")
                remove_player = _row_to_dict(keep_cols, remove_row)

                # 2. Snapshot removed player (for undo)
                # Convert non-serializable types
                snapshot = {}
                for k, v in remove_player.items():
                    if isinstance(v, Decimal):
                        snapshot[k] = float(v)
                    elif isinstance(v, datetime):
                        snapshot[k] = v.isoformat()
                    elif hasattr(v, "isoformat"):
                        snapshot[k] = v.isoformat()
                    else:
                        snapshot[k] = v

                # 3. Re-point event_archive rows
                cur.execute(
                    "UPDATE event_archive SET player_uuid = %s WHERE player_uuid = %s",
                    (keep_uuid, remove_uuid),
                )
                archive_updated = cur.rowcount or 0

                # 4. Re-point active_event_data rows
                cur.execute(
                    "UPDATE active_event_data SET player_uuid = %s WHERE player_uuid = %s",
                    (keep_uuid, remove_uuid),
                )
                active_updated = cur.rowcount or 0

                # 5. Merge stats into keep player
                keep_events = keep_player.get("events_list") or []
                if not isinstance(keep_events, list):
                    keep_events = []
                remove_events = remove_player.get("events_list") or []
                if not isinstance(remove_events, list):
                    remove_events = []
                merged_events = list(dict.fromkeys(keep_events + remove_events))

                keep_games = keep_player.get("games_played") or []
                remove_games = remove_player.get("games_played") or []
                merged_games = list(set(keep_games) | set(remove_games))

                keep_game_counts = keep_player.get("game_counts") or {}
                if not isinstance(keep_game_counts, dict):
                    keep_game_counts = {}
                remove_game_counts = remove_player.get("game_counts") or {}
                if not isinstance(remove_game_counts, dict):
                    remove_game_counts = {}
                merged_game_counts = dict(keep_game_counts)
                for g, cnt in remove_game_counts.items():
                    merged_game_counts[g] = merged_game_counts.get(g, 0) + (cnt or 0)
                merged_favorite = (
                    max(merged_game_counts, key=merged_game_counts.get)
                    if merged_game_counts
                    else keep_player.get("favorite_game")
                )

                merged_total_events = len(merged_events)
                merged_total_paid = Decimal(str(keep_player.get("total_paid") or 0)) + Decimal(
                    str(remove_player.get("total_paid") or 0)
                )

                # Use earliest first_seen, latest last_seen
                keep_first = keep_player.get("first_seen")
                remove_first = remove_player.get("first_seen")
                if keep_first and remove_first:
                    merged_first = min(keep_first, remove_first)
                else:
                    merged_first = keep_first or remove_first

                keep_last = keep_player.get("last_seen")
                remove_last = remove_player.get("last_seen")
                if keep_last and remove_last:
                    merged_last = max(keep_last, remove_last)
                else:
                    merged_last = keep_last or remove_last

                # Prefer keep player's identity fields, fill gaps from removed
                merged_name = keep_player.get("name") or remove_player.get("name")
                merged_tag = keep_player.get("tag") or remove_player.get("tag")
                merged_email = keep_player.get("email") or remove_player.get("email")
                merged_phone = keep_player.get("telephone") or remove_player.get("telephone")
                merged_member = (keep_player.get("is_member") or False) or (
                    remove_player.get("is_member") or False
                )

                now = datetime.now(timezone.utc)
                cur.execute(
                    """
                    UPDATE players SET
                        name = %s,
                        tag = %s,
                        email = %s,
                        telephone = %s,
                        games_played = %s,
                        game_counts = %s,
                        favorite_game = %s,
                        total_events = %s,
                        total_paid = %s,
                        first_seen = %s,
                        last_seen = %s,
                        first_event = %s,
                        last_event = %s,
                        events_list = %s,
                        is_member = %s,
                        updated_at = %s
                    WHERE uuid = %s
                    """,
                    (
                        merged_name,
                        merged_tag,
                        merged_email,
                        merged_phone,
                        merged_games,
                        _Json(merged_game_counts),
                        merged_favorite,
                        merged_total_events,
                        merged_total_paid,
                        merged_first,
                        merged_last,
                        merged_events[0] if merged_events else keep_player.get("first_event"),
                        merged_events[-1] if merged_events else keep_player.get("last_event"),
                        _Json(merged_events),
                        merged_member,
                        now,
                        keep_uuid,
                    ),
                )

                # 6. Delete the removed player
                cur.execute("DELETE FROM players WHERE uuid = %s", (remove_uuid,))

                # 7. Write to merge_log
                cur.execute(
                    """
                    INSERT INTO merge_log (
                        keep_uuid, remove_uuid,
                        user_id, user_name, reason,
                        removed_player_snapshot,
                        archive_rows_updated, active_rows_updated
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        keep_uuid,
                        remove_uuid,
                        merge_user.get("user_id", ""),
                        merge_user.get("user_name", "system"),
                        reason,
                        _Json(snapshot),
                        archive_updated,
                        active_updated,
                    ),
                )
                merge_id = cur.fetchone()[0]

    # Audit log (outside transaction — non-critical)
    log_action(
        merge_user,
        "player_merged",
        "players",
        target_player=keep_uuid,
        reason=reason,
        details=json.dumps({
            "merge_id": merge_id,
            "keep_uuid": keep_uuid,
            "remove_uuid": remove_uuid,
            "archive_rows_updated": archive_updated,
            "active_rows_updated": active_updated,
        }),
    )

    logger.info(
        f"🔀 Merged player {remove_uuid[:8]}… into {keep_uuid[:8]}… "
        f"(archive={archive_updated}, active={active_updated}, merge_id={merge_id})"
    )

    return {
        "merge_id": merge_id,
        "keep_uuid": keep_uuid,
        "remove_uuid": remove_uuid,
        "archive_rows_updated": archive_updated,
        "active_rows_updated": active_updated,
        "keep_player_name": merged_name,
        "keep_player_tag": merged_tag,
        "removed_player_name": remove_player.get("name"),
        "removed_player_tag": remove_player.get("tag"),
    }


def undo_merge(
    merge_id: int,
    *,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Undo a player merge by restoring the removed player and reverting UUID pointers.

    1. Reads the merge_log entry to get the removed player snapshot
    2. Re-creates the removed player from snapshot
    3. Reverts event_archive rows back to the removed UUID
    4. Reverts active_event_data rows back to the removed UUID
    5. Marks the merge_log entry as undone
    """
    from psycopg.types.json import Json as _Json  # type: ignore

    undo_user = user or {"user_id": "", "user_name": "system", "user_email": ""}

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # 1. Load merge log entry
                cur.execute(
                    "SELECT * FROM merge_log WHERE id = %s",
                    (merge_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Merge log entry not found: {merge_id}")
                cols = [desc[0] for desc in cur.description]
                entry = _row_to_dict(cols, row)

                if entry.get("undone"):
                    raise ValueError(f"Merge {merge_id} has already been undone")

                keep_uuid = entry["keep_uuid"]
                remove_uuid = entry["remove_uuid"]
                snapshot = entry["removed_player_snapshot"]

                if not isinstance(snapshot, dict):
                    raise ValueError(f"Invalid snapshot for merge {merge_id}")

                # 2. Re-create the removed player from snapshot
                cur.execute(
                    """
                    INSERT INTO players (
                        uuid, name, tag, email, telephone,
                        games_played, game_counts, favorite_game,
                        total_events, total_paid,
                        first_seen, last_seen, first_event, last_event,
                        events_list, is_member, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, now()
                    )
                    """,
                    (
                        remove_uuid,
                        snapshot.get("name"),
                        snapshot.get("tag"),
                        snapshot.get("email"),
                        snapshot.get("telephone"),
                        snapshot.get("games_played") or [],
                        _Json(snapshot.get("game_counts") or {}),
                        snapshot.get("favorite_game"),
                        snapshot.get("total_events") or 0,
                        snapshot.get("total_paid") or 0,
                        snapshot.get("first_seen"),
                        snapshot.get("last_seen"),
                        snapshot.get("first_event"),
                        snapshot.get("last_event"),
                        _Json(snapshot.get("events_list") or []),
                        snapshot.get("is_member") or False,
                        snapshot.get("created_at"),
                    ),
                )

                # 3. Figure out which archive rows belonged to the removed player
                # Use the removed player's events_list to identify which rows to revert
                remove_events = snapshot.get("events_list") or []
                archive_reverted = 0
                if remove_events:
                    cur.execute(
                        """
                        UPDATE event_archive
                        SET player_uuid = %s
                        WHERE player_uuid = %s
                          AND event_slug = ANY(%s)
                        """,
                        (remove_uuid, keep_uuid, remove_events),
                    )
                    archive_reverted = cur.rowcount or 0

                # 4. Revert active_event_data (use tag match as heuristic)
                active_reverted = 0
                remove_tag = snapshot.get("tag")
                if remove_tag:
                    cur.execute(
                        """
                        UPDATE active_event_data
                        SET player_uuid = %s
                        WHERE player_uuid = %s AND LOWER(tag) = LOWER(%s)
                        """,
                        (remove_uuid, keep_uuid, remove_tag),
                    )
                    active_reverted = cur.rowcount or 0

                # 5. Mark as undone
                cur.execute(
                    "UPDATE merge_log SET undone = true, undone_at = now() WHERE id = %s",
                    (merge_id,),
                )

    # Audit
    log_action(
        undo_user,
        "player_merge_undone",
        "players",
        target_player=remove_uuid,
        details=json.dumps({
            "merge_id": merge_id,
            "keep_uuid": keep_uuid,
            "remove_uuid": remove_uuid,
            "archive_reverted": archive_reverted,
            "active_reverted": active_reverted,
        }),
    )

    logger.info(
        f"↩️ Undid merge #{merge_id}: restored {remove_uuid[:8]}… "
        f"(archive={archive_reverted}, active={active_reverted})"
    )

    return {
        "merge_id": merge_id,
        "undone": True,
        "restored_uuid": remove_uuid,
        "restored_name": snapshot.get("name"),
        "restored_tag": snapshot.get("tag"),
        "archive_reverted": archive_reverted,
        "active_reverted": active_reverted,
    }


def get_merge_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent merge log entries, newest first."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, merged_at, keep_uuid, remove_uuid,
                       user_name, reason,
                       removed_player_snapshot,
                       archive_rows_updated, active_rows_updated,
                       undone, undone_at
                FROM merge_log
                ORDER BY merged_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        (
            mid, merged_at, keep_uuid, remove_uuid,
            user_name, reason, snapshot,
            archive_updated, active_updated,
            undone, undone_at,
        ) = row

        # Extract display names from snapshot
        removed_name = snapshot.get("name", "") if isinstance(snapshot, dict) else ""
        removed_tag = snapshot.get("tag", "") if isinstance(snapshot, dict) else ""

        result.append({
            "id": mid,
            "merged_at": merged_at.isoformat() if merged_at else None,
            "keep_uuid": keep_uuid,
            "remove_uuid": remove_uuid,
            "user_name": user_name,
            "reason": reason,
            "removed_name": removed_name,
            "removed_tag": removed_tag,
            "archive_rows_updated": archive_updated or 0,
            "active_rows_updated": active_updated or 0,
            "undone": undone or False,
            "undone_at": undone_at.isoformat() if undone_at else None,
        })

    return result
